"""Load the UAE decor-buyer research into go4it.

    ./.venv/bin/python scripts/load_uae.py

Reads docs/research/uae_decor_buyers.json -> Leads (source="uae-decor-buyer",
dest_country="AE"), for the Decora Store home-decor range. Idempotent (dedup on
(source, external_id)). Contacts captured; phone-only ones flagged NEEDS ENRICHMENT.
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

SRC = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                   "docs", "research", "uae_decor_buyers.json")


def slug(s):
    return re.sub(r"[^a-z0-9]+", "-", (s or "").lower()).strip("-")[:60]


def to_dt(s):
    for fmt in ("%b %d, %Y", "%Y-%m-%d", "%d %b %Y", "%B %d, %Y"):
        try:
            return datetime.strptime((s or "").strip(), fmt)
        except ValueError:
            continue
    return None


def run():
    if not os.path.exists(SRC):
        print(f"{SRC} not found - run harvest_uae_decor_buyers.py first")
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
                source="uae-decor-buyer", external_id=f"uae-decor:{slug(company)}",
                product="Decorative homeware (Decora range)", category="home-decor",
                spec=buys[:300], dest_country="AE", dest_city=b.get("city") or "",
                buyer_company=company, phone=(phones[0] if phones else ""),
                email=b.get("email") or "", website=b.get("website") or "", notes=notes[:600],
            )
            if create_lead(s, lead, run=False) is not None:
                new += 1
            else:
                dup += 1

        # active RFQs (highest-intent buyers) -> Leads source="uae-decor-rfq"
        rp = os.path.join(os.path.dirname(SRC), "uae_decor_rfqs.json")
        rnew = rdup = 0
        if os.path.exists(rp):
            for q in json.load(open(rp, encoding="utf-8")).get("rfqs", []):
                buyer = (q.get("buyer") or "").strip()
                if not buyer:
                    continue
                gated = "contact GATED (enrich)" if q.get("contact_gated") else "contact available"
                notes = (f"ACTIVE RFQ | wants: {q.get('product', '')} | posted {q.get('posted', '')} "
                         f"| {gated} | {q.get('note', '')}")
                lead = Lead(
                    source="uae-decor-rfq",
                    external_id=f"uae-rfq:{slug(buyer)}:{slug((q.get('product') or '')[:24])}",
                    product=(q.get("product") or "Home decor")[:200], category="home-decor",
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
