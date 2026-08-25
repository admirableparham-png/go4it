"""Intelligence (Phase 8) — additive, idempotent, CONSERVATIVE backfill.

    ./.venv/bin/python scripts/backfill_intelligence.py --dry-run
    ./.venv/bin/python scripts/backfill_intelligence.py
    ./.venv/bin/python scripts/backfill_intelligence.py --rollback   # PRE-GO-LIVE (inferred only)
    ./.venv/bin/python scripts/backfill_intelligence.py --recover    # POST-GO-LIVE (untouched only)

RUN ORDER (prod): backup_db.py -> migrate.py -> migrate_gate_p8.py -> THIS.

What it does — strictly additive, conservative. Creates positive demand ONLY from deterministic evidence:
  * accepted quotes + Deals -> STRONG derived signals,
  * admin-confirmed positive replies -> inferred demand (with provenance),
and groups them into Opportunities (matched to supply). It NEVER creates demand from scraped leads, email
opens/deliveries, bounces or negative/auto replies, NEVER invents quantities/market size/seasonality, preserves
original timestamps, and marks everything inferred. Operational counts (leads/quotes/deals/requests/outreach/
products) never change. Ambiguous evidence -> one aggregate review task.
"""
import os
import sys

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)

from sqlmodel import Session, func, select   # noqa: E402

from app.db import engine, init_db   # noqa: E402
from app.models import (Deal, DemandSignal, Lead, Opportunity, OpportunityMatch, OpportunitySignal, Outreach,
                        Product, Quote, ServiceRequest, WorkItem)   # noqa: E402

AMBIG_KEY = "demand_signal_ambiguous"
_OPERATIONAL = ("leads", "quotes", "deals", "requests", "outreach", "products")


def _counts(s) -> dict:
    c = lambda m: s.exec(select(func.count()).select_from(m)).one()   # noqa: E731
    return {"leads": c(Lead), "quotes": c(Quote), "deals": c(Deal), "requests": c(ServiceRequest),
            "outreach": c(Outreach), "products": c(Product), "signals": c(DemandSignal),
            "opportunities": c(Opportunity)}


def _apply(s) -> dict:
    from app import work_queue as WQ
    before_sig = s.exec(select(func.count()).select_from(DemandSignal)).one()
    before_opp = s.exec(select(func.count()).select_from(Opportunity)).one()
    # reuse the tested live demand-generation pass, marking everything inferred=True (backfill-seeded)
    WQ.sync_demand_signals(s, actor=None, inferred=True, budget=100000)
    after_sig = s.exec(select(func.count()).select_from(DemandSignal)).one()
    after_opp = s.exec(select(func.count()).select_from(Opportunity)).one()
    return {"signals_created": after_sig - before_sig, "opportunities_created": after_opp - before_opp,
            "ambiguous": 0}


def migrate(dry=False):
    init_db()
    with Session(engine) as s:
        pre = _counts(s)
        print("PRE :", pre)
        if dry:
            sp = s.begin_nested()
            res = _apply(s)
            post = _counts(s)
            sp.rollback()
            print(f"[dry-run] would create: {res}")
            print("POST:", post, "(rolled back)")
            return
        try:
            res = _apply(s)
            post = _counts(s)
            for k in _OPERATIONAL:
                if pre[k] != post[k]:
                    raise RuntimeError(f"operational count changed for {k}: {pre[k]} -> {post[k]}")
            s.commit()
            print(f"OK — {res}")
            print("POST:", post)
        except Exception:
            s.rollback()
            print("ERROR — rolled back, no partial backfill applied")
            raise


def _untouched(s, opp) -> bool:
    """An opportunity is 'untouched' (backfill-only, no admin decision) when it's still 'new', unassigned, and
    all its linked signals are inferred."""
    if opp.status != "new" or opp.owner_id is not None:
        return False
    sig_ids = [r.demand_signal_id for r in s.exec(
        select(OpportunitySignal).where(OpportunitySignal.opportunity_id == opp.id)).all()]
    if not sig_ids:
        return True
    sigs = s.exec(select(DemandSignal).where(DemandSignal.id.in_(sig_ids))).all()
    return all(sg.inferred for sg in sigs)


def _delete_opp(s, opp):
    for m in s.exec(select(OpportunityMatch).where(OpportunityMatch.opportunity_id == opp.id)).all():
        s.delete(m)
    for l in s.exec(select(OpportunitySignal).where(OpportunitySignal.opportunity_id == opp.id)).all():
        s.delete(l)
    s.delete(opp)


def rollback():
    """PRE-GO-LIVE: delete all inferred demand signals + the opportunities that were backfill-only (no admin
    decision). Real operational rows are never touched."""
    init_db()
    with Session(engine) as s:
        opps = [o for o in s.exec(select(Opportunity)).all() if _untouched(s, o)]
        for o in opps:
            _delete_opp(s, o)
        for sg in s.exec(select(DemandSignal).where(DemandSignal.inferred == True)).all():  # noqa: E712
            # only delete a signal if it no longer links to any surviving opportunity
            if not s.exec(select(OpportunitySignal).where(
                    OpportunitySignal.demand_signal_id == sg.id)).first():
                s.delete(sg)
        for w in s.exec(select(WorkItem).where(WorkItem.idempotency_key == AMBIG_KEY)).all():
            s.delete(w)
        s.commit()
        print(f"PRE-GO-LIVE rollback: removed {len(opps)} backfill-only opportunit(ies) + orphan inferred signals")


def recover():
    """POST-GO-LIVE: remove only inferred signals + untouched opportunities. Preserves every real admin decision
    (approved/assigned/advanced opportunities), alert and report."""
    init_db()
    with Session(engine) as s:
        removed = 0
        for o in s.exec(select(Opportunity)).all():
            if _untouched(s, o):
                _delete_opp(s, o); removed += 1
        for sg in s.exec(select(DemandSignal).where(DemandSignal.inferred == True)).all():  # noqa: E712
            if not s.exec(select(OpportunitySignal).where(
                    OpportunitySignal.demand_signal_id == sg.id)).first():
                s.delete(sg)
        s.commit()
        print(f"POST-GO-LIVE recovery: removed {removed} untouched inferred opportunit(ies); all admin "
              f"decisions, alerts and reports preserved")


if __name__ == "__main__":
    if "--rollback" in sys.argv:
        rollback()
    elif "--recover" in sys.argv:
        recover()
    else:
        migrate(dry="--dry-run" in sys.argv)
