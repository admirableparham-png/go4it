# Phase 9 — AI Command, Research Orchestration & Safe Automation

Command is now an **admin-only AI copilot** over the Go4it data. It is **deterministic-first**: internal search,
metric lookup, citations, summaries, drafts and action *proposals* all work with **no AI provider**; an LLM is an
optional, provider-neutral orchestration/phrasing layer that is **mocked/off in tests and never called live**.
The copilot is evidence-based, tenant-scoped, confidentiality-aware, auditable, permission-controlled, and
**cannot send outreach or change critical state without an explicit, revalidated runtime approval**. Header nav
only; no permanent sidebar. Trained Research agents/prompts/tools and buyer-harvesting are used read-only and are
unchanged.

## Checkpoint A — conversational Command, evidence, read-only tools
- **`ai_encryption.py`** — a dedicated rotating key set **`AI_DATA_ENCRYPTION_KEYS`** (keys[0] encrypt /
  MultiFernet decrypt, fail-closed on public, documented rotation) — **separate** from `SECRET_KEY` and the
  mailbox credential keys. `redact_secrets()` strips keys/seeds/passwords/tokens **before** any content is
  encrypted or stored; no message content or API key is ever written to logs.
- **`ai_provider.py`** — provider-neutral abstraction. `provider_status()` reports configured/model/allowlist;
  a deterministic **MockProvider** (tests + offline canary), a **NotConfigured** default ("AI provider not
  configured"), and env-gated **live-provider stubs** (no invented endpoints; never called in tests). Strict
  timeout, bounded retries (never a write), max output/tool-steps, per-admin/tenant budgets, usage records,
  emergency **Pause-All**.
- **`ai_prompts.py`** — versioned + checksummed trusted system instructions (role/permissions, confidentiality,
  evidence requirement, tool restrictions, no autonomous comms, retrieved-content-is-untrusted).
- **`ai_search.py`** — allowlisted **structured** search over 16 entities. The model **never** writes SQL:
  validated filter/sort fields, tenant scope, capped limits, and **safe projections** (curated fields only).
  Credential-bearing tables (User/MailAccount/tokens/sessions) are **not registered**.
- **`ai_tools.py`** — one central **tool registry**. The model may select only registered tools; there is **no**
  shell/code/SQL/URL/file/env/credential/auth/payment/contract/quote-accept tool. Read-only tools (search,
  get_metric, source_health, summarize_*, explain_opportunity_score, overdue_work_items, compare_markets) run
  without a separate approval, still authz'd + limited + audited (summaries only — no full payloads).
- **`ai_citations.py`** — every material claim cites a real record (type, safe ref, timestamp, source, freshness,
  observed/verified/derived/inferred, authorized link). Metric answers state definition/range/unit/freshness/
  min-sample; currencies are never combined; prospects are never called "buyers"; negatives are never demand.
  Honest fallbacks ("I could not verify this from current Go4it data." / "Insufficient data." / "…stale." /
  "…an estimate.") — never a fabricated citation.
- **`ai_command.py`** — a **deterministic intent router** + bounded read-only tool loop that answers with or
  without a provider; prompt-injection is detected and **refused + flagged** (a `prompt_injection_review` WQ
  item); messages are stored **encrypted**; a GET never mutates business data. The Command page is redesigned
  into a conversational copilot (history drawer, evidence cards, suggested questions, proposal cards, cancel/
  Pause-All) in the existing dark palette; the legacy buyer-harvest routes are preserved.

## Checkpoint B — actions, approvals, research, automation, evaluations
- **`ai_actions.py`** — the AI **proposes**, an admin **approves**, then a single registered executor runs it via
  an existing domain service. Approval enforces a **one-time nonce** (constant-time), admin authz,
  still-`proposed`+not-expired, **payload-hash match** (stale/tampered → rejected), **idempotent** execution
  (double-click = no-op), and target-state revalidation. Free-form model text is never executed. **Never
  executable by the AI:** payments/remittance, buyer accept/reject, contract signing, suppression bypass, role/
  auth change, audit deletion, credential access, prod-config change, malware/quarantine override — refused at
  propose time.
- **Research orchestration** — a `start_research` proposal creates a `CommandJob` via the **existing** pipeline
  on approval, left **queued** (never auto-run, never claims results early); the route background-runs the
  existing harvest only after approval. Results flow through the existing provenance/dedup/Trade Network path.
- **`ai_drafts.py`** — evidence-based drafts, **never sent or published**: seller-facing drafts block on buyer
  PII (`sanitize_scan`), buyer-facing block on internal cost/margin (+ `strip_seller_identity`), and no
  unresolved placeholders. The Phase-4 send guards / suppression / attachment lock remain the only send path.
- **`automation.py`** — deterministic rules (the AI may only *recommend* one). Actions are **internal only**
  (WorkItem/alert/draft summary/assign review) — automation never sends email, starts campaigns, issues quotes/
  contracts, advances Deals, moves funds, or publishes without existing approval. Idempotent (one run per
  rule+condition_version), bounded (count + wall-clock), per-rule isolated, **dry-run** (no side effects),
  **auto-pause** after repeated failure, global **Pause-All**.
- **`ai_brief.py`** — a cited daily/weekly admin intelligence brief from the Phase-8 metric registry, **in-app
  only** (never auto-emailed).
- **`ai_eval.py`** — a self-contained deterministic evaluation suite (10 scenarios incl. injection refusal,
  cross-currency, negative-exclusion, buyer-PII-leak, payment-refused, seller-draft-PII-blocked, arbitrary-tool-
  refused, insufficient-honest). A regression raises an `ai_evaluation_regression` WQ item.

## Work Queue (§25)
12 new condition-versioned types (`ai_provider_failure, ai_budget_threshold, research_job_failed,
research_result_needs_review, ai_action_awaiting_approval, ai_action_failed, automation_failed,
automation_auto_paused, prompt_injection_review, stale_ai_proposal, brief_generation_failed,
ai_evaluation_regression`) + bounded scanners (`stale_ai_proposal`, `automation`). WorkItem gains
`related_conversation_id/proposal_id/automation_id`.

## Migration & backfill (run order)
```
backup_db.py → migrate.py → migrate_gate_p9.py [--dry-run] → backfill_command_history.py [--dry-run]
```
`migrate_gate_p9.py` idempotently creates the 10 AI tables + partial-unique indexes + the additive WorkItem
columns, runs `PRAGMA integrity_check`, and asserts operational counts (leads/quotes/deals/requests/outreach/
products) invariant. `backfill_command_history.py` is **conservative**: each existing `CommandJob` becomes one
archived, **imported** `AIConversation` with a single encrypted historical message — it **never** re-executes a
command, generates actions/citations, starts Research, creates automation, or calls a provider. `--rollback`
removes the imported archives; `--recover` removes only untouched imports, preserving real conversations/
approvals/actions/automation.

**Proven on a WAL-safe dev-DB snapshot** (`backups/data-20260825-131427.db`): gate dry-run→apply→re-run
idempotent; backfill dry-run→apply→re-run→rollback archived **5** CommandJobs as imported conversations (removed
exactly 5 on rollback); operational counts invariant (leads 4581 / quotes 56 / deals 1 / requests 1 / outreach
197 / products 96); app boots.

## Offline AI canary (`scripts/ai_canary.py`, also `tests/test_ai_canary.py`)
On a disposable DB with **no live provider**: a cited metric answer (deterministic) → messages encrypted, no
buyer PII → a Research **proposal** (not executed; no CommandJob before approval) → an action proposal approved
into **one harmless internal WorkItem** (idempotent) → a payment action refused → a prompt-injection attempt
refused + flagged → a seller denied **every** AI route (403) → a second admin denied the first admin's
conversation (404) → **Pause-All** halts provider work while deterministic answers keep working. **PASSES.** The
**live canary** (real provider, allowlisted admin, read-only-first, strict cost cap, no external messaging) is
documented + gated and is **never simulated** here.

## Security & confidentiality
All AI pages/actions/exports are **admin-only**; conversations are owner-scoped (cross-admin → 404); sellers get
403/404 on every AI route. Message content is encrypted at rest under a dedicated key; secrets are redacted
before persistence; API keys are never rendered or logged. Retrieved/untrusted content can never redefine system
instructions or authorize a tool; arbitrary URL/private-IP/`file://` retrieval is blocked. Every tool call,
proposal, approval and draft is audited. No business mutation on a GET.

## Deferred / production-config (NOT production-ready — gates unchanged)
Wiring a real AI provider + running the **live AI production canary** (gated to an allowlisted admin, read-only-
first) is a production-config step. Embeddings/semantic search → "Not configured" until wired. The platform
stays **not production-ready** until the Phase-4 email canary, `pdf_smoke.py` in the prod container, the Phase-5
Product→Catalog staging canary, real malware-scanner integration, **and** the Phase-9 AI production canary pass —
none block safe local work.
