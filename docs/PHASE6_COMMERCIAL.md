# Phase 6 — Commercial: Quotes, Contracts & Deals

Admin-only Commercial workspace (no sidebar): **Quotes · Contracts · Deals · Templates · Commercial
Analytics**. The journey: Product/pricing → **Quote → buyer decision → Contracts → Deal**. Additive; existing
`compute_quote`, `DEAL_STAGES`, the confidential Trade Network, and Phase 4/5 systems are unchanged.
**Shipment/freight/customs/remittance execution is Phase 7.**

## Data model (additive)
- **Quote** (extended): `current_version_id`, `viewed_at`; status vocabulary expands to 11 states
  (draft/needs_review/approved/sent/viewed/change_requested/accepted/rejected/expired/cancelled/superseded).
- **Deal** (extended): `quote_version_id` (partial-unique — one Deal per accepted version), `ready_for_ops`.
- **WorkItem** (extended): `related_contract_id`.
- **New tables:** `QuoteVersion` (immutable snapshot), `QuoteLineItem`, `QuoteStatusEvent`, `QuoteApproval`,
  `QuoteAccessToken` (hashed), `QuoteDocument` (immutable PDF + sha256); `Contract`, `ContractVersion`,
  `ContractParty`, `ContractStatusEvent`, `ContractTemplate`, `ContractDocument`, `SignatureEvent`.

## Quotes (Checkpoint A)
- **Central workflow** (`app/quote_workflow.py`): one `TRANSITIONS` map, every move validated server-side +
  recorded as a `QuoteStatusEvent` + audited. Only approved→sent; only a sent/viewed, current, unexpired
  version → accepted; expiry computed one way; a scanner flips overdue quotes to `expired`. GET never mutates.
- **Immutable versions** (`quote_service.ensure_version`): each Quote row freezes a `QuoteVersion` snapshot
  (product identity + version + spec + origin + packaging + lead-time + terms + FX/params + margin **and**
  markup, distinct). A revision **duplicates** into a new draft (`revise_quote`) — never edits sent/approved.
  `compute_quote` unchanged (compat-tested).
- **Secure buyer portal** `/q/{token}` (`app/quote_portal.py`): `QuoteAccessToken` is a random string stored
  only as its **sha256 hash** (never plaintext, never logged), scoped to one version, expiring, revocable,
  rotatable. Buyer view/accept/reject/change; **acceptance is idempotent** (double-submit safe) and creates no
  Deal directly — it raises the admin `accepted_quote_needs_deal` task. Rate-limited (`app/ratelimit.py`). The
  legacy `/p/{token}` stays for the 56 existing quotes.
- **Deal on accept:** admin-triggered (`/quotes/{id}/create-deal` →
  `deal_service.ensure_deal_for_quote_version`), idempotent + concurrency-safe via the partial-unique index
  (exactly one Deal per accepted version); reconciled with the legacy won→deal path (no duplicates).
- **Quote PDF** (`app/quote_pdf.py` + `pdf_render.py`): branded, escaped, buyer-safe (no exw/margin/cost/
  seller), rendered through the Phase-5 hardened sandbox (JS-off/offline/network-blocked), stored immutable
  with sha256; no-unresolved-`{{var}}` guard; admin download or buyer-via-token.
- **Sending:** secure-link-only in the email body. The generated-PDF **email attachment is deferred** — the
  `verify_pdf` seam (sha256 + `application/pdf` MIME + `%PDF-` signature + size cap) is built but not wired; the
  **global attachment lock is untouched**.

## Contracts, deals, analytics (Checkpoint B)
- **Contracts** (`app/contract_service.py`): admin explicitly chooses type + side + party; central workflow;
  immutable versions (amendments are new linked versions). Buyer-side and supplier-side are **separate,
  confidential documents** (no cross-party leakage; no "both parties" document).
- **Templates:** allowlisted `{{variables}}` only (unknown/unresolved rejected), HTML-escaped, versioned,
  archive-not-delete, with a "not legal advice" notice.
- **Signed documents + e-sign** (`app/esign.py`): uploaded signed copies are private + **quarantined** +
  admin-only until scan-cleared + hashed + never overwrite the generated original. E-signature is an honest
  **"Not configured"** provider abstraction; manual tracking only — a typed name is **never** a verified
  signature (mocked in tests).
- **Deals:** handoff readiness (`deal_ready_for_ops`) produces Work Queue tasks, **no fake shipments**;
  `DEAL_STAGES` + seller-safe projection preserved.
- **Analytics** `/commercial/analytics`: quotes by status, conversion rates, value **per currency** (never
  summed across currencies), planned vs realized margin; count / value / rate shown distinctly.
- **Work Queue:** 15 new commercial types + condition-versioned scanners (quote review/expired/accepted-needs-
  deal, contract awaiting signature, …).

