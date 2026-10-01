"""Load researched buyers for a seller's buyer-hunt request as CONFIDENTIAL MANAGED buyers (Phase 11).

Every buyer becomes a Lead in the ADMIN pool — owner_id NULL, managed, seller_id = the requester, request_id,
pipeline_stage 'identified', a unique anon_ref and a first StageEvent — so the seller only ever sees the anonymized
funnel, never a buyer identity. ONE transaction (all or nothing); idempotent (a re-run adds 0); never marks the
request done and never notifies anyone. Prints counts only — never names or emails.

    # stream the file from the Mac so no buyer file is left on the server:
    ssh root@<host> 'docker exec -i go4it-app python scripts/load_managed_buyers.py --request SR-202608-0001 \
        --stdin --exclude-countries US,MX --dry-run' < docs/prospects/buyers_trsharks.json

buyers.json = {"buyers": [{company, dest_iso, city, website, email, phones|phone, buys|wants, source_url, ...}]}
(the shape scripts/gen_*_buyers_report.py write). Replaces scripts/deliver_request.py, which gave the buyers to
the seller's own account.
"""
import argparse
import json
import os
import re
import sys
from collections import Counter, defaultdict
from datetime import datetime

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)

from sqlmodel import Session, func, select                                     # noqa: E402

from app import pipeline                                                       # noqa: E402
from app.db import engine, init_db                                             # noqa: E402
from app.lead_service import content_hash, find_duplicate                     # noqa: E402
from app.models import Lead, ServiceRequest, StageEvent                        # noqa: E402

_EMAIL = re.compile(r"[A-Za-z0-9._%+'-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,}")
COLS = ("rows", "excluded", "no_company", "no_email", "duplicate", "existing", "new", "new_with_email")


def slug(s):
    return re.sub(r"[^a-z0-9]+", "-", (s or "").lower()).strip("-")[:60]


def find_request(session, ref):
    ref = (ref or "").strip()
    if ref.isdigit():
        return session.get(ServiceRequest, int(ref))
    return session.exec(select(ServiceRequest).where(ServiceRequest.tracking_code == ref)).first()


def refuse_reason(session, sr):
    if sr is None:
        return "request not found"
    if sr.request_type != "buyer_hunt":
        return f"{sr.tracking_code} is a '{sr.request_type}' request, not a buyer hunt"
    if sr.status in ("rejected", "cancelled"):
        return f"{sr.tracking_code} is {sr.status}"
    if not sr.owner_id:
        return f"{sr.tracking_code} has no seller"
    tag = sr.result_source_tag or f"req-{sr.id}"
    seller_owned = session.exec(select(func.count()).where(
        (Lead.request_id == sr.id) | (Lead.source == tag),
        (Lead.managed == False) | (Lead.managed == None))).one()                  # noqa: E711,E712
    if seller_owned:
        return (f"{seller_owned} buyer(s) of {sr.tracking_code} are still seller-owned (pre-confidential) — "
                "run scripts/backfill_confidential.py first")
    return ""


def _text(v):
    return ", ".join(str(x) for x in v) if isinstance(v, list) else str(v or "")


def plan(session, sr, buyers, exclude_countries=(), exclude_regex="", require_email=False, only_countries=()):
    """Pure read: the Lead rows that WOULD be created + a per-country report. Nothing is added to the session."""
    tag = sr.result_source_tag or f"req-{sr.id}"
    excluded = {c.strip().upper() for c in exclude_countries if c.strip()}
    only = {c.strip().upper() for c in only_countries if c.strip()}       # e.g. a later Canada-only wave
    rx = re.compile(exclude_regex, re.I) if exclude_regex else None
    existing = session.exec(select(Lead).where(Lead.request_id == sr.id)).all()
    have_email = {(ld.email or "").lower() for ld in existing if ld.email}
    have_key = {(slug(ld.buyer_company), (ld.dest_country or "").upper()) for ld in existing}
    report = defaultdict(Counter)
    seen_key, seen_email, rows = set(), set(), []
    for b in buyers:
        company = (b.get("company") or "").strip()
        iso = (b.get("dest_iso") or "").strip().upper()
        c = report[iso or "??"]
        c["rows"] += 1
        if not company:
            c["no_company"] += 1
            continue
        if iso in excluded or (only and iso not in only) or (rx and rx.search(" ".join([company, _text(b.get("tier")), _text(b.get("product")),
                                                          _text(b.get("buys") or b.get("wants"))]))):
            c["excluded"] += 1
            continue
        raw = (b.get("email") or "").strip()
        m = _EMAIL.search(raw)
        email = m.group(0).lower() if m else ""
        key = (slug(company), iso)
        if key in seen_key:
            c["duplicate"] += 1
            continue
        notes = f"match {b.get('match_score', '')} | {b.get('tier', '')} | {_text(b.get('product'))}"
        if raw and raw.lower() != email:
            notes += f" | contact field: {raw[:200]}"          # phones/URLs/extra addresses stay admin-only
        if email and email in seen_email:
            notes += " | same email as another buyer (kept on the first)"
            email = ""
        if not email:
            c["no_email"] += 1
            if require_email:
                continue
        if key in have_key or (email and email in have_email):
            c["existing"] += 1
            seen_key.add(key)
            continue
        phones = b.get("phones") or ([b["phone"]] if b.get("phone") else [])
        lead = Lead(source=tag, external_id=f"{tag}:{key[0]}:{iso}", product=sr.product or "Buyer",
                    category=f"req-{sr.request_type}", owner_id=None, managed=True, seller_id=sr.owner_id,
                    request_id=sr.id, pipeline_stage="identified", status="new",
                    spec=_text(b.get("buys") or b.get("wants"))[:300], dest_country=iso,
                    dest_city=(b.get("city") or "").strip()[:120], buyer_company=company[:200],
                    phone=str(phones[0] if phones else "")[:60], email=email,
                    website=(b.get("website") or "").strip()[:300], source_url=(b.get("source_url") or "")[:500],
                    notes=notes[:600])
        lead.content_hash = content_hash(lead)
        if find_duplicate(session, lead) is not None:
            c["existing"] += 1
            seen_key.add(key)
            continue
        seen_key.add(key)
        if email:
            seen_email.add(email)
        rows.append(lead)
        c["new"] += 1
        c["new_with_email"] += 1 if email else 0
    return rows, report


