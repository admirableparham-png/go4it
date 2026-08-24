"""Phase 7 — shipments, legs, tracking events and delivery confirmation.

A shipment's `current_milestone` only ever moves FORWARD (leg-order-safe): an out-of-order or replayed event
never drags it backward. External tracking ingestion is idempotent on (source, external_event_id). GPS/ETA/
events are never invented. A shipment reaches `delivered` ONLY through a controlled DeliveryConfirmation — never
because an ETA passed — and damage/shortage/failed delivery raise an OperationalException instead of silently
completing. Carrier/provider contacts + booking/container/tracking references are ADMIN-ONLY / masked.
"""
from datetime import datetime

from sqlalchemy.exc import IntegrityError
from sqlmodel import select

from . import ops_exceptions as EXC
from .models import (DeliveryConfirmation, FreightOffer, OperationCase, Shipment, ShipmentEvent, ShipmentLeg)
from .pipeline import audit

# forward-only milestone ladder (mirrors the operational half of DEAL_STAGES)
MILESTONE_RANK = {"planning": 0, "booked": 1, "export_cleared": 2, "in_transit": 3, "import_cleared": 4,
                  "delivered": 5}
# a tracking event's milestone implication (delivered is intentionally ABSENT — only a DeliveryConfirmation
# may mark delivered; a bare 'delivered' tracking event is evidence, not completion)
EVENT_MILESTONE = {"booked": "booked", "export_cleared": "export_cleared", "departed": "in_transit",
                   "import_cleared": "import_cleared"}


def _ref(row_id, now=None):
    now = now or datetime.utcnow()
    return f"SH-{now:%Y%m}-{row_id:04d}"


def book_shipment(session, *, case: OperationCase = None, offer: FreightOffer = None, actor=None, now=None,
                  **fields):
    """Create a shipment record. When seeded from a selected freight offer, mode/carrier default from it.
    Starts at milestone 'booked' when a booking_reference is supplied, else 'planning'."""
    now = now or datetime.utcnow()
    sh = Shipment(created_by=getattr(actor, "id", None), created_at=now, updated_at=now, **fields)
    if case is not None:
        sh.operation_case_id = case.id
        sh.deal_id = sh.deal_id or case.deal_id
        sh.tenant_id = sh.tenant_id or case.tenant_id
    if offer is not None:
        sh.freight_offer_id = offer.id
        sh.mode = sh.mode or offer.mode
    sh.current_milestone = "booked" if sh.booking_reference else "planning"
    session.add(sh)
    session.flush()
    sh.reference = _ref(sh.id, now)
    session.add(sh)
    audit(session, actor, "shipment", sh.id, "shipment_booked",
          {"case_id": sh.operation_case_id, "deal_id": sh.deal_id, "mode": sh.mode}, tenant_id=sh.tenant_id)
    return sh


def add_leg(session, sh: Shipment, *, actor=None, now=None, **fields):
    """Append a leg. `sequence` must be a positive int, unique within the shipment; if omitted it auto-assigns
    the next value. Ordering is validated so events on one leg can't reorder the whole shipment."""
    now = now or datetime.utcnow()
    existing = session.exec(select(ShipmentLeg).where(ShipmentLeg.shipment_id == sh.id)).all()
    seqs = {l.sequence for l in existing}
    seq = fields.pop("sequence", None)
    if seq is None:
        seq = (max(seqs) + 1) if seqs else 1
    if not isinstance(seq, int) or seq < 1:
        return None, "leg sequence must be a positive integer"
    if seq in seqs:
        return None, f"leg sequence {seq} already exists on this shipment"
    leg = ShipmentLeg(shipment_id=sh.id, sequence=seq, created_at=now, **fields)
    session.add(leg)
    session.flush()
    audit(session, actor, "shipment", sh.id, "leg_added", {"leg_id": leg.id, "sequence": seq},
          tenant_id=sh.tenant_id)
    return leg, ""


def _bump_milestone(sh: Shipment, milestone: str, now):
    """Advance current_milestone FORWARD only. Returns True if it moved."""
    if MILESTONE_RANK.get(milestone, -1) > MILESTONE_RANK.get(sh.current_milestone, 0):
        sh.current_milestone = milestone
        sh.updated_at = now
        return True
    return False


