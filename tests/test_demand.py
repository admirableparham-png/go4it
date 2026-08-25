"""Phase 8 (B) — DemandSignal: deterministic evidence only (scraped leads / negative / auto replies excluded),
strong signals from accepted quotes & Deals, deterministic dedup, counting methods, and seasonality history gate."""
from datetime import datetime, timedelta

from sqlmodel import Session, select

from app import demand as DEM
from app.models import Deal, DemandSignal, Lead, Product, Quote, User


def _admin(s):
    return s.exec(select(User).where(User.email == "admin@t.local")).one()


def _quote(s, *, status="accepted"):
    lead = Lead(product="Zinc", category="chem", dest_country="GE", tracking_code="L", owner_id=1)
    s.add(lead); s.commit(); s.refresh(lead)
    p = Product(name="Zinc", status="active"); s.add(p); s.commit(); s.refresh(p)
    q = Quote(lead_id=lead.id, product_id=p.id, owner_id=1, status=status, buyer_response="accepted",
              tracking_code="Q", accepted_at=datetime.utcnow(), current_version_id=1)
    s.add(q); s.commit(); s.refresh(q)
    return lead, q


def test_negative_and_scraped_never_become_demand(ops_engine):
    with Session(ops_engine) as s:
        neg = Lead(product="X", tracking_code="N", owner_id=1, reply_outcome="negative")
        auto = Lead(product="X", tracking_code="A", owner_id=1, reply_outcome="auto_reply")
        scraped = Lead(product="X", tracking_code="S", owner_id=1, reply_outcome="none",
                       engagement_class="prospect")
        s.add(neg); s.add(auto); s.add(scraped); s.commit()
        for ld in (neg, auto, scraped):
            sig, created = DEM.from_positive_reply(s, ld)
            assert sig is None and created is False           # never demand
        assert s.exec(select(DemandSignal)).all() == []


def test_positive_reply_creates_verified_signal(ops_engine):
    with Session(ops_engine) as s:
        pos = Lead(product="Honey", dest_country="AE", tracking_code="P", owner_id=1,
                   reply_outcome="positive", buyer_replied_at=datetime.utcnow())
        s.add(pos); s.commit(); s.refresh(pos)
        sig, created = DEM.from_positive_reply(s, pos); s.commit()
        assert created and sig.signal_type == "positive_reply" and sig.verification_state == "verified"
        assert sig.strength == "moderate"


def test_accepted_quote_and_deal_are_strong_and_deduped(ops_engine):
    with Session(ops_engine) as s:
        lead, q = _quote(s)
        sig, created = DEM.from_accepted_quote(s, q); s.commit()
        assert created and sig.strength == "strong" and sig.verification_state == "derived"
        # a second call is a no-op (one accepted version = one signal)
        _, again = DEM.from_accepted_quote(s, q); s.commit()
        assert again is False
        d = Deal(lead_id=lead.id, owner_id=1, tracking_code="D", stage="won"); s.add(d); s.commit(); s.refresh(d)
        dsig, dc = DEM.from_deal(s, d); s.commit()
        assert dc and dsig.signal_type == "deal" and dsig.strength == "strong"
        assert len(s.exec(select(DemandSignal)).all()) == 2      # accepted_quote + deal, each once


def test_counting_methods(ops_engine):
    with Session(ops_engine) as s:
        # two signals, same company + product/market → 2 events but 1 requirement/company/product-market
        for i in range(2):
            DEM._create(s, signal_type="positive_reply", dedup_key=f"k{i}", product="Zinc", dest_country="GE",
                        company_id=99)
        s.commit()
        assert DEM.count(s, "unique_event") == 2
        assert DEM.count(s, "unique_company") == 1
        assert DEM.count(s, "unique_requirement") == 1
        assert DEM.count(s, "unique_product_market") == 1


def test_seasonality_requires_minimum_history(ops_engine):
    with Session(ops_engine) as s:
        # one period of data → insufficient
        DEM._create(s, signal_type="rfq", dedup_key="s1", product="Tea",
                    observed_at=datetime(2026, 1, 5)); s.commit()
        r = DEM.seasonality(s, "Tea")
        assert r["sufficient"] is False and "Insufficient history" in r["message"]
        # three distinct months → sufficient
        DEM._create(s, signal_type="rfq", dedup_key="s2", product="Tea", observed_at=datetime(2026, 2, 5))
        DEM._create(s, signal_type="rfq", dedup_key="s3", product="Tea", observed_at=datetime(2026, 3, 5))
        s.commit()
        r2 = DEM.seasonality(s, "Tea")
        assert r2["sufficient"] is True and r2["sample_periods"] == 3