def load(session, sr, rows):
    """Create every planned buyer in ONE transaction (the caller commits). anon_ref + StageEvent per buyer."""
    from app.company_service import link_lead_company_safe
    for lead in rows:
        session.add(lead)
        session.flush()
        lead.tracking_code = f"G4-{datetime.utcnow():%Y%m}-{lead.id:04d}"
        pipeline.assign_anon_ref(session, lead)
        session.add(StageEvent(lead_id=lead.id, request_id=sr.id, from_stage="", to_stage="identified",
                               note="loaded as a confidential managed buyer", inferred=False))
        link_lead_company_safe(session, lead)                   # savepoint; never breaks the load
    pipeline.audit(session, None, "request", sr.id, "managed_load",
                   {"new": len(rows), "with_email": sum(1 for r in rows if r.email)}, tenant_id=sr.owner_id)


def print_report(report):
    print("country  " + "  ".join(f"{c:>14}" for c in COLS))
    total = Counter()
    for iso in sorted(report, key=lambda k: -report[k]["rows"]):
        total.update(report[iso])
        print(f"{iso:<8} " + "  ".join(f"{report[iso][c]:>14}" for c in COLS))
    print("TOTAL    " + "  ".join(f"{total[c]:>14}" for c in COLS))
    return total


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("file", nargs="?", help="buyers.json (or use --stdin)")
    ap.add_argument("--request", required=True, help="SR-YYYYMM-NNNN tracking code or request id")
    ap.add_argument("--stdin", action="store_true", help="read buyers.json from stdin")
    ap.add_argument("--exclude-countries", default="", help="comma-separated ISO codes, e.g. US,MX")
    ap.add_argument("--only-countries", default="", help="load ONLY these ISO codes, e.g. CA (a later wave)")
    ap.add_argument("--exclude-regex", default="", help="skip buyers whose company/tier/product/buys match")
    ap.add_argument("--require-email", action="store_true", help="only load buyers with a usable email")
    ap.add_argument("--dry-run", action="store_true", help="plan + report only, write nothing")
    a = ap.parse_args(argv)
    if not a.stdin and not a.file:
        ap.error("give a buyers.json file or --stdin")
    data = json.load(sys.stdin) if a.stdin else json.load(open(a.file, encoding="utf-8"))
    buyers = data.get("buyers", []) if isinstance(data, dict) else list(data)
    init_db()
    with Session(engine) as s:
        sr = find_request(s, a.request)
        why = refuse_reason(s, sr)
        if why:
            print(f"REFUSED: {why}")
            return 2
        rows, report = plan(s, sr, buyers, a.exclude_countries.split(","), a.exclude_regex, a.require_email,
                            a.only_countries.split(","))
        print(f"{sr.tracking_code} · {sr.product} · seller #{sr.owner_id} · {len(buyers)} rows in file")
        total = print_report(report)
        if a.dry_run:
            print(f"[dry-run] would create {total['new']} confidential buyers "
                  f"({total['new_with_email']} with email). Nothing written.")
            return 0
        try:
            load(s, sr, rows)
            s.commit()
        except Exception as e:                                  # noqa: BLE001
            s.rollback()
            print(f"ERROR — rolled back, nothing loaded: {e}")
            return 1
        dups = s.connection().exec_driver_sql(
            "SELECT request_id, anon_ref, COUNT(*) c FROM lead WHERE anon_ref != '' AND request_id = ? "
            "GROUP BY request_id, anon_ref HAVING c > 1", (sr.id,)).fetchall()
        managed = s.exec(select(func.count()).where(Lead.request_id == sr.id, Lead.managed == True,  # noqa: E712
                                                    Lead.owner_id == None)).one()                   # noqa: E711
        print(f"loaded {total['new']} buyers ({total['new_with_email']} with email). Request now has {managed} "
              f"confidential buyers. duplicate anon_refs (must be 0): {len(dups)}")
        return 0 if not dups else 1


if __name__ == "__main__":
    sys.exit(main())
