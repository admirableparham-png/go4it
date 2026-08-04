"""Load a product line's buyers + RFQs into go4it Leads, from its spec.

    ./.venv/bin/python scripts/load_line.py cd-dvd

Reads docs/research/<buyers_file> -> Leads(source=spec.source, category=spec.category) and
docs/research/<rfqs_file> -> Leads(source=spec.rfq_source). Idempotent (dedup on (source,
external_id)). This is the ONE generic loader that replaced load_optical.py / load_uae.py.
"""
import json
import os
import re
import sys
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlmodel import Session  # noqa: E402

from app.db import engine  # noqa: E402
from app.lead_service import create_lead  # noqa: E402
from app.line_spec import load_spec, research_path  # noqa: E402
from app.models import Lead  # noqa: E402


def slug(s):
    return re.sub(r"[^a-z0-9]+", "-", (s or "").lower()).strip("-")[:60]


def to_dt(s):
    for fmt in ("%b %d, %Y", "%Y-%m-%d", "%d %b %Y", "%B %d, %Y", "%b %Y", "%B %Y"):
        try:
            return datetime.strptime((s or "").strip(), fmt)
        except ValueError:
            continue
    return None


def run(line_slug):
    spec = load_spec(line_slug)
    product = spec.get("product") or spec.get("label") or line_slug
    category = spec.get("category") or line_slug
    source = spec["source"]
    rfq_source = spec.get("rfq_source", f"{source}-rfq")

    buyers_path = research_path(spec["buyers_file"])
    if not os.path.exists(buyers_path):
        print(f"{buyers_path} not found - run harvest_line.py {line_slug} first")
        return
    buyers = json.load(open(buyers_path, encoding="utf-8")).get("buyers", [])

    new = dup = 0
    with Session(engine) as s:
        for b in buyers:
            company = (b.get("company") or "").strip()
            if not company:
                continue
            cats = ", ".join(b.get("categories", []))
            buys = ", ".join(b.get("buys", []) or b.get("fits", []))
            score = b.get("match_score", "")
            enrich = " | NEEDS ENRICHMENT" if b.get("needs_enrichment") else ""
            bulk = " | BULK" if b.get("bulk_likely") else ""
            notes = f"match {score} | {cats} | buys: {buys} | {b.get('city', '')}{bulk}{enrich}"
            phones = b.get("phones") or []
            ext = b.get("osmid") or slug(company)
            lead = Lead(
                source=source, external_id=f"{source}:{ext}",
                product=product, category=category,
                spec=buys[:300], dest_country=spec.get("dest", {}).get("iso", ""),
                dest_city=b.get("city") or "",
                buyer_company=company, phone=(phones[0] if phones else ""),
                email=b.get("email") or "", website=b.get("website") or "", notes=notes[:600],
            )
            if create_lead(s, lead, run=False) is not None:
                new += 1
            else:
                dup += 1

        rnew = rdup = 0
        rfqs_path = research_path(spec.get("rfqs_file", "")) if spec.get("rfqs_file") else ""
        if rfqs_path and os.path.exists(rfqs_path):
            for q in json.load(open(rfqs_path, encoding="utf-8")).get("rfqs", []):
                buyer = (q.get("buyer") or "").strip()
                if not buyer:
                    continue
                gated = "contact GATED (enrich)" if q.get("contact_gated") else "contact available"
                notes = (f"ACTIVE RFQ | wants: {q.get('product', '')} | posted {q.get('posted', '')} "
                         f"| {gated} | {q.get('note', '')}")
                lead = Lead(
                    source=rfq_source,
                    external_id=f"{rfq_source}:{slug(buyer)}:{slug((q.get('product') or '')[:24])}",
                    product=(q.get("product") or product)[:200], category=category,
                    spec=(q.get("product") or "")[:300],
                    dest_country=spec.get("dest", {}).get("iso", ""),
                    dest_city=q.get("city") or "", buyer_company=buyer,
                    phone=q.get("phone") or "", email=q.get("email") or "",
                    website=q.get("website") or "", posted_at=to_dt(q.get("posted")),
                    notes=notes[:600],
                )
                if create_lead(s, lead, run=False) is not None:
                    rnew += 1
                else:
                    rdup += 1

    print(f"[{line_slug}] Buyer leads: {new} new, {dup} present  |  RFQ leads: {rnew} new, {rdup} present")


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("usage: load_line.py <line-slug>   (e.g. cd-dvd, decoration)")
        sys.exit(1)
    run(sys.argv[1])
