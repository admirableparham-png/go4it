"""Phase 7 — operational analytics: per-currency (never summed across currencies), and no invented averages
where evidence is insufficient."""
from datetime import datetime, timedelta

from sqlmodel import Session, select

import app.main as main
from app.models import PaymentMilestone, Shipment, User


def _admin(s):
    return s.exec(select(User).where(User.email == "admin@t.local")).one()


def test_payments_are_per_currency(ops_engine, monkeypatch):
    monkeypatch.setattr(main, "engine", ops_engine)
    with Session(ops_engine) as s:
        admin = _admin(s)
        s.add(PaymentMilestone(milestone_type="buyer_deposit", currency="USD", confirmed_amount="1000",
                               status="received"))
        s.add(PaymentMilestone(milestone_type="buyer_deposit", currency="EUR", confirmed_amount="500",
                               status="received"))
        s.commit()
        a = main._ops_analytics(s)
    # USD and EUR are kept separate — never summed into one figure
    assert a["received_by_ccy"].get("USD") == "1000.00"
    assert a["received_by_ccy"].get("EUR") == "500.00"
    assert "1500" not in str(a["received_by_ccy"])


def test_transit_average_only_with_real_data(ops_engine, monkeypatch):
    monkeypatch.setattr(main, "engine", ops_engine)
    with Session(ops_engine) as s:
        # a shipment with no actual dates → no invented transit average
        s.add(Shipment(mode="sea", current_milestone="in_transit")); s.commit()
        a = main._ops_analytics(s)
        assert a["avg_transit_days"] is None                      # insufficient data, not a guess
        dep = datetime.utcnow() - timedelta(days=12)
        s.add(Shipment(mode="sea", current_milestone="delivered", actual_departure=dep,
                       actual_arrival=dep + timedelta(days=12))); s.commit()
        a2 = main._ops_analytics(s)
        assert a2["avg_transit_days"] == 12.0                     # computed only from real dates
