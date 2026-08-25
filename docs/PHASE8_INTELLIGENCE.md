# Phase 8 — Admin Intelligence: Dashboard, Metrics, Demand & Opportunities

An admin-only intelligence layer over the operational data, additive and **read-mostly** (a GET never mutates
business data). It answers "what's happening / which products & markets have **real** demand / which
opportunities to pursue / which sources are fresh" — built strictly on **deterministic positive evidence**,
never conflating a scraped lead or a negative reply with demand. The seller dashboard is unchanged; buyer
identity/contact, internal pricing, provider details and Go4it margin never appear in a chart, label, export,
log or cache key. Header nav only; no sidebar.

## Deterministic evidence model (the spine)
- **Positive demand** ONLY from: `Lead.reply_outcome=="positive"` (admin-confirmed in the Inbox),
  `Lead.accepted_at`/`status=="won"`, an **accepted** `QuoteVersion` / `Quote.buyer_response=="accepted"`, and a
  `Deal`. `demand.from_positive_reply` **refuses** a negative/auto/none reply and a scraped lead.
- **Never demand**: scraped prospects, email opens (untracked), `Outreach.status=="sent"` "delivered", bounces,
  `reply_outcome in (negative, auto_reply)`, directory membership, stale tenders.
- **Funnel** from real states, excluding `inferred` rows; a negative reply is an *engaged* record but never
  enters a positive-interest stage.
- **Freshness** from `Provenance.collected_at/last_seen_at` and `IngestionRun.finished_at`.

## Checkpoint A — metrics, provenance, dashboard, analytics
- **`metrics.py`** — ONE registry (24 metrics). Each carries an exact definition/formula/included/excluded/unit/
  min_sample and a live `compute()`. **No ambiguous "buyers" label** (a prospect is a prospect). Money is
  returned **per currency** and never summed across currencies. KPI cards show the definition on hover.
- **`provenance_view.py`** — Observed / Verified / Derived / Inferred classification + freshness. Inferred
  history is never shown as verified.
- **`data_sources.py`** — source health from `IngestionRun`+`Provenance`: last success/failure, records/
  duplicates/rejected, and a freshness label (Current / Aging / Stale / Failed / **Not configured** / Unknown).
  Data is never "Current" unless it was actually refreshed inside its window.
- **`analytics.py`** — the commercial funnel (Research prospect → … → Settled) with per-stage conversion, a
  documented cohort/message basis, insufficient-data states, and negatives excluded from positive interest;
  per-outcome replies; deals/shipments/WQ aggregates; and an **immutable `AnalyticsSnapshot` cache** (tenant-
  scoped keys with no sensitive identifiers, TTL, and a **stale fallback** when live compute fails).
- **`charts.py`** — server-side chart geometry (bars / funnel / sparkline). **No CDN, no client JS**; every
  chart has accessible labels + a table fallback.
- **Dashboard** — the admin branch gains the metric-registry KPI row (definition tooltips) + the commercial
  funnel; the **trader branch is untouched**. New Intelligence nav children **Overview / Demand / Opportunities /
  Performance / Reports / Data Sources**; Research / Command / Markets preserved. **Performance** rewards quality
  (accepted quotes, delivered deals, resolved exceptions, completed work, response time) — **never raw email/
  lead volume — with min-sample gating (unranked otherwise).

## Checkpoint B — demand, opportunities, scoring, alerts, reports
- **`demand.py`** — `DemandSignal` service. Signals only from deterministic evidence; `dedup_key` (partial-
  unique) counts one underlying event once even across surfaces; documented counting methods (unique event /
  buyer / requirement / company / product-market). **Seasonality** requires ≥ 3 comparable periods else
  "Insufficient history" (no single-season claims).
- **`opportunity_scoring.py`** — deterministic, **versioned** scoring with a fully visible breakdown (**no
  hidden weights**). Weights are env-configurable (`config.OPP_SCORE_WEIGHTS`) and the `score_version` is
  derived from them, so a weight change yields a new version and **never rewrites a historical snapshot**.
  Insufficient evidence lowers confidence + applies a missing-data penalty; nothing is AI-generated; never based
  on lead volume.
