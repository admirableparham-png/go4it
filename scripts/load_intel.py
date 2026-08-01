"""Load the real-source intelligence harvests into go4it.

    ./.venv/bin/python scripts/load_intel.py

Reads the committed harvest datasets under docs/research/ and pushes them into go4it:

  GE demand -> Lead
    * ge_tenders.json        source="ge-procurement"   (live municipal/institutional buyers)
    * ge_businesses.json     source="osm-ge"           (named gyms/sports venues = rubber buyers)
  Supply -> Supplier + Product (quotable catalog)
    * suppliers_uae_iran.json  UAE only  (clean rubber-tile makers w/ phones)
      (Iran parscenter rows are noisy -> kept in the JSON as reference, NOT loaded)

Idempotent: leads dedup on (source, external_id); products on name; suppliers on
normalized name. trade_stats_georgia.json is market intelligence (feeds the opportunity
report), not leads - it is not loaded here.
"""
import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlmodel import Session, select  # noqa: E402

from app.db import engine  # noqa: E402
from app.lead_service import create_lead  # noqa: E402
from app.models import Lead, Product, Supplier  # noqa: E402

RES = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "docs", "research")

DISPLAY = {
    "cold-asphalt": "Cold asphalt (bagged cold-mix)",
    "rubber-tiles": "Rubber tiles (gym / outdoor)",
    "pour-in-place-rubber": "Pour-in-place rubber flooring",
    "construction-buyer": "Construction / building materials",
}
HS = {"cold-asphalt": "2715", "rubber-tiles": "4016.91", "pour-in-place-rubber": "4002/4004"}


def load(name):
    p = os.path.join(RES, name)
    if not os.path.exists(p):
        print(f"   (skip - {name} not found)")
        return None
    with open(p, encoding="utf-8") as f:
        return json.load(f)


def slug(s):
    return re.sub(r"[^a-z0-9]+", "-", (s or "").lower()).strip("-")[:60]


def norm(s):
    return re.sub(r"\s+", " ", (s or "").strip().lower())


def get_or_create_supplier(s, name, country, city, phone):
    n = norm(name)
    ex = s.exec(select(Supplier).where(Supplier.name_normalized == n)).first()
    if ex:
        return ex
    sup = Supplier(name=name, name_normalized=n, country=country, city=city or "", phone=phone or "")
    s.add(sup)
    s.commit()
    s.refresh(sup)
    return sup


def add_lead(s, **kw):
    lead = Lead(**kw)
    return create_lead(s, lead, run=False) is not None   # True=new, False=dup


def run():
    new = dup = prod_new = prod_dup = 0
    with Session(engine) as s:
        # 1) procurement tenders --------------------------------------------------
        d = load("ge_tenders.json")
        for t in (d or {}).get("tenders", []):
            pkey = t.get("product_key", "rubber-tiles")
            notes = (f"Public tender {t.get('number','')} | CPV {t.get('cpv','')} "
                     f"{t.get('cpv_desc','')} | announced {t.get('announced','')} "
                     f"| bid deadline {t.get('deadline','')}")
            ok = add_lead(s, source="ge-procurement", external_id=t.get("number", ""),
                          product=DISPLAY.get(pkey, pkey), category=pkey,
                          spec=(t.get("cpv_desc") or "")[:300], dest_country="GE",
                          buyer_company=(t.get("buyer") or "")[:200], notes=notes[:600])
            new, dup = (new + 1, dup) if ok else (new, dup + 1)

        # 2) OSM businesses (rubber-tile buyers only) -----------------------------
        d = load("ge_businesses.json")
        for b in (d or {}).get("businesses", []):
            if b.get("product_key") != "rubber-tiles":
                continue
            notes = " | ".join(x for x in [
                b.get("role"), b.get("city"), b.get("street"),
                ("NEEDS CONTACT ENRICHMENT" if b.get("needs_enrichment") else "")] if x)
            ok = add_lead(s, source="osm-ge",
                          external_id=f"{slug(b.get('company'))}:{slug(b.get('city'))}",
                          product=DISPLAY["rubber-tiles"], category="rubber-tiles",
                          spec=(b.get("sport") or "")[:120], dest_country="GE",
                          dest_city=b.get("city") or "", buyer_company=b.get("company") or "",
                          phone=b.get("phone") or "", website=b.get("website") or "",
                          notes=notes[:600])
            new, dup = (new + 1, dup) if ok else (new, dup + 1)

        # 3) UAE suppliers -> catalog ---------------------------------------------
        d = load("suppliers_uae_iran.json")
        for sup in (d or {}).get("suppliers", []):
            if sup.get("country") != "AE":
                continue
            pkey = sup.get("product_key", "rubber-tiles")
            company = (sup.get("company") or "").strip()
            if not company:
                continue
            pname = f"{DISPLAY.get(pkey, pkey)} - {company}"
            if s.exec(select(Product).where(Product.name == pname)).first():
                prod_dup += 1
                continue
            phones = sup.get("phones") or []
            supplier = get_or_create_supplier(s, company, "AE", sup.get("city"),
                                               phones[0] if phones else "")
            spec = " | ".join(x for x in [sup.get("location"),
                              ("tel: " + ", ".join(phones) if phones else "")] if x)[:400]
            s.add(Product(name=pname, category=pkey, spec=spec, hs_code=HS.get(pkey, ""),
                          origin_region=sup.get("city") or "UAE", currency="USD",
                          supplier_id=supplier.id))
            s.commit()
            prod_new += 1

    print(f"Leads:    {new} new, {dup} already present")
    print(f"Products: {prod_new} new, {prod_dup} already present (UAE suppliers)")


if __name__ == "__main__":
    run()
