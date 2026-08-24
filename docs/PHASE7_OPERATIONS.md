# Phase 7 — Operations: Freight, Shipments, Documentation, Payments & Remittance

Admin-first operational execution on top of the accepted commercial **Deal**. Strictly additive: the Deal and
its existing `DEAL_STAGES` are unchanged and are now advanced from **verified** operational milestones by ONE
central projector. Sellers receive **sanitized** progress through their existing dashboard. The **Operations**
header workspace (no sidebar) holds **Overview · Freight · Shipments · Documentation · Payments & Remittance ·
Exceptions**; **Requests** stays the seller service entry point.

**Honesty (hard):** Go4it is **not** a licensed payment/remittance/customs/freight provider. Every record here
coordinates and tracks; it never claims a transfer/clearance/booking occurred without verified evidence, and
shows **"Provider integration not configured"** where no integration exists. No live/paid provider is called in
tests or local dev.

## Where each thing lives (the questions this phase answers)
- **Freight/shipping requests** → `FreightRequest` (+ provider `FreightOffer`s), workspace `/operations/freight`.
- **Shipment records** → `Shipment` (+ `ShipmentLeg`, `ShipmentEvent`), `/operations/shipments`.
- **Delivery records** → `DeliveryConfirmation` (evidence-gated; never an ETA), on the shipment detail.
- **Documentation** → `DocumentRequirement` + private `TradeDocument`, `/operations/documentation`.
- **Remittance/exchange** → `RemittanceCase`, `/operations/payments`.
- **Seller progress without buyer/provider contacts** → sanitized projections + allowlisted automatic
  `SellerUpdate`s (see Confidentiality). Shown on the deal page and the seller's Requests view.
- **Operational events → the Deal journey** → the central `operations.project_deal_stage` projector.

## Data model (additive — all money stored as TEXT, handled only via `pricing._d/_q` Decimal)
New tables: **OperationCase, FreightRequest, FreightOffer, Shipment, ShipmentLeg, ShipmentEvent,
DocumentRequirement, TradeDocument, CustomsCase, DeliveryConfirmation, OperationalException, PaymentMilestone,
RemittanceCase, Settlement, SettlementAdjustment**. Additive `WorkItem` linkage columns
(`related_operation_case_id/_shipment_id/_payment_id/_exception_id`). Partial-unique indexes
(`app/db.py::_ensure_operations_indexes`): idempotent external-event ingest `(source, external_event_id)`,
unique OperationCase reference, and one Deal-primary case per Deal. An `OperationCase` may link a Deal, a
ServiceRequest, both, or be a controlled standalone case — a Deal may have **many** shipments/cases.

## Checkpoint A — operations, freight, shipments, documents
- **Central Deal-stage projector** (`operations.project_deal_stage`): the single writer that advances
  `DEAL_STAGES` from verified milestones (supplier confirmed → freight booked → export cleared → in transit →
  import cleared → delivered → settled). **Idempotent, monotonic (never backward), doc-gated** (reuses
  `deal_service.missing_docs_for` — the non-bypassable compliance gate). Admin override requires a reason +
  audit; an erroneous advance is fixed by a controlled `correct_deal_stage` event (never by deleting history).
- **Freight** (`freight.py`): critical physical facts (weight, CBM, hazardous, customs, mode, origin/dest) are
  **never guessed** — a missing one raises `freight_request_incomplete`. Offer money is Decimal; expired offers
  are never silently selected; selecting/replacing an offer is audited (controlled replacement event).
- **Shipments** (`shipments.py`): leg-order validation; idempotent external event ingest; forward-only,
  leg-order-safe milestone (a late/out-of-order event never regresses the shipment); delivery only via a
  controlled `DeliveryConfirmation` (a passed ETA never auto-completes); damage/shortage/failed → an
  `OperationalException`, never a silent completion. Carrier/booking/container/tracking refs are admin-only.
- **Customs** (`customs.py`): explicit status transitions — clearance is **never inferred** from movement.
- **Documents** (`tradedocs.py`): the hardened private-storage pattern (validate via
  `attachments._secure_validate`, generated on-disk name, sha256, quarantine-by-default, archive-not-delete,
  admin-download-gated, path-traversal guard). Seller uploads land on their own request, quarantined, never
  auto buyer-facing. An admin document reaches a seller only after it is marked seller-safe and published via
  the owner-scoped `RequestDeliverable`. Missing docs are requested from the seller through the sanitized
  `SellerUpdate` flow (PII in instructions is refused).
- **Providers**: freight/customs/remittance providers are canonical Trade Network `Company` rows with the new
  additive roles `freight_provider` / `customs_broker` / `remittance_provider` — no separate provider DB.

