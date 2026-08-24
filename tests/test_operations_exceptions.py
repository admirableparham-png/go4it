"""Phase 7 — operational exceptions: de-dup of open exceptions, Work Queue integration without duplicate
unresolved tasks, resolution closes the linked task and clears the shipment flag."""
from sqlmodel import Session, select

from app import ops_exceptions as EXC
from app import shipments as SH
from app.models import OperationalException, Shipment, User, WorkItem


def _seller(s):
    return s.exec(select(User).where(User.email == "sellerA@t.local")).one()


def test_raise_dedups_and_creates_single_task(ops_engine):
    with Session(ops_engine) as s:
        owner = _seller(s)
        sh = SH.book_shipment(s, tenant_id=owner.id, mode="sea", booking_reference="BK"); s.commit()
        e1 = EXC.raise_exception(s, exc_type="tracking_stale", shipment_id=sh.id, tenant_id=owner.id,
                                 seller_safe_description="checking in with the carrier"); s.commit()
        e2 = EXC.raise_exception(s, exc_type="tracking_stale", shipment_id=sh.id, tenant_id=owner.id,
                                 seller_safe_description="again"); s.commit()
        assert e1.id == e2.id                                     # open dup reused
        assert len(s.exec(select(OperationalException)).all()) == 1
        tasks = s.exec(select(WorkItem).where(WorkItem.related_exception_id == e1.id)).all()
        assert len(tasks) == 1                                    # no duplicate unresolved task
        s.refresh(sh)
        assert sh.exception_state == "open"


def test_resolve_closes_task_and_clears_flag(ops_engine):
    with Session(ops_engine) as s:
        owner = _seller(s)
        sh = SH.book_shipment(s, tenant_id=owner.id, mode="sea", booking_reference="BK"); s.commit()
        exc = EXC.raise_exception(s, exc_type="departure_delayed", shipment_id=sh.id, tenant_id=owner.id,
                                  seller_safe_description="short delay"); s.commit()
        EXC.resolve_exception(s, exc, resolution="carrier rescheduled", actor=owner); s.commit()
        assert exc.status == "resolved" and exc.resolved_at is not None
        s.refresh(sh)
        assert sh.exception_state == "none"                       # flag cleared (no other open exception)
        wi = s.exec(select(WorkItem).where(WorkItem.related_exception_id == exc.id)).first()
        assert wi.status in ("completed", "dismissed")


def test_seller_view_hides_internal(ops_engine):
    with Session(ops_engine) as s:
        owner = _seller(s)
        exc = EXC.raise_exception(s, exc_type="customs_hold", tenant_id=owner.id,
                                  internal_description="broker says HS code mismatch, contact +99 123",
                                  seller_safe_description="Customs needs a document."); s.commit()
        view = EXC.exception_seller_view(exc)
        assert "hs code" not in str(view).lower() and "+99" not in str(view)
        assert view["summary"] == "Customs needs a document."
