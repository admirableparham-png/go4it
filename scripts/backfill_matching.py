"""One-time: run catalog matching across ALL leads (matches only, no auto-quote).

    ./.venv/bin/python scripts/backfill_matching.py

Bulk loaders create leads with run=False, so 99% of the corpus never got matched and could not
be quoted from the UI. This backfills Match rows for every lead so the revenue loop works for the
harvested buyers. auto_quote=False avoids drafting thousands of quotes / firing alerts.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlmodel import Session, select  # noqa: E402

from app.db import engine  # noqa: E402
from app.lead_service import run_matching  # noqa: E402
from app.models import Lead  # noqa: E402


def run():
    with Session(engine) as s:
        leads = s.exec(select(Lead)).all()
        total = len(leads)
        matched = 0
        for i, lead in enumerate(leads, 1):
            saved = run_matching(s, lead, auto_quote=False)
            if saved:
                matched += 1
            if i % 250 == 0:
                print(f"   {i}/{total} processed, {matched} with >=1 match")
        print(f"done: {matched}/{total} leads now have at least one catalog match")


if __name__ == "__main__":
    run()
