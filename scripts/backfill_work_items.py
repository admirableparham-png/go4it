"""Requests + Work Queue (Phase 3) — additive, idempotent backfill.

    ./.venv/bin/python scripts/backfill_work_items.py --dry-run   # report; change nothing
    ./.venv/bin/python scripts/backfill_work_items.py             # apply (transactional)
    ./.venv/bin/python scripts/backfill_work_items.py --rollback  # PRE-GO-LIVE revert (see below)
    ./.venv/bin/python scripts/backfill_work_items.py --recover   # POST-GO-LIVE safe cleanup (see below)

RUN ORDER (prod): backup_db.py -> migrate.py -> backfill_confidential.py -> backfill_trade_network.py -> THIS.

TWO reversal paths — pick by whether admins have started using the Work Queue:
  * --rollback  = PRE-GO-LIVE. Nothing real exists yet, so it is safe to fully undo the backfill: delete ALL
                  inferred (backfill-seeded) work items + inferred baseline status events and reset the two
                  request columns (workflow_status='', direction='sell') on the touched requests. Do NOT use
                  this after admins have begun working the queue.
  * --recover   = POST-GO-LIVE. Removes ONLY backfill-seeded work items that NO admin has touched (still open,
                  unassigned, never started/dispositioned) to clear the seeded backlog, while PRESERVING every
                  genuine admin-created/assigned/worked item AND all request workflow_status/history. It never
                  resets request columns. For a full post-go-live revert, restore the pre-migration backup.

CONSERVATIVE: derives `workflow_status` from the legacy status and `direction` from the request type for every
request (seeding ONE inferred RequestStatusEvent as the baseline), then get-or-creates work items ONLY for
genuinely-current conditions (unreviewed requests, open seller questions, open duplicates, bounced contacts,
draft quotes, explicit job failures, overdue requests). It invents NO historical follow-ups and creates NO
overdue task without a due date. Backfill-seeded rows are marked inferred=True so `--rollback` reverses cleanly.
Operational rows (requests/messages/leads/quotes/deals/seller_updates) are never mutated beyond the two new
request columns; PRE/POST counts are asserted equal.
"""
import os
import sys

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)

from sqlmodel import Session, select  # noqa: E402
from sqlalchemy import func  # noqa: E402

from app.db import engine, init_db  # noqa: E402
from app.models import (Deal, Lead, Outreach, Quote, RequestMessage, RequestStatusEvent, ServiceRequest,  # noqa: E402
                        SellerUpdate, WorkItem)
from app import work_queue as WQ  # noqa: E402
from app import request_service as RS  # noqa: E402


def _counts(s) -> dict:
    c = lambda q: s.exec(q).one()  # noqa: E731
    return {
        "requests": c(select(func.count(ServiceRequest.id))),
        "messages": c(select(func.count(RequestMessage.id))),
        "seller_updates": c(select(func.count(SellerUpdate.id))),
        "leads": c(select(func.count(Lead.id))),
        "outreach": c(select(func.count(Outreach.id))),
        "quotes": c(select(func.count(Quote.id))),
        "deals": c(select(func.count(Deal.id))),
        "work_items": c(select(func.count(WorkItem.id))),
        "status_events": c(select(func.count(RequestStatusEvent.id))),
    }


# columns the backfill may set on requests — everything else must match PRE/POST exactly
_OPERATIONAL = ("requests", "messages", "seller_updates", "leads", "outreach", "quotes", "deals")


def _derive_requests(s) -> int:
    """Set direction (from type) + workflow_status (from legacy status) on every request, seeding ONE inferred
    baseline RequestStatusEvent where the request has no status history yet. Idempotent."""
    seeded = 0
    existing_evt_reqs = {r for r in s.exec(select(RequestStatusEvent.request_id).distinct())}
    for sr in s.exec(select(ServiceRequest)).all():
        sr.direction = RS.direction_for_type(sr.request_type)
        wf = sr.workflow_status or RS.workflow_from_legacy(sr.status)
        sr.workflow_status = wf
        s.add(sr)
        if sr.id not in existing_evt_reqs:
            s.add(RequestStatusEvent(request_id=sr.id, from_status="", to_status=wf,
                                     from_legacy="", to_legacy=sr.status, actor_id=None,
                                     reason="migrated from legacy status", inferred=True))
            seeded += 1
    return seeded


