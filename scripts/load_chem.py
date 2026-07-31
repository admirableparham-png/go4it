"""Load the Georgia chemical-buyer research into go4it (Sections A + B + customs).

    ./.venv/bin/python scripts/load_chem.py

Reads the committed harvest datasets and pushes them into go4it as Leads:
  * docs/research/ge_chem_buyers.json  -> source="ge-chem-buyer"  (Section A: buyer universe)
  * docs/research/ge_chem_rfqs.json    -> source="ge-chem-rfq"    (Section B: live procurement RFQs)
  * docs/research/ge_customs.json      -> source="ge-customs"     (paid import records, if any)

Sets Lead.posted_at from the RFQ announce date / customs shipment date (freshness in the app).
Idempotent: dedup on (source, external_id) via create_lead. Contacts captured; missing ones
tagged NEEDS ENRICHMENT in notes for a later Clay pass.
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

RES = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "docs", "research")
CATEGORY = "mining-water-chemicals"


def load(name):
    p = os.path.join(RES, name)
    if not os.path.exists(p):
        return {}
    with open(p, encoding="utf-8") as f:
        return json.load(f)


def slug(s):
    return re.sub(r"[^a-z0-9]+", "-", (s or "").lower()).strip("-")[:60]


def to_dt(iso):
    try:
        return datetime.fromisoformat(iso) if iso else None
    except ValueError:
        return None


def add(s, **kw):
    return create_lead(s, Lead(**kw), run=False) is not None


def run():
    new = dup = 0
    with Session(engine) as s:
        # Section A - buyer universe -------------------------------------------
        for b in load("ge_chem_buyers.json").get("buyers", []):
            company = (b.get("company") or "").strip()
            if not company:
                continue
            enrich = " | NEEDS CONTACT ENRICHMENT" if b.get("needs_enrichment") else ""
            notes = f"{b.get('segment','')} | {b.get('reagents','')}{enrich}"
            ok = add(s, source="ge-chem-buyer", external_id=f"chem-buyer:{slug(company)}",
                     product="Mining & water-treatment chemicals", category=CATEGORY,
                     spec=(b.get("reagents") or "")[:400], dest_country="GE",
                     dest_city=b.get("city") or "", buyer_company=company,
                     email=b.get("email") or "", phone=b.get("phone") or "",
                     website=b.get("website") or "", notes=notes[:600])
            new, dup = (new + 1, dup) if ok else (new, dup + 1)

        # Section B - live procurement RFQs ------------------------------------
        for t in load("ge_chem_rfqs.json").get("tenders", []):
            notes = (f"Tender {t.get('number','')} | CPV {t.get('cpv','')} {t.get('cpv_desc','')} "
                     f"| announced {t.get('announced','')} | deadline {t.get('deadline','')} "
                     f"| {'OPEN' if t.get('open') else 'closed'}")
            ok = add(s, source="ge-chem-rfq", external_id=t.get("number", ""),
                     product=t.get("chemical") or "Chemical", category=CATEGORY,
                     spec=(t.get("cpv_desc") or "")[:300], dest_country="GE",
                     buyer_company=(t.get("buyer") or "")[:200],
                     posted_at=to_dt(t.get("announced_iso")), notes=notes[:600])
            new, dup = (new + 1, dup) if ok else (new, dup + 1)

        # Customs import records (paid) ----------------------------------------
        for r in load("ge_customs.json").get("records", []):
            company = (r.get("company") or "").strip()
            if not company:
                continue
            enrich = " | NEEDS CONTACT ENRICHMENT" if r.get("needs_enrichment") else ""
            hsd = re.sub(r"\D", "", r.get("hs", ""))
            notes = (f"Imported {r.get('chemical','')} (HS {r.get('hs','')}) "
                     f"{r.get('quantity','')} {r.get('unit','')} on {r.get('import_date','')}"
                     f"{' from ' + r['supplier'] if r.get('supplier') else ''}{enrich}")
            ok = add(s, source="ge-customs",
                     external_id=f"customs:{slug(company)}:{hsd}:{r.get('import_date','')}",
                     product=r.get("chemical") or "Chemical", category=CATEGORY,
                     spec=(r.get("product") or "")[:300], dest_country="GE",
                     buyer_company=company, email=r.get("email") or "", phone=r.get("phone") or "",
                     website=r.get("website") or "", posted_at=to_dt(r.get("import_date")),
                     notes=notes[:600])
            new, dup = (new + 1, dup) if ok else (new, dup + 1)

    print(f"Leads: {new} new, {dup} already present")


if __name__ == "__main__":
    run()
