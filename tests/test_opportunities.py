"""Phase 8 (B) — Opportunity workflow, supply matching (explained, missing requirements, no invented
capability), transparent versioned scoring, and score-version preservation when weights change."""
import importlib
from datetime import datetime

from sqlmodel import Session, select

from app import demand as DEM
from app import opportunities as OPP
from app import opportunity_scoring as SCORING
from app.models import (DemandSignal, Lead, Opportunity, OpportunityMatch, Product, Quote, User, WorkItem)


def _seed_accepted(s):
    lead = Lead(product="Zinc Sulphate", category="chemicals", dest_country="GE", tracking_code="L", owner_id=1)
    s.add(lead); s.commit(); s.refresh(lead)
    p = Product(name="Zinc Sulphate Monohydrate", category="chemicals", hs_code="283329", exw_price=590,
                origin_country="IR", min_order_qty=100, completeness_score=80, verification_status="verified",
                status="active")
    s.add(p); s.commit(); s.refresh(p)
    q = Quote(lead_id=lead.id, product_id=p.id, owner_id=1, status="accepted", buyer_response="accepted",
              tracking_code="Q", accepted_at=datetime.utcnow(), current_version_id=1)
    s.add(q); s.commit(); s.refresh(q)
    sig, _ = DEM.from_accepted_quote(s, q); s.commit()
    return sig, p


def test_opportunity_created_scored_and_matched(ops_engine):
    with Session(ops_engine) as s:
        sig, p = _seed_accepted(s)
        opp, created = OPP.ensure_from_signal(s, sig); s.commit()
        assert created and opp.reference.startswith("OPP-")
        assert opp.score > 0 and opp.score_version.startswith("s1:")
        matches = s.exec(select(OpportunityMatch).where(OpportunityMatch.opportunity_id == opp.id)).all()
        assert len(matches) == 1 and matches[0].product_id == p.id
        assert matches[0].explanation and "similarity" in matches[0].explanation  # every match explained
        # a second signal for the same product/market attaches, never duplicates the opportunity
        opp2, c2 = OPP.ensure_from_signal(s, sig); s.commit()
        assert opp2.id == opp.id and c2 is False


def test_no_supply_raises_workqueue_and_explains(ops_engine):
    with Session(ops_engine) as s:
        # a demand signal for a product with NO matching catalog entry
        sig, _ = DEM._create(s, signal_type="rfq", dedup_key="rfq1", product="Unobtainium widget",
                             dest_country="GE"); s.commit()
        opp, _ = OPP.ensure_from_signal(s, sig); s.commit()
        assert s.exec(select(OpportunityMatch).where(OpportunityMatch.opportunity_id == opp.id)).all() == []
        wi = s.exec(select(WorkItem).where(WorkItem.type == "high_demand_no_supply")).first()
        assert wi is not None and wi.related_opportunity_id == opp.id


def test_match_shows_missing_requirements_no_invented_capability(ops_engine):
    with Session(ops_engine) as s:
        lead = Lead(product="Saffron", category="spices", dest_country="AE", tracking_code="L", owner_id=1)
        s.add(lead); s.commit(); s.refresh(lead)
        # an incomplete/unverified product — matching must SHOW what's missing, never assume capability
        p = Product(name="Saffron premium", category="spices", exw_price=0, hs_code="", origin_country="",
                    completeness_score=20, verification_status="unverified", status="active")
        s.add(p); s.commit(); s.refresh(p)
        sig, _ = DEM._create(s, signal_type="positive_reply", dedup_key="p1", product="Saffron",
                             category="spices", dest_country="AE"); s.commit()
        opp, _ = OPP.ensure_from_signal(s, sig); s.commit()
        m = s.exec(select(OpportunityMatch).where(OpportunityMatch.opportunity_id == opp.id)).first()
        if m:   # if it matched on name/category, it must flag the gaps and NOT be verified
            assert m.verified is False and "verification" in m.missing_requirements


def test_workflow_transitions_are_validated(ops_engine):
    with Session(ops_engine) as s:
        sig, _ = _seed_accepted(s)
        opp, _ = OPP.ensure_from_signal(s, sig); s.commit()
        ok, err = OPP.set_status(s, opp, "converted")             # can't jump new -> converted
        assert ok is False and "cannot move" in err
        assert OPP.set_status(s, opp, "ready_for_review")[0] is True
        assert OPP.set_status(s, opp, "approved")[0] is True
        s.commit()


def test_scoring_is_transparent_and_penalises_missing_evidence():
    # a strong signal with matched supply scores well; a lone weak signal with no supply is penalised
    class S:  # minimal stand-ins
        def __init__(self, strength, conf, src, at):
            self.strength = strength; self.confidence = conf; self.source = src
            self.observed_at = at; self.created_at = at
    class Mtc:
        def __init__(self, pid=1, verified=True, missing=""):
            self.product_id = pid; self.verified = verified; self.missing_requirements = missing
    now = datetime.utcnow()
    strong, sc, bd = SCORING.score([S("strong", 90, "quote", now)], [Mtc()], now=now)
    weak, wc, bd2 = SCORING.score([S("weak", 40, "directory", now)], [], now=now)
    assert strong > weak
    # the breakdown is fully visible with a missing-data penalty component (no hidden weights)
    names = {c["component"] for c in bd2}
    assert "missing_data_penalty" in names and any(c["points"] < 0 for c in bd2)


def test_score_version_changes_when_weights_change(monkeypatch):
    v1 = SCORING.scoring_version()
    import app.config as CFG
    monkeypatch.setitem(CFG.OPP_SCORE_WEIGHTS, "demand_strength", 99.0)
    importlib.reload(SCORING)   # picks up the mutated weights
    try:
        v2 = SCORING.scoring_version()
        assert v1 != v2          # a weight change yields a NEW version (old snapshots keep theirs)
    finally:
        monkeypatch.setitem(CFG.OPP_SCORE_WEIGHTS, "demand_strength", 22.0)
        importlib.reload(SCORING)
