"""Phase 7 — Freight requests + provider offers.

A FreightRequest captures the physical shipping need. Critical physical facts (weight, CBM, hazardous status,
customs requirement, mode, origin/destination) are NEVER guessed — a missing one raises a Work Queue action.

A FreightOffer is a provider's priced snapshot. The provider is a canonical Trade Network Company with a
`freight_provider` role (never a disconnected provider DB). Money is Decimal (via pricing._d/_q); expired
offers are never silently selected; selecting or replacing an offer is audited. Provider identity/contact and
internal costs are ADMIN-ONLY — a seller sees only mode / route summary / estimated timing / status.
"""
from datetime import datetime

from sqlmodel import select

from .models import FreightOffer, FreightRequest, OperationCase
from .pipeline import audit
from .pricing import _d, _q

# critical physical facts that must be known before a freight request can be quoted — never guessed
CRITICAL_FIELDS = ("mode", "origin_country", "dest_country", "gross_weight_kg", "volume_cbm",
                   "hazardous", "customs_required")


def critical_missing(fr: FreightRequest) -> list:
    """Critical fields that are still unknown (None/blank). hazardous/customs_required are booleans where
    None = genuinely unknown (NOT False) — so an unanswered hazard question is surfaced, never assumed safe."""
    missing = []
    for f in CRITICAL_FIELDS:
        v = getattr(fr, f)
        if v is None or (isinstance(v, str) and not v.strip()):
            missing.append(f)
    return missing


def _ref(row_id, now=None):
    now = now or datetime.utcnow()
    return f"FR-{now:%Y%m}-{row_id:04d}"


def create_freight_request(session, *, case: OperationCase = None, actor=None, now=None, **fields):
    """Create a FreightRequest (optionally under a case/deal). Returns (fr, missing_critical). If critical
    fields are missing it still creates the draft but raises a `freight_request_incomplete` Work Queue action —
    it never fabricates the missing facts."""
    now = now or datetime.utcnow()
    fr = FreightRequest(created_by=getattr(actor, "id", None), created_at=now, updated_at=now, **fields)
    if case is not None:
        fr.operation_case_id = case.id
        fr.deal_id = fr.deal_id or case.deal_id
        fr.tenant_id = fr.tenant_id or case.tenant_id
    session.add(fr)
    session.flush()
    fr.reference = _ref(fr.id, now)
    session.add(fr)
    audit(session, actor, "freight_request", fr.id, "freight_request_created",
          {"case_id": fr.operation_case_id, "deal_id": fr.deal_id}, tenant_id=fr.tenant_id)
    missing = critical_missing(fr)
    if missing:
        _incomplete_task(session, fr, missing)
    return fr, missing


def _incomplete_task(session, fr, missing):
    try:
        from . import work_queue as WQ
        WQ.create_work_item_safe(
            session, tenant_id=fr.tenant_id, type="freight_request_incomplete", source="automatic",
            title=f"Freight request {fr.reference}: missing critical details",
            description=f"Cannot quote — unknown: {', '.join(missing)}. These must be confirmed, never assumed.",
            related_operation_case_id=fr.operation_case_id, related_deal_id=fr.deal_id,
            idempotency_key=f"freight_request_incomplete:fr:{fr.id}", condition_version=",".join(sorted(missing)))
    except Exception:  # noqa: BLE001
        pass


def add_offer(session, fr: FreightRequest, *, actor=None, now=None, **fields):
    """Record a provider offer against a freight request. Money strings are normalised to 2dp Decimal text and
    the total is recomputed from the components (never trusted blindly)."""
    now = now or datetime.utcnow()
    offer = FreightOffer(freight_request_id=fr.id, created_by=getattr(actor, "id", None), created_at=now,
                         **fields)
    total = _q(_d(offer.base_freight) + _d(offer.surcharges) + _d(offer.insurance_cost) + _d(offer.customs_cost))
    offer.base_freight = str(_q(offer.base_freight))
    offer.surcharges = str(_q(offer.surcharges))
    offer.insurance_cost = str(_q(offer.insurance_cost))
    offer.customs_cost = str(_q(offer.customs_cost))
    offer.total = str(total)
    session.add(offer)
    session.flush()
    if fr.status == "draft":
        fr.status = "quoting"
        session.add(fr)
    audit(session, actor, "freight_offer", offer.id, "freight_offer_added",
          {"freight_request_id": fr.id, "provider_company_id": offer.provider_company_id,
           "total": offer.total, "currency": offer.currency}, tenant_id=fr.tenant_id)
    return offer


def offer_expired(offer: FreightOffer, now=None) -> bool:
    now = now or datetime.utcnow()
    return bool(offer.valid_until and now > offer.valid_until)


def select_offer(session, offer: FreightOffer, *, actor=None, reason="", now=None):
    """Select an offer for its freight request. Returns (ok, error). An EXPIRED offer is never silently
    selected. If another offer is already selected, this is a CONTROLLED REPLACEMENT (old → 'replaced' with a
    reason + link), fully audited."""
    now = now or datetime.utcnow()
    if offer.selection_status == "selected":
        return True, ""                                   # idempotent
    if offer_expired(offer, now):
        return False, "offer has expired — request a fresh quote (expired offers are never used)"
    fr = session.get(FreightRequest, offer.freight_request_id)
    prior = session.exec(select(FreightOffer).where(
        FreightOffer.freight_request_id == offer.freight_request_id,
        FreightOffer.selection_status == "selected")).all()
    for p in prior:
        if p.id == offer.id:
            continue
        p.selection_status = "replaced"
        p.replaced_by_id = offer.id
        p.replacement_reason = (reason or "replaced by a newer selection")[:300]
        session.add(p)
        audit(session, actor, "freight_offer", p.id, "freight_offer_replaced",
              {"replaced_by": offer.id, "reason": p.replacement_reason},
              tenant_id=fr.tenant_id if fr else None)
    offer.selection_status = "selected"
    offer.selected_by = getattr(actor, "id", None)
    offer.selected_at = now
    session.add(offer)
    if fr:
        fr.status = "offer_selected"
        fr.updated_at = now
        session.add(fr)
    audit(session, actor, "freight_offer", offer.id, "freight_offer_selected",
          {"freight_request_id": offer.freight_request_id, "total": offer.total, "currency": offer.currency},
          tenant_id=fr.tenant_id if fr else None)
    return True, ""


# --------------------------------------------------------------------- seller-safe projections
def offer_seller_view(offer: FreightOffer) -> dict:
    """The ONLY freight-offer fields a seller may see — never provider identity/contact or any cost/total."""
    timing = ""
    if offer.lead_time_days:
        timing = f"~{offer.lead_time_days} days"
    return {"mode": offer.mode, "route": offer.route_summary, "timing": timing,
            "status": offer.selection_status}


def freight_seller_view(fr: FreightRequest) -> dict:
    """Seller-safe freight-request summary: mode + route + status only (no addresses/ports leaking a buyer)."""
    return {"reference": fr.reference, "mode": fr.mode, "status": fr.status,
            "origin_country": fr.origin_country, "dest_country": fr.dest_country}
