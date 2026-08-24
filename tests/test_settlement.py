"""Phase 7 — settlement: Decimal, per-currency (no cross-currency mix without FX), immutable snapshot with
appended adjustments, readiness gate, and no margin leakage to sellers."""
from sqlmodel import Session, select

from app import payments as PAY
from app.models import Deal, Lead, PaymentMilestone, Settlement, SettlementAdjustment, User


def _users(s):
    admin = s.exec(select(User).where(User.email == "admin@t.local")).one()
    seller = s.exec(select(User).where(User.email == "sellerA@t.local")).one()
    return admin, seller


def _deal(s, owner, stage="delivered"):
    lead = Lead(product="x", tracking_code="G4-S", dest_country="GE", owner_id=owner.id)
    s.add(lead); s.commit(); s.refresh(lead)
    d = Deal(lead_id=lead.id, owner_id=owner.id, stage=stage, tracking_code="G4-S-D")
    s.add(d); s.commit(); s.refresh(d)
    return d


def _paid_buyer(s, deal, admin):
    pm = PAY.create_milestone(s, milestone_type="buyer_balance", currency="USD", expected_amount="10000",
                              deal_id=deal.id, actor=admin)
    s.commit()
    PAY.confirm_payment(s, pm, confirmed_amount="10000", reference_code="WIRE", actor=admin)
    s.commit()


def test_settlement_blocks_until_ready(ops_engine):
    with Session(ops_engine) as s:
        admin, _ = _users(s)
        d = _deal(s, admin)                                       # admin owns → authorized
        st, err = PAY.record_settlement(s, d, revenue="10000", verified_costs="7000", currency="USD",
                                        actor=admin)
        assert st is None and "not ready" in err                 # no buyer payment received yet
        _paid_buyer(s, d, admin)
        st2, err2 = PAY.record_settlement(s, d, revenue="10000", verified_costs="7000", currency="USD",
                                          actor=admin); s.commit()
        assert st2 is not None and err2 == ""
        assert st2.go4it_margin == "3000.00" and st2.realized_margin == "3000.00"
        assert d.stage == "settled" and d.realized_margin == 3000.0


def test_settlement_is_immutable_corrections_are_adjustments(ops_engine):
    with Session(ops_engine) as s:
        admin, _ = _users(s)
        d = _deal(s, admin); _paid_buyer(s, d, admin)
        st, _ = PAY.record_settlement(s, d, revenue="10000", verified_costs="7000", currency="USD",
                                      actor=admin); s.commit()
        # a second settlement is refused — corrections must be adjustments
        st2, err = PAY.record_settlement(s, d, revenue="9000", verified_costs="7000", currency="USD",
                                         actor=admin)
        assert st2 is st and "already exists" in err
        adj, aerr = PAY.add_adjustment(s, st, field="verified_costs", delta="250", reason="missed fee",
                                       actor=admin); s.commit()
        assert adj is not None and aerr == ""
        assert s.exec(select(SettlementAdjustment)).one().delta == "250.00"
        # the original snapshot is untouched
        s.refresh(st)
        assert st.verified_costs == "7000.00"


def test_cross_currency_adjustment_needs_fx(ops_engine):
    with Session(ops_engine) as s:
        admin, _ = _users(s)
        d = _deal(s, admin); _paid_buyer(s, d, admin)
        st, _ = PAY.record_settlement(s, d, revenue="10000", verified_costs="7000", currency="USD",
                                      actor=admin); s.commit()
        adj, err = PAY.add_adjustment(s, st, field="operational_costs", delta="100", currency="EUR",
                                      reason="eur fee", actor=admin)
        assert adj is None and "FX snapshot" in err              # no cross-currency mix without FX


def test_seller_never_sees_margin(ops_engine):
    with Session(ops_engine) as s:
        admin, _ = _users(s)
        d = _deal(s, admin); _paid_buyer(s, d, admin)
        st, _ = PAY.record_settlement(s, d, revenue="10000", verified_costs="7000", currency="USD",
                                      actor=admin); s.commit()
        view = PAY.settlement_seller_view(st)
        assert "3000" not in str(view) and "margin" not in str(view).lower()
        assert view["settled"] is True
