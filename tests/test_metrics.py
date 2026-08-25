"""Phase 8 (A) — the metric registry: exact definitions/formulas, deterministic counts, positive vs negative
reply separation, and per-currency money that is never summed across currencies."""
from datetime import datetime, timedelta

from sqlmodel import Session, select

from app import metrics as M
from app.models import Deal, Lead, Quote, Settlement, User


def _seller(s):
    return s.exec(select(User).where(User.email == "sellerA@t.local")).one()


def test_every_metric_has_a_full_definition():
    for key, d in M.METRICS.items():
        assert d.label and d.definition and d.formula and d.included and d.excluded and d.unit
        assert d.key == key
        assert "buyers" not in d.label.lower() or "qualified" in d.label.lower()  # no ambiguous "buyers" label


def test_positive_and_negative_replies_are_separate(ops_engine):
    with Session(ops_engine) as s:
        o = _seller(s)
        now = datetime.utcnow()
        s.add(Lead(product="x", tracking_code="L1", owner_id=o.id, buyer_replied_at=now, reply_outcome="positive"))
        s.add(Lead(product="x", tracking_code="L2", owner_id=o.id, buyer_replied_at=now, reply_outcome="negative"))
        s.add(Lead(product="x", tracking_code="L3", owner_id=o.id, buyer_replied_at=now, reply_outcome="auto_reply"))
        s.add(Lead(product="x", tracking_code="L4", owner_id=o.id))  # scraped, never replied
        s.commit()
        assert M.compute("positive_replies", s) == 1
        assert M.compute("negative_replies", s) == 1
        # human replies excludes autoresponders and never-replied leads
        assert M.compute("human_replies", s) == 2
        assert M.compute("researched_prospects", s) == 4


def test_pipeline_and_settled_value_are_per_currency(ops_engine):
    with Session(ops_engine) as s:
        o = _seller(s)
        # two settlements in different currencies must never be summed into one figure
        s.add(Settlement(deal_id=1, revenue="1000", currency="USD", settlement_date=datetime.utcnow()))
        s.add(Settlement(deal_id=2, revenue="500", currency="EUR", settlement_date=datetime.utcnow()))
        s.commit()
        settled = M.compute("settled_value", s)
        assert settled.get("USD") == "1000.00" and settled.get("EUR") == "500.00"
        assert "1500" not in str(settled)


def test_time_window_filters(ops_engine):
    with Session(ops_engine) as s:
        o = _seller(s)
        old = datetime.utcnow() - timedelta(days=100)
        recent = datetime.utcnow() - timedelta(days=2)
        s.add(Lead(product="x", tracking_code="A", owner_id=o.id, created_at=old))
        s.add(Lead(product="x", tracking_code="B", owner_id=o.id, created_at=recent))
        s.commit()
        assert M.compute("researched_prospects", s) == 2
        since = datetime.utcnow() - timedelta(days=30)
        assert M.compute("researched_prospects", s, since=since) == 1   # only the recent one


def test_unknown_metric_fails_loud(ops_engine):
    with Session(ops_engine) as s:
        try:
            M.compute("not_a_metric", s)
            assert False, "should raise"
        except KeyError:
            pass
