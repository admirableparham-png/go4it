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

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlmodel import Session  # noqa: E402

from app.db import engine  # noqa: E402
from app.lead_service import create_lead  # noqa: E402
from app.models import Lead  # noqa: E402

SRC = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                   "docs", "research", "uae_decor_buyers.json")


def slug(s):
    return re.sub(r"[^a-z0-9]+", "-", (s or "").lower()).strip("-")[:60]


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
            fits = ", ".join(b.get("fits", []))
            enrich = " | NEEDS CONTACT ENRICHMENT" if b.get("needs_enrichment") else ""
            notes = f"{cats} | Decora fit: {fits} | {b.get('location', '')}{enrich}"
            phones = b.get("phones") or []
            lead = Lead(
                source="uae-decor-buyer", external_id=f"uae-decor:{slug(company)}",
                product="Decorative homeware (Decora range)", category="home-decor",
                spec=fits[:300], dest_country="AE", dest_city=b.get("city") or "",
                buyer_company=company, phone=(phones[0] if phones else ""),
                website=b.get("website") or "", notes=notes[:600],
            )
            if create_lead(s, lead, run=False) is not None:
                new += 1
            else:
                dup += 1

    print(f"Leads: {new} new, {dup} already present")


if __name__ == "__main__":
    run()
