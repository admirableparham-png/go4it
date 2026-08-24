"""Phase 7 — seller-safe operational progress + automatic sanitized updates.

The seller sees a sanitized view of their own deal's execution: journey stage, completed + next milestone, safe
timing, requested documents, seller-safe deliverables, sanitized exceptions, and their OWN authorized payment/
proceeds — and NOTHING else. Buyer identity/contact, freight-provider/customs-broker contacts, internal tracking
credentials, internal offers/costs, Go4it margin, buyer payment details, internal compliance notes and other
sellers' records are never projected.

Automatic progress updates fire only from allowlisted, fixed, PII-free templates (no free text, no raw provider
payload, no cost/margin), are idempotent per (deal, stage) so a milestone never notifies twice, and record their
source event. A SENSITIVE exception never auto-publishes — it creates a DRAFT update requiring admin approval.
"""
from datetime import datetime

from sqlmodel import select

from . import ops_exceptions as EXC
from . import payments as PAY
from . import shipments as SHIP
from .deal_service import DEAL_STAGES
from .models import (Deal, DocumentRequirement, Lead, OperationCase, OperationalException, PaymentMilestone,
                     SellerUpdate, Shipment)
from .pipeline import audit, sanitize_scan

# allowlisted seller-safe milestone → (public status label, fixed safe summary). NO dynamic buyer/provider/cost
# data ever enters these strings.
AUTO_TEMPLATES = {
    "supplier_confirmed": ("Supply confirmed", "Your supply for this order has been confirmed."),
    "payment_received": ("Payment received", "A payment milestone for this order has completed."),
    "freight_booked": ("Freight booked", "Freight has been booked for your order."),
    "export_cleared": ("Export cleared", "Your order has cleared export customs."),
    "in_transit": ("In transit", "Your order has departed and is in transit."),
    "import_cleared": ("Import cleared", "Your order has cleared import customs."),
    "delivered": ("Delivered", "Your order has been delivered."),
    "settled": ("Settled", "This order has been settled."),
}
STAGE_LABEL = {"won": "Won", "supplier_confirmed": "Supplier confirmed", "payment_received": "Payment received",
               "freight_booked": "Freight booked", "export_cleared": "Export cleared", "in_transit": "In transit",
               "import_cleared": "Import cleared", "delivered": "Delivered", "settled": "Settled",
               "closed": "Closed"}


def _anon_ref(deal) -> str:
    """A neutral opaque reference for a deal — never the buyer's name or a raw tracking code."""
    return f"D{deal.id:05d}"


def _request_for_deal(session, deal):
    """The seller's originating ServiceRequest for this deal (via its lead), or None. Seller-visible updates
    live on the seller's request, so a deal with no linked request simply publishes no auto-update."""
    lead = session.get(Lead, deal.lead_id) if deal.lead_id else None
    return lead.request_id if lead else None


def on_deal_stage_change(session, deal, from_stage, to_stage, *, actor=None, now=None):
    """The projector's hook. Publishes ONE allowlisted, sanitized SellerUpdate for the reached stage — attached
    to the seller's originating request. Idempotent per (seller, deal, stage): a repeat is a no-op. Skips when
    the deal has no linked request. Never raises into the caller."""
    now = now or datetime.utcnow()
    tmpl = AUTO_TEMPLATES.get(to_stage)
    if not tmpl or deal.owner_id is None:
        return None
    request_id = _request_for_deal(session, deal)
    if not request_id:
        return None                                      # no seller request to attach the update to
    public_status, summary = tmpl
    # defence in depth — the templates are fixed, but never publish anything that scans dirty
    if sanitize_scan(public_status) or sanitize_scan(summary):
        return None
    anon = _anon_ref(deal)
    dup = session.exec(select(SellerUpdate).where(
        SellerUpdate.seller_id == deal.owner_id, SellerUpdate.anon_ref == anon,
        SellerUpdate.public_status == public_status)).first()
    if dup:
        return dup                                       # already notified for this stage
    su = SellerUpdate(request_id=request_id, lead_id=deal.lead_id, seller_id=deal.owner_id, anon_ref=anon,
                      public_status=public_status, summary=summary, published=True,
                      published_by=getattr(actor, "email", "") or "system", created_at=now)
    session.add(su)
    session.flush()
    audit(session, actor, "seller_update", su.id, "auto_progress_published",
          {"deal_id": deal.id, "stage": to_stage, "source": "deal_stage_change"}, tenant_id=deal.owner_id)
    return su


