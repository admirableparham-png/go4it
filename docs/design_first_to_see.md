# go4it — Integrated Recommendation: First to See, Fastest to Respond

## 1) Recommended Solution

Stop trying to scrape go4worldbusiness and instead **let go4world push leads to you**: its own paid membership already emails you every keyword-matched buy-lead "as and when they get published." We make the go4it pipeline consume those emails (read-only Gmail, zero portal requests, zero re-login, zero block risk), have Claude parse → prioritize → draft a bilingual (Russian + Georgian) quote **instantly**, and drop a one-tap **Approve** button into Telegram. The AI does 100% of the reading, ranking, pricing, and translating in seconds; the human does a single <5-second tap for the one irreversible, quota-consuming action (submitting the quotation on the portal). Result: you are effectively first-to-see (email arrives at publish time with no human present) and first-to-respond (the quote is already written when you tap), manual effort is near zero, and the paid account — the crown jewel — is never driven by a bot, so it stays alive. Every new capture channel plugs into the `RawLead → create_lead → run_matching → create_quote → Telegram` chain you already built; nothing downstream changes.

## 2) How Leads Get In — Capture Channels Ranked

| Rank | Channel | Latency | Why it dodges the block |
|---|---|---|---|
| **1 — PRIMARY** | **go4world lead-alert email → `EmailLeadSource` (Gmail API, read-only)** | Seconds to a few minutes from publish (**must be measured — see Limits**) | It is a **push the vendor is eager to send**, not a fetch. Zero origin requests, zero login, zero fingerprint. Read-only OAuth scope literally cannot post or throttle the account. |
| **2 — PARALLEL** | **SPA OCDS API** (Georgian State Procurement, `odapi.spa.ge`, OCDS JSON) | Poll cadence (minutes) | Public government API, legal, machine-readable, **no go4world competitor watches it**, and it's where Georgian buyer demand actually originates. No account risk at all. |
| 2b — PARALLEL | **Marketplace email alerts** (TradeKey / TradeWheel / EC21 / ExportHub / ExportersIndia) — one generic `EmailAlertSource`, one label per marketplace | Push (minutes) | Same email-push logic as #1. Diversifies away from single-vendor dependence. All safe, no scraping. |
| 3 — FALLBACK | **Tampermonkey / MV3 userscript** in the user's real logged-in Chrome; `MutationObserver` reads already-rendered leads → POSTs `/api/leads/raw` | Instant on render, bounded by human refresh cadence | Runs in the genuine session, reads only DOM the human already loaded. **Not always-on** (browser must stay open = operational SPOF) and any refresh timer reintroduces request risk. Belt-and-suspenders only. |
| 4 — MANUAL ONLY | Playwright `connectOverCDP` deep-detail pull against a copied `--user-data-dir` | On demand | Detectable + re-fetches pages; use as a manual, on-demand detail tool, **never in a loop**. |
| **5 — DO NOT BUILD** | Stealth headless + residential proxy | — | Re-logs the paid account from an anomalous IP — the **textbook ban trigger**. This is exactly what already got you blocked down to the login page. Delete it from the roadmap. |

**Second inbound email — don't conflate it:** after you submit a quotation (spends ~1 of ~100/day quota), go4world emails the **buyer's phone + email**. `EmailLeadSource` parses this too, dedups it to the *same* lead via `(source, external_id)`, and **enriches** the existing record (then Clay MCP sharpens the follow-up). Lead-alert email = the speed signal; contact-details email = the enrichment.

**The highest-leverage single action isn't code — it's keyword tuning.** Matching is driven off your paid profile keywords (bricks/HS6904, ceramic tiles/HS6907, Iran origin, Georgia/Turkey destination). Wrong or narrow keywords mean the right leads are never emailed and no parser can recover them. Tune the profile first.

## 3) The AI Agent — Parse → Prioritize → Draft → One-Tap

A 4-stage Claude pipeline hung off `run_matching()`. **Pin exact model IDs via the `claude-api` skill at build time.** Every buy-lead body is treated as **untrusted data** (prompt-injection defense: lead text goes only in the user turn, never near system authority; numbers come from the catalog, never the model).