## Buyer-portal hardening (pre-Phase 7)
- **Token never in a server-visible URL.** The buyer link is `{BASE_URL}/q/#<token>` — the token lives ONLY in
  the URL **fragment**, which browsers never send to the server, so it cannot enter reverse-proxy/access logs.
  `GET /q/` serves a tiny bootstrap page (the server sees only `/q/`, no token); a nonce'd script reads the
  fragment, **`history.replaceState`s it out of the URL + history**, and POSTs it to `POST /q/exchange`.
- **Server-side session; cookie holds only an opaque id.** The exchange opens a **`PortalSession`** row
  (server-side) holding the quote version, token hash, expiry, revocation state + CSRF; the browser cookie
  carries ONLY the opaque `sid`. Nothing sensitive lives in a client-readable/signed cookie. Subsequent
  navigation + decisions use the tokenless `/q/session*` paths.
- **Single-use link.** The link is **atomically consumed** on first exchange (`QuoteAccessToken.consumed_at`
  set via a compare-and-swap on NULL); a buyer reopening the still-valid link **re-issues the same** session
  (not a second one). Expired/revoked/consumed-and-gone → fails safe.
- **Security headers** on every portal response: `Referrer-Policy: no-referrer`, `Cache-Control: no-store`,
  strict CSP (`default-src 'none'; frame-ancestors 'none'; script-src 'nonce-…'; connect-src 'self'`),
  `X-Frame-Options: DENY`, `X-Robots-Tag: noindex`.
- **Decisions protected:** accept/reject/change are **POST-only** (`/q/session/respond`), **CSRF-checked**,
  **idempotent for every decision** (replay/double-submit safe), scoped to the **exact version** the buyer
  saw, rate-limited. Acceptance only raises the admin deal task.
- **Viewed via POST:** the GET render never mutates; a nonce'd beacon fires an **idempotent
  `POST /q/session/view`** after render to record `sent→viewed` once.
- **EXW:** the internal EXW **cost** is never shown; an intentionally-included buyer-facing **EXW commercial
  option** (approved buyer price, included/excluded) IS displayed via `QuoteVersion.options`.

### Production proxy note (still required)
The fragment approach means the raw token never reaches the origin server or its access logs. As
defence-in-depth, the production reverse proxy should still be reviewed to confirm `/q/` request URLs are not
being expanded/logged with query strings, and **the actual production proxy config must be tested** (not only
application logs) before go-live. If a future flow ever puts a token in the path/query, redact `/q/*` in the
proxy access log.

## Work Queue count reconciliation
The Phase-5 report's "87 incomplete-product tasks" were a **scanner PREDICTION** computed inside a rolled-back
savepoint — they were **never persisted**. The current open queue is the pre-existing set (≈105–108: 53
`approve_quote`, 29 `review_potential_duplicate`, 12 `failed_system_job`, 11 `replace_invalid_contact`).
**The first production `run_all_sync` (the worker's periodic repair pass) will CREATE** the **87
`product_incomplete`** tasks (products missing HS/origin/unit/price/supplier/category) plus a small number of
`quote_expired` tasks (legacy sent quotes past validity), taking the open total to ≈195. This is expected,
idempotent, and condition-versioned (re-running creates no duplicates).

## Migration (run order)
```
backup_db.py → migrate.py → migrate_gate_p6.py [--dry-run] → backfill_commercial.py [--dry-run]
```
`migrate_gate_p6.py` idempotently creates the 13 tables + `uq_quoteaccesstoken_hash` + `uq_deal_quote_version`
+ indexes, runs `PRAGMA integrity_check`, and asserts operational counts (quotes/deals/requests/outreach/
products) invariant. `backfill_commercial.py` is **conservative**: one inferred `QuoteVersion` per existing
Quote (status mirrors the quote — never elevated to approved; **never sent, never tokened, never a Deal**),
links deals to their version deterministically, ambiguous → review task. Applied to the dev DB: **56 versions,
1 deal linked, 0 ambiguous; counts invariant**.

## Rollback / recovery
- Pre-go-live: `backfill_commercial.py --rollback` (deletes inferred versions + unlinks deals; quotes/deals/
  totals preserved) or restore the dated backup. Gate schema is additive.
- Post-go-live: `backfill_commercial.py --recover` removes only untouched inferred versions (no approval/token/
  deal reference); preserves every real approval, buyer action, contract and deal.

## Deferred / production-config
- Generated-PDF **email attachment** — deferred (link-only); verify seam shipped.
- **E-signature provider** — Not configured (manual tracking + mocks).
- **PDF in prod** — needs Chromium (`pdf_smoke.py`); degrades to a work item otherwise.
- **Phase 7** — shipment/freight/customs/delivery/documentation/remittance. **Phase 8** — predictive analytics.

## Production gates (unchanged — NOT production-ready)
The platform stays **not production-ready** until the Phase-4 **live email canary** passes, `pdf_smoke.py`
passes **inside the production container**, and the Phase-5 **Product→Catalog staging canary** passes.
