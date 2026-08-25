"""Phase 7 — Operations foundation: OperationCase lifecycle + the CENTRAL Deal-stage projector.

An OperationCase is the umbrella for a piece of execution work; it may link to a Deal, a ServiceRequest, both,
or stand alone. Creation is IDEMPOTENT (a Deal has at most one primary case; repeated clicks / worker retries
never duplicate — enforced by the partial-unique index uq_opcase_deal_primary).

The **projector** is the single writer that advances the existing, unchanged `deal_service.DEAL_STAGES` from
VERIFIED operational milestones. It is idempotent, MONOTONIC (never moves a deal backward), prerequisite-gated
(reuses `deal_service.missing_docs_for` — the non-bypassable compliance-doc gate), and never advances on an
estimate alone. Admin overrides require a reason + audit; erroneous advances are fixed by a controlled
correction event (`correct_deal_stage`) that preserves history — never by deleting rows or silent regression.
"""
from datetime import datetime

from sqlalchemy.exc import IntegrityError
from sqlmodel import select

from . import deal_service as DS
from .models import (CustomsCase, Deal, DeliveryConfirmation, OperationCase, PaymentMilestone, ServiceRequest,
                     Settlement, Shipment, ShipmentEvent)
from .pipeline import audit


def _ref(prefix: str, row_id: int, now=None) -> str:
    now = now or datetime.utcnow()
    return f"{prefix}-{now:%Y%m}-{row_id:04d}"


# --------------------------------------------------------------------- OperationCase creation (idempotent)
def ensure_case_for_deal(session, deal: Deal, *, actor=None, now=None):
    """Return (case, created). Exactly ONE primary case per Deal — concurrency-safe via the partial-unique
    index on operationcase(deal_id) WHERE case_type='deal'. A racing/duplicate call returns the existing case."""
    now = now or datetime.utcnow()
    existing = session.exec(
        select(OperationCase).where(OperationCase.deal_id == deal.id,
                                    OperationCase.case_type == "deal")).first()
    if existing:
        return existing, False
    case = OperationCase(case_type="deal", deal_id=deal.id, tenant_id=deal.owner_id,
                         owner_id=getattr(actor, "id", None), created_by=getattr(actor, "id", None),
                         status="open", created_at=now, updated_at=now)
    session.add(case)
    try:
        session.flush()
    except IntegrityError:
        session.rollback()
        again = session.exec(
            select(OperationCase).where(OperationCase.deal_id == deal.id,
                                        OperationCase.case_type == "deal")).first()
        return again, False
    case.reference = _ref("OP", case.id, now)
    session.add(case)
    audit(session, actor, "operation_case", case.id, "case_created",
          {"source": "deal", "deal_id": deal.id}, tenant_id=deal.owner_id)
    return case, True


def ensure_case_for_request(session, req: ServiceRequest, *, actor=None, deal_id=None, now=None):
    """Return (case, created) for an approved ServiceRequest. Idempotent per request (one request-primary case).
    Callers must have validated required data first — this never manufactures missing operational facts."""
    now = now or datetime.utcnow()
    existing = session.exec(
        select(OperationCase).where(OperationCase.request_id == req.id,
                                    OperationCase.case_type == "request")).first()
    if existing:
        return existing, False
    case = OperationCase(case_type="request", request_id=req.id, deal_id=deal_id, tenant_id=req.owner_id,
                         owner_id=getattr(actor, "id", None), created_by=getattr(actor, "id", None),
                         status="open", category=req.request_type or "", created_at=now, updated_at=now)
    session.add(case)
    session.flush()
    case.reference = _ref("OP", case.id, now)
    session.add(case)
    audit(session, actor, "operation_case", case.id, "case_created",
          {"source": "request", "request_id": req.id, "deal_id": deal_id}, tenant_id=req.owner_id)
    return case, True


