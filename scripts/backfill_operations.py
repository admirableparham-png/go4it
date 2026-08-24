"""Operations (Phase 7) — additive, idempotent, CONSERVATIVE backfill.

    ./.venv/bin/python scripts/backfill_operations.py --dry-run
    ./.venv/bin/python scripts/backfill_operations.py
    ./.venv/bin/python scripts/backfill_operations.py --rollback   # PRE-GO-LIVE (inferred cases only)
    ./.venv/bin/python scripts/backfill_operations.py --recover    # POST-GO-LIVE (untouched cases only)

RUN ORDER (prod): backup_db.py -> migrate.py -> migrate_gate_p7.py -> THIS.

What it does — strictly additive, conservative:
  * Creates ONE inferred baseline OperationCase per existing Deal (the deterministic Deal↔case relationship),
    guarded by the partial-unique index so re-running never duplicates.
  * NEVER invents shipments, freight requests/offers, payments, customs clearance, delivery dates or
    remittance. It preserves legacy Deal stages exactly.
An OperationCase that cannot be tied deterministically (there are none in this deterministic pass) would raise
ONE aggregate review task. Operational counts (quotes/deals/requests/outreach/products) never change.
"""
import os
import sys
from datetime import datetime

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)

from sqlmodel import Session, func, select   # noqa: E402

from app.db import engine, init_db   # noqa: E402
from app.models import (CustomsCase, Deal, DocumentRequirement, FreightRequest, OperationCase,
                        OperationalException, Outreach, PaymentMilestone, Product, Quote, RemittanceCase,
                        ServiceRequest, Shipment, WorkItem)   # noqa: E402

AMBIG_KEY = "ambiguous_legacy_operations"
_OPERATIONAL = ("quotes", "deals", "requests", "outreach", "products")


def _counts(s) -> dict:
    c = lambda m: s.exec(select(func.count()).select_from(m)).one()   # noqa: E731
    return {"quotes": c(Quote), "deals": c(Deal), "requests": c(ServiceRequest), "outreach": c(Outreach),
            "products": c(Product), "cases": c(OperationCase)}


def _apply(s) -> dict:
    res = {"cases_created": 0, "ambiguous": 0}
    now = datetime.utcnow()
    for d in s.exec(select(Deal)).all():
        existing = s.exec(select(OperationCase).where(OperationCase.deal_id == d.id,
                                                      OperationCase.case_type == "deal")).first()
        if existing:
            continue
        case = OperationCase(case_type="deal", deal_id=d.id, tenant_id=d.owner_id, status="open",
                             inferred=True, created_at=now, updated_at=now)
        s.add(case)
        s.flush()
        case.reference = f"OP-{now:%Y%m}-{case.id:04d}"
        s.add(case)
        res["cases_created"] += 1
    if res["ambiguous"] and not s.exec(select(WorkItem).where(WorkItem.idempotency_key == AMBIG_KEY)).first():
        try:
            from app import work_queue as WQ
            WQ.create_work_item_safe(s, tenant_id=None, type="operational_handoff_required", source="automatic",
                                     title="Ambiguous legacy operational relationship",
                                     description=f"{res['ambiguous']} record(s) could not be tied to a Deal/"
                                                 "Request deterministically — review before use.",
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


def _case_referenced(s, case) -> bool:
    """True if any real operational record hangs off this case (so recover must keep it)."""
    for model, col in ((Shipment, Shipment.operation_case_id), (FreightRequest, FreightRequest.operation_case_id),
                       (DocumentRequirement, DocumentRequirement.operation_case_id),
                       (CustomsCase, CustomsCase.operation_case_id),
                       (PaymentMilestone, PaymentMilestone.operation_case_id),
                       (RemittanceCase, RemittanceCase.operation_case_id),
                       (OperationalException, OperationalException.operation_case_id)):
        if s.exec(select(model).where(col == case.id)).first():
            return True
    return False


def rollback():
    """PRE-GO-LIVE: delete inferred baseline cases + the aggregate review task. Deals/quotes/requests and their
    totals/history are never touched."""
    init_db()
    with Session(engine) as s:
        cases = s.exec(select(OperationCase).where(OperationCase.inferred == True)).all()   # noqa: E712
        for c in cases:
            s.delete(c)
        for w in s.exec(select(WorkItem).where(WorkItem.idempotency_key == AMBIG_KEY)).all():
            s.delete(w)
        s.commit()
        print(f"PRE-GO-LIVE rollback: deleted {len(cases)} inferred operation case(s)")


def recover():
    """POST-GO-LIVE: remove only inferred cases that were NEVER used (no shipment/freight/doc/customs/payment/
    remittance/exception references). Preserves every real operational record, document, payment and update."""
    init_db()
    with Session(engine) as s:
        removed = 0
        for c in s.exec(select(OperationCase).where(OperationCase.inferred == True)).all():   # noqa: E712
            if not _case_referenced(s, c):
                s.delete(c); removed += 1
        s.commit()
        print(f"POST-GO-LIVE recovery: removed {removed} untouched inferred case(s); all real operational "
              f"records, documents, payments and updates preserved")


if __name__ == "__main__":
    if "--rollback" in sys.argv:
        rollback()
    elif "--recover" in sys.argv:
        recover()
    else:
        migrate(dry="--dry-run" in sys.argv)