- **`opportunities.py`** — workflow (New → Needs Research → Needs Supply → Ready for Review → Approved →
  Monitoring → Pursuing → Converted → Rejected → Expired → Archived, validated transitions). **Supply matching**
  over canonical Products: every match is **explained**, its **missing requirements** shown, capability is never
  invented, buyer identity is never exposed, and nothing auto-contacts a buyer or seller; missing supply raises
  a `high_demand_no_supply` Work Queue action.
- **`alerts.py`** — admin-only in-app alerts, **idempotent** on `(alert_key, condition_version)`; a material
  change may create a fresh one. **Timezone-aware** cadence scheduling (08:00 local per `schedule_tz`; stored
  timestamps stay naive UTC). Snooze/dismiss/review. **No automatic external email or outreach.**
- **`reports.py`** — internal CSV + **sandboxed-PDF** reports (`pdf_render` hardened sandbox, prod-Chromium
  gate), **private** storage + sha256, metric definitions + source-freshness appendix, **currencies separated**,
  **aggregate-only (no PII)**, audited generate/download, **no auto emailing**. PDF degrades to a failed row +
  a Work Queue task without Chromium (never crashes).

## Work Queue (§22)
11 new condition-versioned types (`opportunity_needs_review`, `high_demand_no_supply`, `source_stale_failed`,
`demand_signal_ambiguous/duplicate`, `data_quality_anomaly`, `report_generation_failed`, `scheduled_report_
review`, `seasonal_preparation_due`, …) + bounded scanners. `sync_demand_signals` is the live demand-generation
pass (accepted quotes / Deals / confirmed positive replies → signals + opportunities), idempotent + bounded by
record count and wall-clock.

## Migration & backfill (run order)
```
backup_db.py → migrate.py → migrate_gate_p8.py [--dry-run] → backfill_intelligence.py [--dry-run]
```
`migrate_gate_p8.py` idempotently creates the 7 tables + partial-unique indexes + the additive WorkItem columns,
runs `PRAGMA integrity_check`, and asserts operational counts (leads/quotes/deals/requests/outreach/products)
invariant. `backfill_intelligence.py` is **conservative**: demand only from deterministic evidence (accepted
quotes/Deals → strong derived; positive replies → inferred with provenance), everything marked `inferred`, never
inventing quantities/market size/seasonality, timestamps preserved; ambiguous → one aggregate review task.
`--rollback` (pre-go-live) removes inferred/backfill-only rows; `--recover` (post-go-live) preserves every real
admin decision, alert and report.

**Proven on a WAL-safe dev-DB snapshot:** gate dry-run→apply→re-run idempotent; backfill dry-run→apply→re-run→
rollback created 2 signals + 1 opportunity from the real accepted quote/Deal; operational counts invariant
(leads 4581 / quotes 56 / deals 1 / requests 1 / outreach 197 / products 96); app boots.

## Intelligence canary (`scripts/intel_canary.py`, also `tests/test_intel_canary.py`)
On a disposable DB, no external service: an accepted quote → a **strong deduped** demand signal (a negative
reply produces **none**) → an Opportunity with an **explained** supply match and a **transparent versioned
score** (with a visible missing-data penalty) → an **idempotent** alert → a CSV report (**per-currency, no
PII**, audited) → an authenticated second seller is refused **every** intelligence surface (403/404) and no
buyer PII appears on the admin analytics pages.

## Security & confidentiality
All Phase-8 pages/actions/exports are **admin-only** (`is_admin` → `_forbidden`); cross-tenant → `_not_found`.
Sellers never reach demand-signal buyer identity, opportunities, source evidence, team performance, supplier
scoring, margins, source-reliability notes, reports, snapshots or alerts. No sensitive identifier enters a chart
label, URL, log or cache key. Report generation/download require server-side authorization and are audited.

## Deferred / production-config (NOT production-ready — gates unchanged)
Conversational-AI Command upgrades → **Phase 9**. Report PDFs need production Chromium (`pdf_smoke.py`).
Background metric recomputation runs via the existing worker/scanner (no new daemon). The platform stays **not
production-ready** until the Phase-4 email canary, `pdf_smoke.py` in the prod container, the Phase-5
Product→Catalog staging canary, and **real malware-scanner integration** pass — none block safe local Phase 8
work.