def route_service_request(session, req, *, actor=None, now=None):
    """Idempotently scaffold the correct SPECIALIZED record for an approved ServiceRequest, WITHOUT minting any
    premature binding/operational record — never a PaymentMilestone, never a Shipment. It calls the EXISTING
    domain constructors (an adapter, not a reimplementation) and is safe to call repeatedly:

        remittance → OperationCase(category='remittance') + RemittanceCase(status='requested')
        freight    → OperationCase(category='freight')    + FreightRequest(status='draft')
        docs       → OperationCase(category='docs')        (DocumentRequirements are added later by the admin)
        contract   → a linked, non-binding draft Contract (needs_review; no assumed parties) — Commercial, not Ops
        buyer_hunt → handled by the existing Research pipeline (via the start_research adapter), not here

    Payment milestones (remittance) and shipments (freight) are created only after their real prerequisites are
    confirmed — amount/currency/payer/payee/due/compliance for a payment; a selected offer + booking for a
    shipment. Returns a dict of the records that were ensured."""
    now = now or datetime.utcnow()
    rtype = (getattr(req, "request_type", "") or "").strip()
    out = {"request_type": rtype}
    if rtype in ("remittance", "freight", "docs"):
        case, _created = ensure_case_for_request(session, req, actor=actor, now=now)
        out["operation_case"] = case
    if rtype == "remittance":
        from . import remittance as _REMIT
        rc, _ = _REMIT.ensure_case_for_request(session, req, case=out["operation_case"], actor=actor, now=now)
        out["remittance_case"] = rc
    elif rtype == "freight":
        from . import freight as _FREIGHT
        fr, _ = _FREIGHT.ensure_request_for_request(session, req, case=out["operation_case"], actor=actor,
                                                    now=now)
        out["freight_request"] = fr
    elif rtype == "contract":
        from . import contract_service as _CONTRACT
        c, _ = _CONTRACT.ensure_draft_for_request(session, req, actor=actor)
        out["contract"] = c
    return out


def create_standalone_case(session, *, tenant_id=None, actor=None, category="", origin_country="",
                           dest_country="", notes="", now=None):
    """A controlled standalone service case (no Deal/Request). Admin-created only."""
    now = now or datetime.utcnow()
    case = OperationCase(case_type="standalone", tenant_id=tenant_id, owner_id=getattr(actor, "id", None),
                         created_by=getattr(actor, "id", None), status="open", category=category,
                         origin_country=origin_country, dest_country=dest_country, notes=notes,
                         created_at=now, updated_at=now)
    session.add(case)
    session.flush()
    case.reference = _ref("OP", case.id, now)
    session.add(case)
    audit(session, actor, "operation_case", case.id, "case_created", {"source": "standalone"}, tenant_id=tenant_id)
    return case


# --------------------------------------------------------------------- milestone evidence (VERIFIED only)
def deal_milestone_signals(session, deal: Deal) -> dict:
    """Compute the per-stage VERIFIED evidence for a Deal from concrete operational records. Later evidence
    implies earlier stages (a delivered shipment was necessarily booked) so the projector never gets stuck on a
    skipped intermediate. Nothing here is inferred from an estimate — only recorded/confirmed facts."""
    shipments = session.exec(select(Shipment).where(Shipment.deal_id == deal.id)).all()
    sh_ids = [s.id for s in shipments]
    pays = session.exec(select(PaymentMilestone).where(PaymentMilestone.deal_id == deal.id)).all()
    customs = session.exec(select(CustomsCase).where(CustomsCase.deal_id == deal.id)).all()
    delivered = False
    if sh_ids:
        dcs = session.exec(select(DeliveryConfirmation).where(DeliveryConfirmation.shipment_id.in_(sh_ids))).all()
        delivered = any((not dc.failed) for dc in dcs)
    settled = session.exec(select(Settlement).where(Settlement.deal_id == deal.id)).first() is not None

    def _received(kinds):
        return any(p.milestone_type in kinds and p.status in ("received", "partially_received") for p in pays)

    export_cleared = any(c.side == "export" and c.status == "cleared" for c in customs)
    import_cleared = any(c.side == "import" and c.status == "cleared" for c in customs)
    freight_booked = any(s.booking_reference or s.current_milestone in
                         ("booked", "export_cleared", "in_transit", "import_cleared", "delivered")
                         for s in shipments)
    in_transit = any(s.actual_departure is not None or
                     s.current_milestone in ("in_transit", "import_cleared", "delivered") for s in shipments)
    if not in_transit and sh_ids:
        ev = session.exec(select(ShipmentEvent).where(ShipmentEvent.shipment_id.in_(sh_ids),
                                                      ShipmentEvent.event_type == "departed")).first()
        in_transit = ev is not None
    supplier_confirmed = _received(("supplier_advance", "supplier_balance")) or bool(shipments)
    payment_received = _received(("buyer_deposit", "buyer_balance"))

    sig = {
        "supplier_confirmed": supplier_confirmed,
        "payment_received": payment_received,
        "freight_booked": freight_booked,
        "export_cleared": export_cleared,
        "in_transit": in_transit,
        "import_cleared": import_cleared,
        "delivered": delivered,
        "settled": settled,
    }
    # cumulative implication: later evidence backfills earlier milestones so a monotonic walk never stalls
    order = ["supplier_confirmed", "payment_received", "freight_booked", "export_cleared",
             "in_transit", "import_cleared", "delivered", "settled"]
    seen_later = False
    for name in reversed(order):
        if sig[name]:
            seen_later = True
        elif seen_later:
            sig[name] = True
    return sig