def record_event(session, sh: Shipment, *, event_type, event_at=None, location="", source="manual",
                 external_event_id="", leg_id=None, raw_reference="", admin_note="", seller_safe_summary="",
                 confidence="recorded", actor=None, now=None):
    """Record a tracking event (manual or imported). IDEMPOTENT on (source, external_event_id): a replayed
    webhook/import returns the existing event rather than duplicating. Advances the shipment milestone
    forward-only and never regresses on an out-of-order event. Returns (event, created)."""
    now = now or datetime.utcnow()
    if external_event_id:
        dup = session.exec(select(ShipmentEvent).where(
            ShipmentEvent.source == source, ShipmentEvent.external_event_id == external_event_id)).first()
        if dup:
            return dup, False
    ev = ShipmentEvent(shipment_id=sh.id, leg_id=leg_id, event_type=event_type, event_at=event_at,
                       recorded_at=now, location=location, source=source, external_event_id=external_event_id,
                       raw_reference=raw_reference, admin_note=admin_note,
                       seller_safe_summary=seller_safe_summary, confidence=confidence,
                       created_by=getattr(actor, "id", None))
    session.add(ev)
    try:
        session.flush()
    except IntegrityError:                     # concurrent duplicate on the partial-unique index
        session.rollback()
        dup = session.exec(select(ShipmentEvent).where(
            ShipmentEvent.source == source, ShipmentEvent.external_event_id == external_event_id)).first()
        return dup, False
    # record actual timing + last update on the shipment (and leg), forward-only milestone bump
    when = event_at or now
    if event_type == "departed":
        if not sh.actual_departure or (event_at and event_at > sh.actual_departure):
            sh.actual_departure = when
    elif event_type == "arrived":
        if not sh.actual_arrival or (event_at and event_at > sh.actual_arrival):
            sh.actual_arrival = when
    sh.last_tracking_update = when
    if source != "manual":
        sh.tracking_source = source
    _bump_milestone(sh, EVENT_MILESTONE.get(event_type, sh.current_milestone), now)
    if leg_id:
        leg = session.get(ShipmentLeg, leg_id)
        if leg and leg.shipment_id == sh.id:
            if event_type == "departed" and not leg.actual_departure:
                leg.actual_departure = when
                leg.status = "in_progress"
            elif event_type == "arrived":
                leg.actual_arrival = when
                leg.status = "completed"
            session.add(leg)
    session.add(sh)
    audit(session, actor, "shipment", sh.id, "tracking_event",
          {"event_type": event_type, "source": source, "external_event_id": external_event_id},
          tenant_id=sh.tenant_id)
    return ev, True


def confirm_delivery(session, sh: Shipment, *, source="admin", confirmed_at=None, recipient_role="",
                     document_id=None, condition_notes="", has_shortage=False, has_damage=False, failed=False,
                     actor=None, now=None):
    """Record delivery evidence. A CLEAN delivery marks the shipment delivered; a damaged/short/failed delivery
    raises an OperationalException and does NOT mark it delivered (never silently completes the deal). Returns
    (confirmation, exception_or_None)."""
    now = now or datetime.utcnow()
    dc = DeliveryConfirmation(shipment_id=sh.id, deal_id=sh.deal_id, source=source,
                              confirmed_at=confirmed_at or now, recipient_role=recipient_role,
                              document_id=document_id, condition_notes=condition_notes,
                              has_shortage=has_shortage, has_damage=has_damage, failed=failed,
                              admin_verifier=getattr(actor, "id", None), created_at=now)
    session.add(dc)
    session.flush()
    exc = None
    if failed or has_damage or has_shortage:
        exc_type = "failed_delivery" if failed else ("cargo_damage" if has_damage else "shortage")
        exc = EXC.raise_exception(
            session, exc_type=exc_type, severity="high", actor=actor,
            internal_description=f"Delivery issue on {sh.reference}: {condition_notes}"[:1000],
            seller_safe_description="A delivery issue is being resolved.",
            operation_case_id=sh.operation_case_id, shipment_id=sh.id, deal_id=sh.deal_id,
            tenant_id=sh.tenant_id, now=now)
    else:
        sh.delivery_date = confirmed_at or now
        _bump_milestone(sh, "delivered", now)
        session.add(sh)
    audit(session, actor, "shipment", sh.id, "delivery_confirmed",
          {"source": source, "failed": failed, "damage": has_damage, "shortage": has_shortage},
          tenant_id=sh.tenant_id)
    return dc, exc


# --------------------------------------------------------------------- seller-safe projection / masking
_MILESTONE_LABEL = {"planning": "Being planned", "booked": "Freight booked", "export_cleared": "Export cleared",
                    "in_transit": "In transit", "import_cleared": "Import cleared", "delivered": "Delivered"}


def shipment_seller_view(sh: Shipment) -> dict:
    """The ONLY shipment fields a seller may see. Booking/container/tracking references, carrier identity and
    internal notes are NEVER included."""
    eta = sh.estimated_arrival.date().isoformat() if sh.estimated_arrival else ""
    return {"reference": sh.reference, "mode": sh.mode, "milestone": _MILESTONE_LABEL.get(sh.current_milestone,
            sh.current_milestone), "origin_country": _country(sh.origin), "dest_country": _country(sh.destination),
            "estimated_arrival": eta, "has_exception": sh.exception_state == "open"}


def _country(loc: str) -> str:
    """Reduce a free-form location to a coarse region token (last comma-separated part) so a seller never sees a
    full buyer address through a shipment origin/destination."""
    if not loc:
        return ""
    return loc.split(",")[-1].strip()
