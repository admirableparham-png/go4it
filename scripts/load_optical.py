"""Load the UAE blank-optical-media buyer research into go4it.

    ./.venv/bin/python scripts/load_optical.py

Reads docs/research/uae_optical_buyers.json -> Leads (source="uae-optical-buyer",
category="cd-dvd", dest_country="AE") and docs/research/uae_optical_rfqs.json -> Leads
(source="uae-optical-rfq"). Idempotent (dedup on (source, external_id)).
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
from app.models import Lead  # noqa: E402

RESEARCH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "docs", "research")
SRC = os.path.join(RESEARCH, "uae_optical_buyers.json")
RFQS = os.path.join(RESEARCH, "uae_optical_rfqs.json")
PRODUCT = "Printable blank optical media (CD-R / DVD-R / Blu-ray)"
CATEGORY = "cd-dvd"


def slug(s):
    return re.sub(r"[^a-z0-9]+", "-", (s or "").lower()).strip("-")[:60]


def to_dt(s):
    for fmt in ("%b %d, %Y", "%Y-%m-%d", "%d %b %Y", "%B %d, %Y", "%b %Y", "%B %Y"):
        try:
            return datetime.strptime((s or "").strip(), fmt)
        except ValueError:
            continue
    return None


def run():
    if not os.path.exists(SRC):
        print(f"{SRC} not found - run harvest_uae_optical_buyers.py first")
        return
    with open(SRC, encoding="utf-8") as f:
        buyers = json.load(f).get("buyers", [])

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
            notes = f"match {score} | {cats} | buys: {buys} | {b.get('location', '')}{enrich}"
            phones = b.get("phones") or []
            lead = Lead(
                source="uae-optical-buyer", external_id=f"uae-optical:{slug(company)}",
                product=PRODUCT, category=CATEGORY,
                spec=buys[:300], dest_country="AE", dest_city=b.get("city") or "",
                buyer_company=company, phone=(phones[0] if phones else ""),
                email=b.get("email") or "", website=b.get("website") or "", notes=notes[:600],
            )
            if create_lead(s, lead, run=False) is not None:
                new += 1
            else:
                dup += 1

        # active RFQs (highest-intent buyers who posted demand) -> Leads source="uae-optical-rfq"
        rnew = rdup = 0
        if os.path.exists(RFQS):
            for q in json.load(open(RFQS, encoding="utf-8")).get("rfqs", []):
                buyer = (q.get("buyer") or "").strip()
                if not buyer:
                    continue
                gated = "contact GATED (enrich)" if q.get("contact_gated") else "contact available"
                notes = (f"ACTIVE RFQ | wants: {q.get('product', '')} | posted {q.get('posted', '')} "
                         f"| {gated} | {q.get('note', '')}")
                lead = Lead(
                    source="uae-optical-rfq",
                    external_id=f"uae-optical-rfq:{slug(buyer)}:{slug((q.get('product') or '')[:24])}",
                    product=(q.get("product") or PRODUCT)[:200], category=CATEGORY,
                    spec=(q.get("product") or "")[:300], dest_country="AE",
                    dest_city=q.get("city") or "", buyer_company=buyer,
                    phone=q.get("phone") or "", email=q.get("email") or "",
                    website=q.get("website") or "", posted_at=to_dt(q.get("posted")),
                    notes=notes[:600],
                )
                if create_lead(s, lead, run=False) is not None:
                    rnew += 1
                else:
                    rdup += 1

    print(f"Buyer leads: {new} new, {dup} present  |  RFQ leads: {rnew} new, {rdup} present")


if __name__ == "__main__":
    run()
