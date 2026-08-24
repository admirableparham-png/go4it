"""Commercial (Phase 6) — additive, idempotent, CONSERVATIVE backfill.

    ./.venv/bin/python scripts/backfill_commercial.py --dry-run
    ./.venv/bin/python scripts/backfill_commercial.py
    ./.venv/bin/python scripts/backfill_commercial.py --rollback   # PRE-GO-LIVE (inferred rows only)
    ./.venv/bin/python scripts/backfill_commercial.py --recover    # POST-GO-LIVE (untouched only)

RUN ORDER (prod): backup_db.py -> migrate.py -> migrate_gate_p6.py -> THIS.

What it does — strictly additive, conservative:
  * Creates ONE inferred QuoteVersion per existing Quote from its stored terms (status mirrors the quote;
    NEVER marks a version approved unless the quote row is already approved/sent/accepted). Sets
    quote.current_version_id. NEVER sends, NEVER mints a buyer token, NEVER creates a Deal.
  * Links the existing Deal(s) to their quote's inferred version conservatively (only when unambiguous).
    An ambiguous quote↔deal link raises ONE aggregate review task.
Operational counts (quotes/deals/requests/outreach/products) never change.
"""
import os
import sys

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)

from sqlmodel import Session, func, select   # noqa: E402

from app import quote_service as QS   # noqa: E402
from app.db import engine, init_db   # noqa: E402
from app.models import (Deal, Outreach, Product, Quote, QuoteVersion, ServiceRequest, WorkItem)   # noqa: E402

AMBIG_KEY = "ambiguous_legacy_commercial"
_OPERATIONAL = ("quotes", "deals", "requests", "outreach", "products")


def _counts(s) -> dict:
    c = lambda m: s.exec(select(func.count()).select_from(m)).one()   # noqa: E731
    return {"quotes": c(Quote), "deals": c(Deal), "requests": c(ServiceRequest), "outreach": c(Outreach),
            "products": c(Product), "versions": c(QuoteVersion)}


def _apply(s) -> dict:
    res = {"versions_created": 0, "deals_linked": 0, "ambiguous": 0}
    for q in s.exec(select(Quote)).all():
        existing = s.exec(select(QuoteVersion).where(QuoteVersion.quote_id == q.id)).first()
        if existing:
            continue
        QS.ensure_version(s, q, inferred=True)   # status mirrors the quote; never elevated to approved
        res["versions_created"] += 1
    # link existing deals to the inferred version of their quote (deterministic on deal.quote_id)
    for d in s.exec(select(Deal).where(Deal.quote_version_id == None)).all():   # noqa: E711
        if not d.quote_id:
            res["ambiguous"] += 1
            continue
        ver = s.exec(select(QuoteVersion).where(QuoteVersion.quote_id == d.quote_id)).first()
        if ver:
            d.quote_version_id = ver.id; s.add(d); res["deals_linked"] += 1
        else:
            res["ambiguous"] += 1
    if res["ambiguous"] and not s.exec(select(WorkItem).where(WorkItem.idempotency_key == AMBIG_KEY)).first():
        try:
            from app import work_queue as WQ
            WQ.create_work_item_safe(s, tenant_id=None, type="ambiguous_legacy_commercial", source="automatic",
                                     title="Ambiguous legacy commercial relationship",
                                     description=f"{res['ambiguous']} deal(s) could not be linked to a quote "
                                                 "version deterministically — review before use.",
                                     idempotency_key=AMBIG_KEY, condition_version="backfill")
        except Exception:  # noqa: BLE001
            pass
    return res


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


def rollback():
    """PRE-GO-LIVE: delete inferred quote versions + unlink deals + the aggregate review task. Quotes/deals
    and their totals/history are never touched."""
    init_db()
    with Session(engine) as s:
        vers = s.exec(select(QuoteVersion).where(QuoteVersion.inferred == True)).all()   # noqa: E712
        vids = {v.id for v in vers}
        n_unlink = 0
        for d in s.exec(select(Deal)).all():
            if d.quote_version_id in vids:
                d.quote_version_id = None; s.add(d); n_unlink += 1
        for q in s.exec(select(Quote)).all():
            if q.current_version_id in vids:
                q.current_version_id = None; s.add(q)
        for v in vers:
            s.delete(v)
        for w in s.exec(select(WorkItem).where(WorkItem.idempotency_key == AMBIG_KEY)).all():
            s.delete(w)
        s.commit()
        print(f"PRE-GO-LIVE rollback: deleted {len(vers)} inferred version(s), unlinked {n_unlink} deal(s)")


def recover():
    """POST-GO-LIVE: remove only inferred versions that were NEVER referenced (no approvals/tokens/deal link).
    Preserves every real approval, buyer action, contract and deal."""
    init_db()
    with Session(engine) as s:
        from app.models import QuoteAccessToken, QuoteApproval
        removed = 0
        for v in s.exec(select(QuoteVersion).where(QuoteVersion.inferred == True)).all():   # noqa: E712
            referenced = (
                s.exec(select(Deal).where(Deal.quote_version_id == v.id)).first()
                or s.exec(select(QuoteAccessToken).where(QuoteAccessToken.quote_version_id == v.id)).first()
                or s.exec(select(QuoteApproval).where(QuoteApproval.quote_version_id == v.id)).first())
            q = s.get(Quote, v.quote_id)
            if not referenced and q and q.status in ("draft", "expired", "cancelled", "superseded"):
                if q.current_version_id == v.id:
                    q.current_version_id = None; s.add(q)
                s.delete(v); removed += 1
        s.commit()
        print(f"POST-GO-LIVE recovery: removed {removed} untouched inferred version(s); all real approvals, "
              f"buyer actions, contracts and deals preserved")


if __name__ == "__main__":
    if "--rollback" in sys.argv:
        rollback()
    elif "--recover" in sys.argv:
        recover()
    else:
        migrate(dry="--dry-run" in sys.argv)
