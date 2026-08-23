"""One-time, idempotent, transactional backfill of the Trade Network (Phase 2) canonical layer.

Creates/links canonical Companies + Contacts + Provenance from existing Suppliers, Leads and seller
accounts, derives engagement classifications from real evidence, and scans for duplicate CANDIDATES (never
auto-merges). Strictly additive: it only SETS company_id/engagement_class/reply_outcome on existing rows and
populates the new side tables — it never touches owner_id/seller_id/request_id/managed/anon_ref/pipeline
history. Re-running skips already-linked rows. Provenance from the backfill is marked inferred=True.

    python scripts/backfill_trade_network.py --dry-run   # counts + candidate estimate, no writes
    python scripts/backfill_trade_network.py             # backfill
    python scripts/backfill_trade_network.py --rollback  # remove backfill-created companies/side rows + unlink

RUN ORDER on prod:  backup_db.py  ->  migrate.py  ->  backfill_confidential.py  ->  THIS.
"""
import os
import sys

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)

from sqlalchemy import func                                                    # noqa: E402
from sqlmodel import Session, select                                           # noqa: E402

from app import company_service as CS                                          # noqa: E402
from app.db import engine, init_db                                            # noqa: E402
from app.models import (Company, CompanyRole, Contact, Deal, DuplicateCandidate, Lead, Provenance,  # noqa: E402
                        Quote, ServiceRequest, Supplier, User)


def _counts(s):
    c = lambda q: s.exec(q).one()  # noqa: E731
    return {
        "leads": c(select(func.count(Lead.id))),
        "suppliers": c(select(func.count(Supplier.id))),
        "users": c(select(func.count(User.id))),
        "requests": c(select(func.count(ServiceRequest.id))),
        "campaigns(distinct source)": c(select(func.count(func.distinct(Lead.source)))),
        "quotes": c(select(func.count(Quote.id))),
        "deals": c(select(func.count(Deal.id))),
        "companies": c(select(func.count(Company.id))),
        "contacts": c(select(func.count(Contact.id))),
        "provenance": c(select(func.count(Provenance.id))),
        "leads_linked": c(select(func.count(Lead.id)).where(Lead.company_id != None)),          # noqa: E711
        "suppliers_linked": c(select(func.count(Supplier.id)).where(Supplier.company_id != None)),  # noqa: E711
        "dup_strong": c(select(func.count(DuplicateCandidate.id)).where(DuplicateCandidate.match_type == "strong")),
        "dup_potential": c(select(func.count(DuplicateCandidate.id)).where(DuplicateCandidate.match_type == "potential")),
        "unknown_sources": c(select(func.count(Provenance.id)).where(Provenance.source_type == "unknown")),
    }


def _print_counts(label, s):
    print(f"{label}:", {k: v for k, v in _counts(s).items()})


def migrate(dry=False):
    init_db()   # ensures new tables + the trade-network indexes exist
    with Session(engine) as s:
        _print_counts("PRE ", s)
        todo_sup = s.exec(select(Supplier).where(Supplier.company_id == None)).all()             # noqa: E711
        todo_lead = s.exec(select(Lead).where(Lead.company_id == None, Lead.buyer_company != "")  # noqa: E711
                           .order_by(Lead.id)).all()
        seller_uids = {u.id for u in s.exec(select(User).where(User.role.in_(("agent", "manager")))).all()}
        seller_uids |= {r.requester_id for r in s.exec(select(ServiceRequest)).all() if r.requester_id}
        print(f"to link: {len(todo_sup)} suppliers, {len(todo_lead)} leads, {len(seller_uids)} seller accounts")
        if dry:
            print("dry-run — no changes.")
            return
        n_sup = n_lead = n_seller = 0
        try:
            for sup in todo_sup:
                CS.link_supplier_company(s, sup, inferred=True)
                n_sup += 1
            s.commit()
            for lead in todo_lead:
                CS.link_lead_company(s, lead, inferred=True)
                n_lead += 1
                if n_lead % 500 == 0:
                    s.commit()
            s.commit()
            # Phase C — seller companies for platform accounts (agent/manager + request requesters)
            for uid in sorted(seller_uids):
                u = s.get(User, uid)
                if not u:
                    continue
                existing = s.exec(select(Company).where(Company.account_user_id == uid,
                                                        Company.primary_role == "seller")).first()
                if existing:
                    continue
                co = CS.get_or_create_company(s, None, u.name or u.email, "", "", role="seller")
                co.account_user_id = uid
                co.primary_role = "seller"
                s.add(co)
                s.flush()
                CS.add_provenance(s, "company", co.id, None, "manual", "Platform account",
                                  source_ref=f"user:{uid}", inferred=True)
                n_seller += 1
            s.commit()
            created = CS.scan_duplicates(s)
            s.commit()
        except Exception as e:                              # noqa: BLE001
            s.rollback()
            print("ERROR — transaction rolled back, no partial backfill:", e)
            raise
        print(f"linked {n_sup} suppliers, {n_lead} leads; created {n_seller} seller companies; "
              f"{created} duplicate candidates.")
        _print_counts("POST", s)
        # integrity assertions
        bad = s.exec(select(func.count(Lead.id)).where(
            Lead.company_id != None, Lead.company_id.notin_(select(Company.id)))).one()          # noqa: E711
        print(f"leads with dangling company_id (must be 0): {bad}")
        cross = 0
        for dc in s.exec(select(DuplicateCandidate)).all():
            la, lb = s.get(Company, dc.left_id), s.get(Company, dc.right_id)
            if la and lb and la.tenant_id != lb.tenant_id:
                cross += 1
        print(f"cross-tenant duplicate candidates (must be 0): {cross}")
        if bad or cross:
            raise RuntimeError("integrity assertion failed after backfill")


def rollback():
    """Remove ONLY backfill-created companies (those with no observed/live provenance) + their side rows,
    and unlink the leads/suppliers that pointed at them. Companies touched by the live hooks (inferred=False
    provenance) or manual admin work are preserved."""
    with Session(engine) as s:
        _print_counts("PRE ", s)
        live_company_ids = {p.entity_id for p in s.exec(select(Provenance).where(
            Provenance.entity_type == "company", Provenance.inferred == False)).all()}           # noqa: E712
        del_ids = {co.id for co in s.exec(select(Company)).all() if co.id not in live_company_ids}
        for lead in s.exec(select(Lead).where(Lead.company_id.in_(del_ids))).all():
            lead.company_id = None
            lead.engagement_class = ""
            lead.reply_outcome = ""
            s.add(lead)
        for sup in s.exec(select(Supplier).where(Supplier.company_id.in_(del_ids))).all():
            sup.company_id = None
            s.add(sup)
        for dc in s.exec(select(DuplicateCandidate)).all():
            if dc.left_id in del_ids or dc.right_id in del_ids:
                s.delete(dc)
        for ct in s.exec(select(Contact).where(Contact.company_id.in_(del_ids))).all():
            s.delete(ct)
        for p in s.exec(select(Provenance).where(Provenance.entity_type == "company",
                                                 Provenance.entity_id.in_(del_ids))).all():
            s.delete(p)
        for r in s.exec(select(CompanyRole).where(CompanyRole.company_id.in_(del_ids))).all():
            s.delete(r)
        for co in s.exec(select(Company).where(Company.id.in_(del_ids))).all():
            s.delete(co)
        s.commit()
        print(f"rolled back {len(del_ids)} backfill-created companies + side rows.")
        _print_counts("POST", s)


if __name__ == "__main__":
    if "--rollback" in sys.argv:
        rollback()
    else:
        migrate(dry="--dry-run" in sys.argv)