def migrate(dry=False):
    init_db()
    if dry:
        # A dry-run must mutate nothing even though run_all_sync uses inner SAVEPOINTs (create_work_item_safe).
        # Bind the Session to a dedicated CONNECTION-level transaction and roll THAT back — robust to the inner
        # savepoints (an outer session.begin_nested() gets invalidated when an inner savepoint rolls back on a
        # duplicate-key IntegrityError, which is common once work items already exist).
        conn = engine.connect()
        trans = conn.begin()
        try:
            ds = Session(bind=conn)
            pre = _counts(ds)
            print("PRE :", pre)
            seeded = _derive_requests(ds)
            summary = WQ.run_all_sync(ds, None, inferred=True)
            post = _counts(ds)
            ds.close()
        finally:
            trans.rollback()               # discard EVERYTHING done on this connection — nothing persisted
            conn.close()
        print(f"[dry-run] would seed {seeded} baseline status event(s); create {summary['total']} work "
              f"item(s): " + ", ".join(f"{k}={v}" for k, v in summary.items() if k != "total" and v))
        print("POST:", post, "(rolled back — nothing persisted)")
        return
    with Session(engine) as s:
        pre = _counts(s)
        print("PRE :", pre)
        try:
            seeded = _derive_requests(s)
            summary = WQ.run_all_sync(s, None, inferred=True)
            post = _counts(s)
            # integrity: operational rows are untouched; only work items + status events grew
            for k in _OPERATIONAL:
                if pre[k] != post[k]:
                    raise RuntimeError(f"operational count changed for {k}: {pre[k]} -> {post[k]}")
            # every work item resolves to a real tenant-or-system + at least a related record or 'other'.
            # Check EVERY related_* column (request/lead/company/quote/outreach AND the newer deal/product/
            # contract/operation_case/shipment/payment/exception/opportunity/alert/conversation/proposal/
            # automation links) so newer work-item types are never mis-flagged as unrelated.
            _related_cols = [c for c in WorkItem.__table__.columns.keys() if c.startswith("related_")]
            for wi in s.exec(select(WorkItem)).all():
                if wi.type != "other" and not any(getattr(wi, c) for c in _related_cols):
                    raise RuntimeError(f"work item {wi.id} has no related record")
            s.commit()
            print(f"OK — seeded {seeded} baseline status event(s); created {summary['total']} work item(s): "
                  + ", ".join(f"{k}={v}" for k, v in summary.items() if k != "total" and v))
            print("POST:", post)
        except Exception:
            s.rollback()
            print("ERROR — transaction rolled back, no partial backfill applied")
            raise


def rollback():
    """PRE-GO-LIVE full revert — safe only while nothing real exists yet (see module docstring)."""
    init_db()
    with Session(engine) as s:
        # requests touched by the backfill are exactly those with an inferred baseline status event
        touched = {r for r in s.exec(select(RequestStatusEvent.request_id)
                                     .where(RequestStatusEvent.inferred == True))}   # noqa: E712
        for sr in s.exec(select(ServiceRequest).where(ServiceRequest.id.in_(touched))).all():
            sr.workflow_status = ""
            sr.direction = "sell"    # the post-migration column default
            s.add(sr)
        evts = s.exec(select(RequestStatusEvent).where(RequestStatusEvent.inferred == True)).all()  # noqa: E712
        wis = s.exec(select(WorkItem).where(WorkItem.inferred == True)).all()   # noqa: E712
        n_evt, n_wi = len(evts), len(wis)
        for e in evts:
            s.delete(e)
        for w in wis:
            s.delete(w)
        s.commit()
        print(f"PRE-GO-LIVE rollback: reset {len(touched)} request(s), deleted {n_evt} inferred status "
              f"event(s) and {n_wi} inferred work item(s)")


def recover():
    """POST-GO-LIVE safe cleanup. Removes ONLY backfill-seeded (inferred) work items that NO admin has
    touched — still open, unassigned, never started or dispositioned — clearing the seeded backlog while
    PRESERVING every genuine admin-created/assigned/worked item and ALL request workflow_status + history.
    Never resets request columns. (For a full post-go-live revert, restore the pre-migration backup instead.)"""
    init_db()
    with Session(engine) as s:
        total_inferred = s.exec(select(func.count(WorkItem.id)).where(WorkItem.inferred == True)).one()  # noqa: E712
        untouched = s.exec(select(WorkItem).where(
            WorkItem.inferred == True,                    # noqa: E712  backfill-seeded only
            WorkItem.status == "open",                    # never worked
            WorkItem.assigned_admin_id.is_(None),         # never assigned
            WorkItem.started_at.is_(None))).all()         # never started
        n = len(untouched)
        preserved = total_inferred - n                    # inferred items an admin has engaged, kept intact
        for w in untouched:
            s.delete(w)
        s.commit()
        print(f"POST-GO-LIVE recovery: removed {n} untouched backfill-seeded work item(s); preserved {preserved} "
              f"engaged seeded item(s) + all admin-created items and request state (no columns reset)")


if __name__ == "__main__":
    if "--rollback" in sys.argv:
        rollback()
    elif "--recover" in sys.argv:
        recover()
    else:
        migrate(dry="--dry-run" in sys.argv)
