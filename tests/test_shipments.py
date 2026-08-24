"""Phase 7 — shipments/legs/tracking: leg ordering, idempotent external ingest, leg-order-safe forward-only
milestone, controlled delivery (no ETA auto-complete; damage → exception), and seller-safe masking."""
from datetime import datetime, timedelta

from sqlmodel import Session, select

from app import shipments as SH
from app.models import (DeliveryConfirmation, OperationalException, Shipment, ShipmentEvent, ShipmentLeg, User)


def _seller(s):
    return s.exec(select(User).where(User.email == "sellerA@t.local")).one()


def _ship(s, owner, **kw):
    sh = SH.book_shipment(s, tenant_id=owner.id, mode="sea", origin="Bandar Abbas, IR",
                          destination="Poti, GE", **kw)
    s.commit()
    return sh


def test_leg_ordering_validates_sequence(ops_engine):
    with Session(ops_engine) as s:
        sh = _ship(s, _seller(s))
        l1, e1 = SH.add_leg(s, sh, mode="road", origin="Isfahan", destination="Bandar Abbas"); s.commit()
        assert l1.sequence == 1 and e1 == ""
        l2, _ = SH.add_leg(s, sh, mode="sea", origin="Bandar Abbas", destination="Poti"); s.commit()
        assert l2.sequence == 2                              # auto-assigned next
        dup, err = SH.add_leg(s, sh, sequence=1, mode="x", origin="a", destination="b")
        assert dup is None and "already exists" in err
        bad, err2 = SH.add_leg(s, sh, sequence=0, mode="x", origin="a", destination="b")
        assert bad is None and "positive" in err2


def test_external_event_ingest_is_idempotent(ops_engine):
    with Session(ops_engine) as s:
        sh = _ship(s, _seller(s), booking_reference="BK-1")
        ev1, made1 = SH.record_event(s, sh, event_type="departed", source="webhook:acme",
                                     external_event_id="EVT-1", event_at=datetime.utcnow()); s.commit()
        ev2, made2 = SH.record_event(s, sh, event_type="departed", source="webhook:acme",
                                     external_event_id="EVT-1", event_at=datetime.utcnow()); s.commit()
        assert made1 is True and made2 is False and ev1.id == ev2.id     # replay is a no-op
        assert len(s.exec(select(ShipmentEvent)).all()) == 1


def test_milestone_is_forward_only_and_leg_order_safe(ops_engine):
    with Session(ops_engine) as s:
        sh = _ship(s, _seller(s), booking_reference="BK-2")
        assert sh.current_milestone == "booked"
        SH.record_event(s, sh, event_type="export_cleared", source="manual"); s.commit()
        assert sh.current_milestone == "export_cleared"
        SH.record_event(s, sh, event_type="departed", source="manual", event_at=datetime.utcnow()); s.commit()
        assert sh.current_milestone == "in_transit"
        # a late/out-of-order earlier event must NOT drag the shipment backward
        SH.record_event(s, sh, event_type="export_cleared", source="manual"); s.commit()
        assert sh.current_milestone == "in_transit"
        # a bare 'delivered' tracking event does NOT complete it — only a DeliveryConfirmation may
        SH.record_event(s, sh, event_type="delivered", source="manual"); s.commit()
        assert sh.current_milestone == "in_transit"


def test_clean_delivery_marks_delivered(ops_engine):
    with Session(ops_engine) as s:
        sh = _ship(s, _seller(s), booking_reference="BK-3")
        dc, exc = SH.confirm_delivery(s, sh, source="pod_document", recipient_role="buyer"); s.commit()
        assert exc is None and sh.current_milestone == "delivered" and sh.delivery_date is not None
        assert isinstance(dc, DeliveryConfirmation)


def test_damaged_delivery_raises_exception_not_delivered(ops_engine):
    with Session(ops_engine) as s:
        sh = _ship(s, _seller(s), booking_reference="BK-4")
        dc, exc = SH.confirm_delivery(s, sh, source="admin", has_damage=True,
                                     condition_notes="3 pallets crushed"); s.commit()
        assert exc is not None and exc.exc_type == "cargo_damage"
        assert sh.current_milestone != "delivered"          # never silently completes
        assert sh.exception_state == "open"
        assert s.exec(select(OperationalException)).first() is not None


def test_seller_view_masks_sensitive_refs(ops_engine):
    with Session(ops_engine) as s:
        sh = _ship(s, _seller(s), booking_reference="BK-SECRET", container_reference="MSKU-999",
                   carrier_name_cache="Maersk internal")
        sh.estimated_arrival = datetime.utcnow() + timedelta(days=10)
        s.add(sh); s.commit()
        view = SH.shipment_seller_view(sh)
        blob = str(view).lower()
        assert "bk-secret" not in blob and "msku-999" not in blob and "maersk" not in blob
        assert view["mode"] == "sea" and view["dest_country"] == "GE"    # coarse region only
