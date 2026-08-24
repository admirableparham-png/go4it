"""Phase 7 — payment milestones: Decimal amounts, confirmation requires an authorized admin + evidence (never
an email/screenshot alone), corrections audited, and seller views are masked."""
from decimal import Decimal

from sqlmodel import Session, select

from app import payments as PAY
from app.models import AuditLog, Deal, Lead, PaymentMilestone, User


def _users(s):
    admin = s.exec(select(User).where(User.email == "admin@t.local")).one()
    seller = s.exec(select(User).where(User.email == "sellerA@t.local")).one()
    return admin, seller


def _deal(s, owner):
    lead = Lead(product="x", tracking_code="G4-P", dest_country="GE", owner_id=owner.id)
    s.add(lead); s.commit(); s.refresh(lead)
    d = Deal(lead_id=lead.id, owner_id=owner.id, stage="won", tracking_code="G4-P-D")
    s.add(d); s.commit(); s.refresh(d)
    return d


def test_amounts_are_decimal(ops_engine):
    with Session(ops_engine) as s:
        admin, _ = _users(s)
        pm = PAY.create_milestone(s, milestone_type="buyer_deposit", currency="usd",
                                  expected_amount="1000.5", actor=admin); s.commit()
        assert pm.expected_amount == "1000.50" and pm.currency == "USD"
        assert pm.reference.startswith("PM-")


def test_confirmation_requires_authorized_admin_and_evidence(ops_engine):
    with Session(ops_engine) as s:
        admin, seller = _users(s)
        pm = PAY.create_milestone(s, milestone_type="buyer_deposit", currency="USD",
                                  expected_amount="1000", actor=admin); s.commit()
        # a non-authorized actor (agent/seller) cannot confirm
        ok, err = PAY.confirm_payment(s, pm, confirmed_amount="1000", reference_code="WIRE-1", actor=seller)
        assert ok is False and "authorized admin" in err
        # even an admin cannot confirm without a reference OR evidence document
        ok2, err2 = PAY.confirm_payment(s, pm, confirmed_amount="1000", actor=admin)
        assert ok2 is False and "reference or evidence" in err2
        # admin + reference → confirmed
        ok3, _ = PAY.confirm_payment(s, pm, confirmed_amount="1000", reference_code="WIRE-1", actor=admin)
        s.commit()
        assert ok3 and pm.status == "received" and pm.confirmed_by == admin.id


def test_partial_payment(ops_engine):
    with Session(ops_engine) as s:
        admin, _ = _users(s)
        pm = PAY.create_milestone(s, milestone_type="buyer_balance", currency="USD",
                                  expected_amount="1000", actor=admin); s.commit()
        PAY.confirm_payment(s, pm, confirmed_amount="400", reference_code="WIRE-2", actor=admin); s.commit()
        assert pm.status == "partially_received"


def test_seller_view_masks_reference_and_hides_unauthorized(ops_engine):
    with Session(ops_engine) as s:
        admin, _ = _users(s)
        hidden = PAY.create_milestone(s, milestone_type="supplier_advance", currency="USD",
                                      expected_amount="500", seller_visible=False, actor=admin); s.commit()
        assert PAY.payment_seller_view(hidden) is None            # not authorized → invisible
        shown = PAY.create_milestone(s, milestone_type="buyer_deposit", currency="USD",
                                     expected_amount="500", seller_visible=True, actor=admin); s.commit()
        PAY.confirm_payment(s, shown, confirmed_amount="500", reference_code="SECRET-BANK-REF-9999",
                            actor=admin); s.commit()
        view = PAY.payment_seller_view(shown)
        assert "SECRET-BANK-REF-9999" not in str(view) and view["payment_reference"] == "••••9999"


def test_status_change_cannot_fake_received(ops_engine):
    with Session(ops_engine) as s:
        admin, _ = _users(s)
        pm = PAY.create_milestone(s, milestone_type="buyer_deposit", currency="USD",
                                  expected_amount="1", actor=admin); s.commit()
        ok, err = PAY.set_status(s, pm, "received", actor=admin)
        assert ok is False and "confirm_payment" in err          # can't bypass evidence via a status flip
        PAY.set_status(s, pm, "awaiting", actor=admin); s.commit()
        assert pm.status == "awaiting"