def _request_for_exception(session, exc: OperationalException):
    if exc.deal_id:
        deal = session.get(Deal, exc.deal_id)
        if deal:
            rid = _request_for_deal(session, deal)
            if rid:
                return rid
    if exc.operation_case_id:
        case = session.get(OperationCase, exc.operation_case_id)
        if case and case.request_id:
            return case.request_id
    return None


def draft_exception_update(session, exc: OperationalException, *, actor=None, now=None):
    """A sensitive exception never auto-publishes — create a DRAFT (published=False) update for admin review,
    attached to the seller's originating request. Skips when there is no seller request to attach to. The
    seller-safe description is sanitized; if it scans dirty it is dropped to a neutral placeholder."""
    now = now or datetime.utcnow()
    if exc.tenant_id is None:
        return None
    request_id = _request_for_exception(session, exc)
    if not request_id:
        return None
    safe = exc.seller_safe_description or "An operational issue is being handled."
    if sanitize_scan(safe):
        safe = "An operational issue is being handled."
    su = SellerUpdate(request_id=request_id, seller_id=exc.tenant_id, anon_ref=f"EX{exc.id:05d}",
                      public_status="Update pending", summary=safe, published=False,
                      published_by="", created_at=now)
    session.add(su)
    session.flush()
    audit(session, actor, "seller_update", su.id, "exception_draft_created",
          {"exception_id": exc.id}, tenant_id=exc.tenant_id)
    return su


def deal_seller_progress(session, deal) -> dict:
    """The complete sanitized operational picture a seller may see for their own deal. Every nested projection
    is already masked; this assembles them and NEVER adds buyer/provider identity, costs, or margin."""
    idx = DEAL_STAGES.index(deal.stage) if deal.stage in DEAL_STAGES else 0
    completed = [STAGE_LABEL.get(s, s) for s in DEAL_STAGES[:idx + 1] if s != "closed"]
    nxt = DEAL_STAGES[idx + 1] if idx + 1 < len(DEAL_STAGES) else None
    ships = session.exec(select(Shipment).where(Shipment.deal_id == deal.id)).all()
    excs = session.exec(select(OperationalException).where(
        OperationalException.deal_id == deal.id,
        OperationalException.status.in_(EXC.OPEN_STATUSES))).all()
    reqs = session.exec(select(DocumentRequirement).where(
        DocumentRequirement.deal_id == deal.id,
        DocumentRequirement.seller_action_required == True)).all()  # noqa: E712
    pays = session.exec(select(PaymentMilestone).where(
        PaymentMilestone.deal_id == deal.id, PaymentMilestone.seller_visible == True)).all()  # noqa: E712
    return {
        "reference": _anon_ref(deal),
        "stage": STAGE_LABEL.get(deal.stage, deal.stage),
        "completed_milestones": completed,
        "next_milestone": STAGE_LABEL.get(nxt) if nxt and nxt != "closed" else None,
        "action_required": any(r.status in ("missing", "requested") for r in reqs),
        "shipments": [SHIP.shipment_seller_view(s) for s in ships],
        "exceptions": [EXC.exception_seller_view(e) for e in excs],
        "requested_documents": [{"doc_type": r.doc_type, "status": r.status,
                                 "due": r.due_date.date().isoformat() if r.due_date else ""} for r in reqs],
        "payments": [v for v in (PAY.payment_seller_view(p) for p in pays) if v],
    }