## Checkpoint B — payments, remittance, exceptions, seller progress, analytics
- **Payments** (`payments.py`): `PaymentMilestone` (Decimal). Confirmation requires an **authorized admin
  (manager+) AND a reference or evidence document** — never on an email/screenshot alone. Corrections audited.
  No card numbers / banking passwords / crypto keys or seeds. Seller-facing references are masked.
- **Settlement** (`payments.py`): an **immutable per-currency snapshot** — a deal settles only once its required
  financial milestones are complete; no cross-currency sum without an FX snapshot; corrections append a
  `SettlementAdjustment`. Sellers never see Go4it margin or buyer payments.
- **Remittance** (`remittance.py`): a coordination/tracking record. Phase-5 FX snapshot (never presented as
  live when manual); sensitive account identifiers **encrypted at rest** (`outreach.mail_encrypt`); compliance
  reasons internal; **"Provider integration not configured"** honesty; no auto-initiated transfers.
- **Exceptions** (`ops_exceptions.py`): internal + separate seller-safe description; open-exception de-dup;
  Work Queue integration **without duplicate unresolved tasks**.
- **Seller-safe progress** (`seller_progress.py`): a sanitized deal projection (journey stage, completed/next
  milestone, safe timing, requested documents, sanitized exceptions, own authorized/masked payments) and
  **allowlisted, idempotent** automatic updates (one per deal+stage, attached to the seller's request). A
  **sensitive** exception creates a **draft** update requiring admin approval — never auto-published. No
  buyer/provider identity or contact, tracking credentials, internal offers/costs, Go4it margin, buyer payment
  detail, compliance notes, or other sellers' records ever reach a seller.
- **Analytics** (Overview): shipments by mode/milestone, avg transit **only where real dates exist** (never
  invented), exception rate, payments received/due **per currency** (never summed across currencies),
  delivered/settled counts. No invented provider ratings.
- **External integrations** (`ops_providers.py`): honest **"Not configured"** carrier/freight/customs/payment/
  remittance interfaces + HMAC webhook verification + replay protection. The `/ops/webhook/tracking` endpoint is
  signature-verified, replay-protected and rate-limited; with no `OPS_WEBHOOK_SECRET` configured it **fails
  closed** (401). No live/paid provider is ever called.

## Work Queue (condition-versioned; scanners bounded by record count AND a wall-clock deadline)
22 new operations types incl. `approved_request_needs_case, operation_missing_data, freight_request_incomplete,
freight_offer_needs_review, freight_offer_expiring, booking_confirmation_required, shipment_update_overdue,
tracking_stale, customs_document_missing, customs_hold, seller_document_required, uploaded_document_needs_review,
payment_due, payment_overdue, payment_confirmation_required, remittance_compliance_review, remittance_delayed,
delivery_confirmation_required, cargo_damage_shortage, settlement_review_required, operational_handoff_required,
external_integration_failure`. Six scanners (freight-expiring, tracking-stale, booking-confirmation,
delivery-confirmation, payment-overdue, settlement-review). Completing/dismissing an item never recreates it
until the condition materially changes.

## Migration & backfill (run order)
```
backup_db.py → migrate.py → migrate_gate_p7.py [--dry-run] → backfill_operations.py [--dry-run]
```
`migrate_gate_p7.py` idempotently creates the 15 tables + partial-unique indexes + the additive WorkItem
columns, runs `PRAGMA integrity_check`, and asserts operational counts (quotes/deals/requests/outreach/products)
invariant. `backfill_operations.py` is **conservative**: one inferred baseline `OperationCase` per existing Deal
(the deterministic Deal↔case relationship) — it **never** invents shipments, freight, payments, customs
clearance, delivery dates or remittance, and preserves legacy Deal stages exactly. `--rollback` (pre-go-live)
removes only inferred cases; `--recover` (post-go-live) removes only untouched inferred cases (no operational
references), preserving every real record.

**Proven on a snapshot of the dev DB:** gate dry-run→apply→re-run idempotent, backfill dry-run→apply→re-run→
rollback, operational counts invariant (quotes 56 / deals 1 / requests 1 / outreach 197 / products 96), app
boots.

## Deferred / production-config (NOT production-ready — gates unchanged)
- Carrier-tracking / freight-quote / customs / payment / remittance provider integrations = **"Not
  configured"** (env-config + encrypted-credential + HMAC-webhook-verify seams shipped; no live calls).
- Real malware scanning of uploaded documents is still an **admin-attestation** seam (`scan-clear`).
- Generated trade PDFs need production Chromium (`scripts/pdf_smoke.py`).
- The platform stays **not production-ready** until the Phase-4 live email canary, `pdf_smoke.py` inside the
  production container, and the Phase-5 Product→Catalog staging canary pass. These do **not** block safe local
  Phase 7 work.
