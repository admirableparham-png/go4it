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
        # a signal from a recorded accepted quote is DERIVED (calculated), never inferred (assumption)
        assert created and sig.strength == "strong" and sig.verification_state == "derived"
        assert sig.inferred is False
        # a second call is a no-op (one accepted version = one signal)
        _, again = DEM.from_accepted_quote(s, q); s.commit()
        assert again is False


def test_quote_to_deal_is_one_countable_demand_event(ops_engine):
    """An accepted quote and the Deal created FROM it are ONE commercial demand event. Both are preserved as
    evidence, but they must not count as two independent demand signals."""
    with Session(ops_engine) as s:
        lead, q = _quote(s)                                     # q.current_version_id == 1
        qsig, _ = DEM.from_accepted_quote(s, q); s.commit()
        # the Deal was created FROM this accepted quote version (Phase 6: one Deal per version)
        d = Deal(lead_id=lead.id, owner_id=1, tracking_code="D", stage="won", quote_version_id=1)
        s.add(d); s.commit(); s.refresh(d)
        dsig, dc = DEM.from_deal(s, d); s.commit()
        assert dc and dsig.signal_type == "deal"
        # BOTH rows are kept as evidence...
        assert len(s.exec(select(DemandSignal)).all()) == 2
        # ...but they share a commercial_event_key, so they count as ONE demand event
        assert qsig.commercial_event_key == dsig.commercial_event_key == "qv:1"
        assert DEM.count(s, "unique_event") == 1
        # two unrelated positive replies remain two distinct events
        for i in range(2):
            l = Lead(product="Y", tracking_code=f"R{i}", owner_id=1, reply_outcome="positive",
                     buyer_replied_at=datetime.utcnow())
            s.add(l); s.commit(); s.refresh(l)
            DEM.from_positive_reply(s, l)
        s.commit()
        assert DEM.count(s, "unique_event") == 3               # 1 (quote+deal) + 2 replies


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


def test_seasonality_needs_same_season_across_three_years(ops_engine):
    """A seasonal claim requires the SAME season across >= 3 different YEARS — not three consecutive months of
    one year, and not three weeks."""
    with Session(ops_engine) as s:
        # three DIFFERENT months in ONE year → NOT a seasonal cycle → insufficient
        for i, m in enumerate((1, 2, 3)):
            DEM._create(s, signal_type="rfq", dedup_key=f"a{i}", product="Tea",
                        observed_at=datetime(2026, m, 5))
        s.commit()
        r = DEM.seasonality(s, "Tea")
        assert r["sufficient"] is False and "at least 3 years" in r["message"]
        # the SAME month (January) across THREE years → a real seasonal cycle → sufficient
        for i, y in enumerate((2024, 2025, 2026)):
            DEM._create(s, signal_type="rfq", dedup_key=f"jan{i}", product="Saffron",
                        observed_at=datetime(y, 1, 15))
        s.commit()
        r2 = DEM.seasonality(s, "Saffron")
        assert r2["sufficient"] is True and r2["best_season_years"] == 3
        assert "Jan" in r2["seasons"] and r2["seasons"]["Jan"] == [2024, 2025, 2026]
