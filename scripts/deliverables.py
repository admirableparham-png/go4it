"""Review delivered files for buyer-PII before any seller can download them.

Migrated deliverables stay seller_safe=False (default) — a seller can download ONLY what an admin has
explicitly cleared. Use this to list every deliverable for manual review, then mark a specific one safe
once you've confirmed it contains no buyer name, company, contact details, URLs or identifying metadata.

    python scripts/deliverables.py                 # list all (seller_safe flag shown)
    python scripts/deliverables.py --unsafe         # list only the ones still NOT seller-safe (review queue)
    python scripts/deliverables.py --mark 12        # mark deliverable 12 seller_safe=True (after review)
    python scripts/deliverables.py --unmark 12      # revoke seller-safe on 12
"""
import os
import sys

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)

from sqlmodel import Session, select                                           # noqa: E402

from app.db import engine                                                      # noqa: E402
from app.models import RequestDeliverable, ServiceRequest, User               # noqa: E402


def _rows(session, unsafe_only=False):
    q = select(RequestDeliverable).order_by(RequestDeliverable.request_id, RequestDeliverable.id)
    if unsafe_only:
        q = q.where(RequestDeliverable.seller_safe == False)                   # noqa: E712
    return session.exec(q).all()


def show(unsafe_only=False):
    with Session(engine) as s:
        rows = _rows(s, unsafe_only)
        srs = {r.id: r for r in s.exec(select(ServiceRequest)).all()}
        users = {u.id: u for u in s.exec(select(User)).all()}
        print(f"{'ID':>4}  {'REQ':>4}  {'SAFE':<5}  {'PRODUCT':<22}  {'SELLER':<22}  FILE / URL")
        print("-" * 100)
        for d in rows:
            sr = srs.get(d.request_id)
            seller = users.get(sr.owner_id) if sr else None
            safe = "YES" if d.seller_safe else "no"
            prod = (sr.product if sr else "?")[:22]
            who = (seller.email if seller else "?")[:22]
            print(f"{d.id:>4}  {d.request_id:>4}  {safe:<5}  {prod:<22}  {who:<22}  {d.file_path or d.url}")
        n_safe = sum(1 for d in rows if d.seller_safe)
        print("-" * 100)
        print(f"{len(rows)} deliverable(s); {n_safe} seller-safe, {len(rows) - n_safe} NOT seller-safe"
              f"{' (review queue)' if unsafe_only else ''}")


def mark(did, safe):
    with Session(engine) as s:
        d = s.get(RequestDeliverable, did)
        if not d:
            print(f"no deliverable id={did}"); return
        d.seller_safe = safe
        s.add(d); s.commit()
        print(f"deliverable {did} seller_safe = {safe}  (file: {d.file_path or d.url})")


if __name__ == "__main__":
    args = sys.argv[1:]
    if "--mark" in args:
        mark(int(args[args.index("--mark") + 1]), True)
    elif "--unmark" in args:
        mark(int(args[args.index("--unmark") + 1]), False)
    else:
        show(unsafe_only="--unsafe" in args)
