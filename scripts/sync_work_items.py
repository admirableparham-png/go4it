"""Idempotent Work Queue synchronization / repair (Phase 3).

    ./.venv/bin/python scripts/sync_work_items.py            # create any MISSING open work items
    ./.venv/bin/python scripts/sync_work_items.py --dry-run  # report what WOULD be created; create nothing

Scans the safe, documented sources (unreviewed requests, open seller questions, open duplicate candidates,
bounced contacts, draft quotes, explicit job failures, overdue requests) and get-or-creates the matching OPEN
work items. Re-running NEVER duplicates an open task — the idempotency_key + the partial-unique OPEN index
guarantee it. Safe to run on a cron/launchd alongside the app; the app also creates these live at the event.
"""
import os
import sys

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)

from sqlmodel import Session  # noqa: E402

from app.db import engine, init_db  # noqa: E402
from app import work_queue as WQ  # noqa: E402


def run(dry: bool = False) -> dict:
    init_db()
    with Session(engine) as s:
        if dry:
            # Wrap the whole pass in an OUTER savepoint and roll THAT back, so the dry-run mutates nothing.
            # (A plain session.rollback() after begin_nested does not discard autobegun-savepoint work.)
            sp = s.begin_nested()
            summary = WQ.run_all_sync(s, None)
            sp.rollback()
            print(f"[dry-run] would create {summary['total']} work item(s): "
                  + ", ".join(f"{k}={v}" for k, v in summary.items() if k != "total" and v))
            return summary
        summary = WQ.run_all_sync(s, None)
        s.commit()
        print(f"created {summary['total']} work item(s): "
              + ", ".join(f"{k}={v}" for k, v in summary.items() if k != "total" and v))
        return summary


if __name__ == "__main__":
    run(dry="--dry-run" in sys.argv)
