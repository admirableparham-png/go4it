"""One-time, idempotent, transactional migration to the confidential model.

Reassigns every seller-delivered buyer (Lead.source 'req-<id>') from the seller to the ADMIN pool
(owner_id=NULL, managed=1), and stamps seller_id + request_id + pipeline_stage + a stable anon_ref + an
initial StageEvent so the funnel has real history. Re-running only touches rows not yet migrated. Prints
pre/post counts. Quotes/Deals/Messages/files are keyed by lead_id and stay connected (unchanged).

    python scripts/backfill_confidential.py             # migrate
    python scripts/backfill_confidential.py --rollback  # undo: owner_id=seller_id, managed=0
    python scripts/backfill_confidential.py --dry-run   # counts only, no changes
"""
import os
import re
import sys

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)

from sqlalchemy import func                                                    # noqa: E402
from sqlmodel import Session, select                                           # noqa: E402

from app import pipeline                                                       # noqa: E402
from app.db import engine, init_db                                             # noqa: E402
from app.models import Deal, Lead, Quote, RequestMessage, ServiceRequest, StageEvent  # noqa: E402

STATUS_TO_STAGE = {"new": "identified", "quoted": "quote_sent", "negotiating": "negotiating",
                   "won": "won", "lost": "lost"}


def _index_info(s):
    """Prove the DB-level uniqueness enforcement exists (not just an app-side assertion): the UNIQUE index on
    lead(request_id, anon_ref). Shows uniqueness + partial-ness + the DDL, straight from sqlite_master."""
    conn = s.connection()
    ddl = conn.exec_driver_sql(
        "SELECT sql FROM sqlite_master WHERE type='index' AND name='uq_lead_req_anonref'").fetchone()
    if not ddl:
        return "MISSING — no DB-level UNIQUE(request_id, anon_ref) index!"
    meta = {r[1]: (r[2], r[4]) for r in conn.exec_driver_sql("PRAGMA index_list('lead')").fetchall()}
    unique, partial = meta.get("uq_lead_req_anonref", (0, 0))
    cols = [r[2] for r in conn.exec_driver_sql("PRAGMA index_info('uq_lead_req_anonref')").fetchall()]
    return f"uq_lead_req_anonref UNIQUE={bool(unique)} PARTIAL={bool(partial)} cols={cols} :: {ddl[0]}"


def _counts(s):
    c = lambda q: s.exec(q).one()  # noqa: E731
    return {
        "delivered_leads(req-*)": c(select(func.count(Lead.id)).where(Lead.source.like("req-%"))),
        "managed": c(select(func.count(Lead.id)).where(Lead.managed == True)),                # noqa: E712
        "delivered_admin_owned": c(select(func.count(Lead.id)).where(
            Lead.source.like("req-%"), Lead.owner_id == None)),                                # noqa: E711
        "requests": c(select(func.count(ServiceRequest.id))),
        "quotes": c(select(func.count(Quote.id))),
        "deals": c(select(func.count(Deal.id))),
        "request_messages": c(select(func.count(RequestMessage.id))),
    }


def migrate(dry=False):
    init_db()   # ensure StageEvent/SellerUpdate/AuditLog tables + the anon_ref unique index exist
    with Session(engine) as s:
        print("PRE  index:", _index_info(s))
        print("PRE :", _counts(s))
        todo = s.exec(select(Lead).where(
            Lead.source.like("req-%"),
            (Lead.managed == False) | (Lead.managed == None)                                  # noqa: E711,E712
        ).order_by(Lead.id)).all()
        print(f"to migrate: {len(todo)} delivered buyers")
        if dry:
            print("dry-run — no changes."); return
        n = 0
        try:
            for lead in todo:
                m = re.match(r"req-(\d+)", lead.source or "")
                if m:
                    lead.request_id = int(m.group(1))
                lead.seller_id = lead.owner_id
                lead.managed = True
                lead.pipeline_stage = STATUS_TO_STAGE.get(lead.status, "identified")
                s.add(lead)
                if not (lead.anon_ref or "").strip():
                    pipeline.assign_anon_ref(s, lead)      # unique per request, retry-safe
                # inferred=True: this is a migration-seeded starting point, NOT observed activity. The funnel
                # marks these buyers as history-incomplete rather than inventing the steps they never had.
                s.add(StageEvent(lead_id=lead.id, request_id=lead.request_id, from_stage="",
                                 to_stage=lead.pipeline_stage, note="migrated to confidential (inferred)",
                                 inferred=True))
                lead.owner_id = None                       # → admin pool: seller can no longer see the PII row
                s.add(lead)
                # cascade ownership of any existing quotes/deals so they can't surface on a seller page
                for q in s.exec(select(Quote).where(Quote.lead_id == lead.id)).all():
                    q.owner_id = None; s.add(q)
                for d in s.exec(select(Deal).where(Deal.lead_id == lead.id)).all():
                    d.owner_id = None; s.add(d)
                n += 1
            s.commit()
        except Exception as e:                             # noqa: BLE001
            s.rollback()
            print("ERROR — transaction rolled back, no partial migration:", e)
            raise
        print(f"migrated {n} buyers to confidential.")
        print("POST:", _counts(s))
        # integrity: every migrated buyer's quotes/deals still resolve by lead_id
        orphan_q = s.exec(select(func.count(Quote.id)).where(
            Quote.lead_id.notin_(select(Lead.id)))).one()
        print(f"orphaned quotes (should be 0): {orphan_q}")
        # uniqueness held: re-assert the partial unique index exists, then verify no duplicate
        # (request_id, anon_ref) slipped through. On a live SQLite DB __table_args__ won't create the
        # constraint — the index (made by init_db / migrate.py) does; this proves it applied.
        conn = s.connection()
        conn.exec_driver_sql("CREATE UNIQUE INDEX IF NOT EXISTS uq_lead_req_anonref "
                             "ON lead(request_id, anon_ref) WHERE anon_ref != ''")
        dups = conn.exec_driver_sql(
            "SELECT request_id, anon_ref, COUNT(*) c FROM lead WHERE anon_ref != '' "
            "GROUP BY request_id, anon_ref HAVING c > 1").fetchall()
        print(f"duplicate (request_id, anon_ref) groups (must be 0): {len(dups)}")
        if dups:
            raise RuntimeError(f"anon_ref uniqueness violated after backfill: {list(dups[:5])}")
        print("POST index:", _index_info(s))


def rollback():
    with Session(engine) as s:
        print("PRE :", _counts(s))
        rows = s.exec(select(Lead).where(Lead.managed == True, Lead.source.like("req-%"))).all()  # noqa: E712
        for lead in rows:
            if lead.seller_id is not None:
                lead.owner_id = lead.seller_id
                for q in s.exec(select(Quote).where(Quote.lead_id == lead.id)).all():
                    q.owner_id = lead.seller_id; s.add(q)
                for d in s.exec(select(Deal).where(Deal.lead_id == lead.id)).all():
                    d.owner_id = lead.seller_id; s.add(d)
            lead.managed = False
            s.add(lead)
        s.commit()
        print(f"rolled back {len(rows)} buyers to seller-owned (managed=0).")
        print("POST:", _counts(s))


if __name__ == "__main__":
    if "--rollback" in sys.argv:
        rollback()
    else:
        migrate(dry="--dry-run" in sys.argv)