1. **PARSE** (Haiku) — free text → structured fields via strict JSON schema, but product/qty/unit/incoterm/dest/specs are **nullable** (a null quantity is correct signal that the buyer didn't specify; a hallucinated quantity is a costly quote error). Per-field confidence; low-confidence leads escalate to Sonnet.
2. **PRIORITIZE** — (a) deterministic prefilter in code: HS6 (6904/6907) + keyword match against catalog `Product` rows + a hard feasibility gate (can we source it? can we deliver Iran→Georgia→Turkey?); (b) LLM judge (Haiku, or Sonnet when judgment matters) → **0–100 priority score**. This decides which leads spend the scarce ~100/day quota first — the load-bearing economic decision.
3. **DRAFT multilingual quote + outreach** — **numbers are never from the model.** The price / MOQ / origin=Iran / HS / cert header is rendered programmatically from `create_quote()`'s frozen `Decimal` two-price (EXW + delivered-to-Georgia); the model writes only the prose body. Canonical English (Sonnet; Opus for high-value leads), then Russian (safe) and **Georgian with a mandatory back-translation entity-check** (Haiku re-translates Georgian→English and compares product/HS/price/MOQ/incoterm to source; any mismatch → human review). A translation bug that corrupts a price is a real financial commitment.
4. **RESPOND — one-tap Approve-and-Send (the default policy).** AI pushes the top-priority ready quote to Telegram with inline **Approve / Edit / Skip** buttons; the human taps in <5s and performs the actual portal submit.

**Auto-send policy:** Draft-only is rejected (too slow, wastes the edge). Capped auto-send is rejected as default (the send is irreversible *and* the only way to auto-send is to drive the blocked portal — the exact account risk we're avoiding). **One-tap approve-and-send is correct** — the first-responder edge is won in the seconds saved on prep, not in removing the final tap. True auto-send is reserved only for high-confidence + low-value + already-verified leads, and only ever through an official API if one is obtained — never a bot on the paid login.

**Guardrails (mirroring the trading system's non-bypassable chain):** schema validation + confidence gating at every stage; quote hard-rules before sendable (margin floor, price sanity vs. catalog, MOQ feasibility, dest-country whitelist GE/TR); **quota governor** capping ~100/day spent on descending priority; two-layer dedup (already built); prompt-injection isolation; Georgian entity-check; human-in-the-loop above any value/confidence threshold. **Cost levers:** prompt caching (catalog + system prompt as stable prefix ≈ 90% savings across ~100 leads/day); Batch API (50% off) for nightly re-scoring only, never live.

## 4) Phased Build

**Phase 0 — Profile + safety config (hours, no code).** Tune go4world profile keywords (HS6904/HS6907, Iran, Georgia/Turkey). Set Gmail filters → labels `g4w/lead-alert`, `g4w/contact`. Confirm `GO4WORLD_PORTAL_ENABLED=false`. Inspect a real lead-alert + contact-details email (lock down sender, subject, HTML). Post a test keyword and **time the first email**.
*Done when:* keywords live, labels routing correctly, and measured email latency is documented.

**Phase 1 — Email capture (the core edge).** Build `app/sources/email_go4world.py` (`EmailLeadSource`, Gmail read-only OAuth, token stored like `.go4world_session.json`); `parse_email_html()` mirroring the existing `parse_leads_html()` pattern with defensive multi-selector + regex-on-URL fallback; contact-details emails dedup-enrich the same lead. Register it in `app/worker.py` `run_once()` next to `Go4WorldCsvSource` (IMAP/poll fallback works immediately). *Reuse:* `sources/base.py` (`RawLead`, `LeadSource`), `ingest.py` (`ingest_source`, dedup, `IngestionRun`), `lead_service.create_lead/run_matching`, `quote_service.create_quote`, `telegram.py`, `worker.py`.
*Done when:* a real lead-alert email produces a matched lead + auto-drafted quote + Telegram alert end-to-end, and a zero-lead run raises an alert (broken parser looks identical to a quiet day).

**Phase 2 — `/api/leads/raw` + userscript.** Add authed FastAPI route in `app/main.py` (`X-API-Key` constant-time compare, new `INGEST_API_KEY` in `config.py`, bind `127.0.0.1`), returning **`202` immediately** and running `create_lead` in a background task. Ship the Tampermonkey/MV3 userscript (`MutationObserver` → POST), `web_accessible_resources: []`, passive reads only. *Reuse:* the entire `create_lead → run_matching` core (no new dedup — the chatty observer's replays are absorbed by the two-layer idempotency).
*Done when:* a lead rendered in the real browser tab arrives as a `RawLead` via the API with zero extra portal requests.

**Phase 3 — AI parse + score.** Insert Stage 1 (Haiku parse, nullable schema) at ingest and Stage 2 (deterministic prefilter extending `matching.score_lead_product` + LLM judge) into/around `run_matching`. Add prompt caching. *Reuse:* `run_matching` sort, catalog `Product` rows, `DEFAULT_PARAMS`.
*Done when:* leads arrive clean, structured, and priority-ranked 0–100.

**Phase 4 — AI draft + Telegram one-tap (the "minimal manual, <5-second send" milestone).** Stage 3 drafting (programmatic header from `create_quote`, model prose; EN→RU→KA with Georgian back-translation check). Upgrade `app/telegram.py` to inline Approve/Edit/Skip + `POST /api/telegram/callback` flipping `Quote.status` draft→approved and calling the existing `/quotes/{id}/send`. Add quote hard-rules gate + quota governor (daily counter table). *Reuse:* `notify_quote_ready`, `Quote.status` flow, existing `/quotes/{id}/approve` + `/quotes/{id}/send` routes.
*Done when:* a matched lead becomes an approved, quota-tracked, sent quote from a single Telegram tap.

**Phase 5 — Diversify sources.** `SpaOcdsSource` (verify live `odapi.spa.ge` endpoint + OpenAPI spec first — TLS cert was expired last fetch; also register a free `tenders.procurement.gov.ge` account for redundant alerts) and generic `EmailAlertSource` (TradeKey/TradeWheel/EC21/etc. via Gmail labels). Optional IndiaMART CRM Push webhook → `/api/webhooks/indiamart` reusing the `/api/leads/raw` core.
*Done when:* two independent non-go4world channels feed the same pipeline.

**Phase 6 — Hardening.** Gmail `watch` → Cloud Pub/Sub → `POST /api/gmail/push` (sub-minute, event-driven, replaces polling); Clay enrichment on contact unlock; Batch-API nightly re-scoring; capped true-auto-send only if an official API appears.
*Done when:* capture is push-driven end-to-end and enrichment runs automatically post-unlock.

## 5) What We Need From the Founder + Account-Safety Notes

**Decisions/inputs needed:**
- **Access to the paid account's Gmail** to inspect real lead-alert + contact-details emails (sender, subject, HTML) and to set up filters/labels + read-only OAuth. Everything downstream depends on this.
- **Confirm the profile keywords** to target (HS6904 bricks, HS6907 tiles, Iran origin, Georgia/Turkey destination) and any others — this controls both recall and speed.
- **Value threshold** above which a lead always requires human review (vs. one-tap), and the **margin floor** for the quote hard-rules gate.
- **Georgian-language reviewer** (or acceptance that any entity-check mismatch parks the lead for manual review).
- **Confirm the ~100/day quota number** and how it resets, so the quota governor is accurate.
- Sign-off that the userscript/email-parsing "no automated access" ToS clause is an **accepted managed risk** (private tooling, human does the send).
- Whether to stand up Cloud Pub/Sub (Phase 6) or stay on the zero-infra poll.

**Account-safety (structural, not disciplinary):**
- The AI **never** drives the paid account's browser. The one irreversible, quota-consuming action is a human tap or an official API.
- **Zero extra portal requests** — primary capture is read-only Gmail (can't post, can't throttle); userscript reads only already-rendered DOM in the real session.
- `GO4WORLD_PORTAL_ENABLED` **stays `false`** — the Playwright scraper remains off-by-default, manual-only, session-reuse, self-halting on "Too many requests."
- **No headless, no CDP loop, no proxy, no re-login.** Stealth-headless-over-proxy is deleted from the roadmap — it's what caused the original block.
- Quota governor caps sends and protects against runaway automation; Georgian output never auto-sends without the entity-check.

## 6) Honest Limits

- **The whole thesis rests on one unverified number: email latency.** The edge assumes lead-alert emails arrive *per-lead in minutes*, not as a batched daily digest. This is MEDIUM confidence. **Mandatory first step before building the AI layer:** post a test RFQ and time the first email. If it's a daily digest, the first-responder claim for this channel collapses and SPA OCDS + marketplace alerts become primary.
- **Quota, not capture, is the real bottleneck.** Capturing 400+ leads/day is worthless if you can only respond to ~100, and responding is the *only* way to unlock buyer contact. The AI's job is ruthless ranking to spend scarce quota on best-fit bricks/tiles leads — not responding to everything.
- **Gmail parser is brittle.** You're coupled to go4world's email HTML; a template change silently breaks extraction and looks identical to a quiet day. Mitigated by defensive parsing + zero-lead alerts, not eliminated.
- **ToS "no automated access"** applies to all options including parsing your own inbox. Low exposure (private, reading your own mail, human sends) but non-zero — managed, not solved.
- **Multilingual risk is financial.** Georgian is a documented LLM hallucination hotspot; the back-translation check reduces but does not fully remove the risk of a corrupted price/MOQ reaching a buyer — hence numbers are injected as fixed tokens, never model-generated.
- **SPA OCDS endpoint was unhealthy on last fetch** (expired TLS cert, stale mirror) — must be re-verified live before Phase 5.
- **Userscript is an operational SPOF** — it only works while a browser stays open on a machine that never sleeps; it is a fallback, never the 24/7 spine.
- **No safe path to true auto-send exists today** — the only way to auto-submit is to drive the blocked portal. Until an official API appears, the human tap is a permanent feature, not a temporary limitation.