def project_deal_stage(session, deal: Deal, *, actor=None, now=None):
    """Advance `deal.stage` forward through DEAL_STAGES to the furthest stage the VERIFIED evidence supports.
    Idempotent, monotonic (never backward), and doc-gated: entering export_cleared/in_transit/import_cleared is
    blocked unless the REQUIRED_DOCS are verified (a blocked hop stops advancement + raises a Work Queue item).
    Returns (final_stage, [hops]). Adds rows only — the caller commits."""
    now = now or datetime.utcnow()
    signals = deal_milestone_signals(session, deal)
    hops = []
    guard = 0
    while guard < len(DS.DEAL_STAGES):
        guard += 1
        nxt = DS.next_stage(deal.stage)
        if nxt is None or nxt in ("settled", "closed"):
            break                      # settlement/close are explicit financial/admin actions, not projected
        if not signals.get(nxt, False):
            break                      # no verified evidence for the next stage → stop
        missing = DS.missing_docs_for(session, deal, nxt)
        if missing:
            _gate_task(session, deal, nxt, missing)
            break                      # non-bypassable compliance gate: cannot advance until docs verified
        frm = deal.stage
        deal.stage = nxt
        deal.updated_at = now
        session.add(deal)
        audit(session, actor, "deal", deal.id, "deal_stage_projected",
              {"from": frm, "to": nxt, "source": "operations_projector"}, tenant_id=deal.owner_id)
        _emit_stage_update(session, deal, frm, nxt, actor)
        hops.append(nxt)
    return deal.stage, hops


def override_deal_stage(session, deal: Deal, to_stage: str, *, reason: str, actor=None, now=None):
    """Admin override — advance to a stage without full evidence. FORWARD-only (still monotonic) and REQUIRES a
    reason; writes a distinct audit event. Backward correction is `correct_deal_stage`, not this."""
    now = now or datetime.utcnow()
    if to_stage not in DS.DEAL_STAGES:
        return False, "unknown stage"
    if not (reason or "").strip():
        return False, "an override reason is required"
    if DS.DEAL_STAGES.index(to_stage) <= DS.DEAL_STAGES.index(deal.stage):
        return False, "override is forward-only (use a correction to move back)"
    frm = deal.stage
    deal.stage = to_stage
    deal.updated_at = now
    session.add(deal)
    audit(session, actor, "deal", deal.id, "deal_stage_override",
          {"from": frm, "to": to_stage, "reason": reason[:500]}, tenant_id=deal.owner_id)
    _emit_stage_update(session, deal, frm, to_stage, actor)
    return True, ""


def correct_deal_stage(session, deal: Deal, to_stage: str, *, reason: str, actor=None, now=None):
    """The ONLY path that may move a deal BACKWARD — a controlled correction of an erroneous advance. Requires a
    reason, writes a `deal_stage_correction` audit event, and never deletes prior history."""
    now = now or datetime.utcnow()
    if to_stage not in DS.DEAL_STAGES:
        return False, "unknown stage"
    if not (reason or "").strip():
        return False, "a correction reason is required"
    frm = deal.stage
    deal.stage = to_stage
    deal.updated_at = now
    session.add(deal)
    audit(session, actor, "deal", deal.id, "deal_stage_correction",
          {"from": frm, "to": to_stage, "reason": reason[:500]}, tenant_id=deal.owner_id)
    return True, ""


def _gate_task(session, deal, stage, missing):
    try:
        from . import work_queue as WQ
        WQ.create_work_item_safe(
            session, tenant_id=deal.owner_id, type="customs_document_missing", source="automatic",
            title=f"Deal {deal.tracking_code}: documents needed for {stage}",
            description=f"Cannot advance to {stage} — required documents not verified: {', '.join(missing)}.",
            related_deal_id=deal.id, idempotency_key=f"customs_document_missing:deal:{deal.id}:{stage}",
            condition_version=",".join(sorted(missing)))
    except Exception:  # noqa: BLE001
        pass


def _emit_stage_update(session, deal, frm, to, actor):
    """Seam for the Checkpoint-B automatic seller-safe progress update. No-op if seller_progress isn't present
    yet; never raises into the projector."""
    try:
        from . import seller_progress as SP
        SP.on_deal_stage_change(session, deal, frm, to, actor=actor)
    except Exception:  # noqa: BLE001
        pass


# --------------------------------------------------------------------- seller-safe case projection
def case_seller_view(case: OperationCase) -> dict:
    """The ONLY OperationCase fields a seller may see: reference, high-level status, safe geography. Never the
    admin owner, internal notes, buyer/provider identity, or costs."""
    return {"reference": case.reference, "status": case.status,
            "origin_country": case.origin_country, "dest_country": case.dest_country,
            "category": case.category}
