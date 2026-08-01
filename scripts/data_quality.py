"""Lead data-quality pass.

    ./.venv/bin/python scripts/data_quality.py            # report only
    ./.venv/bin/python scripts/data_quality.py --apply    # blank bogus shared phones

A phone that appears on more than THRESHOLD leads is a directory placeholder, not a real contact
(e.g. 526704615 was attached to 43 harvested leads) — blanking it stops outreach wasting dials and
un-inflates the 'contactable' count. Duplicate (company, dest_country) groups are REPORTED only;
merging is left manual on purpose to avoid destroying real distinct records.
"""
import os
import sys
from collections import Counter, defaultdict

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlmodel import Session, select  # noqa: E402

from app.db import engine  # noqa: E402
from app.models import Lead  # noqa: E402

THRESHOLD = 10


def run(apply=False):
    with Session(engine) as s:
        leads = s.exec(select(Lead)).all()
        counts = Counter(l.phone for l in leads if l.phone)
        bogus = {p for p, c in counts.items() if c > THRESHOLD}
        print(f"bogus shared phones (>{THRESHOLD} leads): {[(p, counts[p]) for p in bogus] or 'none'}")

        if apply and bogus:
            blanked = 0
            for l in leads:
                if l.phone in bogus:
                    l.phone = ""
                    l.notes = ((l.notes + " | ") if l.notes else "") + "[dq] blanked shared placeholder phone"
                    s.add(l)
                    blanked += 1
            s.commit()
            print(f"APPLIED: blanked {blanked} bogus-phone leads")

        groups = defaultdict(list)
        for l in leads:
            company = (l.buyer_company or "").strip().lower()
            if company:
                groups[(company, l.dest_country or "")].append(l.id)
        dups = {k: v for k, v in groups.items() if len(v) > 1}
        top = sorted(dups.items(), key=lambda x: -len(x[1]))[:6]
        print(f"duplicate (company,dest) groups: {len(dups)} "
              f"covering {sum(len(v) for v in dups.values())} leads")
        for (company, dest), ids in top:
            print(f"   {len(ids)}x  {company[:38]:<38} {dest}  ids={ids[:6]}")


if __name__ == "__main__":
    run(apply="--apply" in sys.argv)
