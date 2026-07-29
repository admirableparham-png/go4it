"""Load the new-products research dataset into go4it.

    ./.venv/bin/python scripts/load_newproducts.py

Reads docs/research/new_products_2026-07.json and:
  - Georgia BUYERS  -> Lead (source="research-newprod", dest_country="GE"), via create_lead(run=False)
  - Iran/UAE SELLERS -> Supplier + Product (real hs_code + origin_region + exw_price) => quotable catalog

Idempotent: re-running inserts nothing new (leads dedup on (source, external_id); products dedup on name;
suppliers dedup on normalized name). Writes to the live data.db (gitignored) — the committed JSON is the backup.
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

SRC = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                   "docs", "research", "new_products_2026-07.json")

PRODUCT_DISPLAY = {
    "cold-asphalt": "Cold asphalt (bagged cold-mix)",
    "rubber-tiles": "Rubber tiles (gym / outdoor)",
    "pour-in-place-rubber": "Pour-in-place rubber flooring",
}
DEFAULT_HS = {"cold-asphalt": "2715", "rubber-tiles": "4016.91", "pour-in-place-rubber": "4002/4004"}


def slug(s):
    return re.sub(r"[^a-z0-9]+", "-", (s or "").lower()).strip("-")


def norm(name):
    return re.sub(r"\s+", " ", (name or "").strip().lower())


def parse_price(s):
    """Extract a leading number from an indicative-price string, else 0.0."""
    m = re.search(r"(\d+(?:[.,]\d+)?)", s or "")
    if not m:
        return 0.0
    try:
        return float(m.group(1).replace(",", "."))
    except ValueError:
        return 0.0


def get_or_create_supplier(session, name, country, city, contact, email, phone):
    n = norm(name)
    existing = session.exec(select(Supplier).where(Supplier.name_normalized == n)).first()
    if existing:
        return existing
    sup = Supplier(name=name, name_normalized=n, country=country or "IR",
                   city=city or "", contact=contact or "", email=email or "", phone=phone or "")
    session.add(sup)
    session.commit()
    session.refresh(sup)
    return sup


def run():
    with open(SRC, encoding="utf-8") as f:
        data = json.load(f)
    records = data.get("records", [])

    leads_new = leads_dup = prod_new = prod_dup = 0
    with Session(engine) as s:
        for r in records:
            pkey = r.get("product_key", "")
            disp = PRODUCT_DISPLAY.get(pkey, pkey)
            role = r.get("role", "")
            company = (r.get("company") or "").strip()
            if not company:
                continue
            hs = r.get("hs_code") or DEFAULT_HS.get(pkey, "")

            if role == "GE-buyer":
                note_bits = [b for b in [
                    r.get("product_spec"),
                    (f"HS {hs}" if hs else ""),
                    (f"indicative: {r['indicative_price']}" if r.get("indicative_price") else ""),
                    r.get("notes"),
                ] if b]
                lead = Lead(
                    source="research-newprod",
                    external_id=f"{pkey}:{slug(company)}",
                    product=disp, category=pkey,
                    spec=(r.get("product_spec") or "")[:400],
                    dest_country="GE", dest_city=r.get("city") or "",
                    buyer_company=company, contact_name=r.get("contact_name") or "",
                    email=r.get("email") or "", phone=r.get("phone") or "",
                    website=r.get("website") or "",
                    notes=" | ".join(note_bits)[:600],
                )
                res = create_lead(s, lead, run=False)  # no matching/quoting/alerts
                if res is None:
                    leads_dup += 1
                else:
                    leads_new += 1

            elif role in ("IR-seller", "UAE-seller"):
                pname = f"{disp} — {company}"
                if s.exec(select(Product).where(Product.name == pname)).first():
                    prod_dup += 1
                    continue
                country = "IR" if role == "IR-seller" else "AE"
                sup = get_or_create_supplier(s, company, country, r.get("city"),
                                             r.get("contact_name"), r.get("email"), r.get("phone"))
                spec = " | ".join(b for b in [
                    r.get("product_spec"),
                    (f"indicative EXW: {r['indicative_price']}" if r.get("indicative_price") else ""),
                    (r.get("website") or ""),
                    r.get("notes"),
                ] if b)[:500]
                prod = Product(
                    name=pname, category=pkey, spec=spec, hs_code=hs,
                    exw_price=parse_price(r.get("indicative_price")), currency="USD",
                    origin_region=(r.get("city") or r.get("country") or country),
                    supplier_id=sup.id,
                )
                s.add(prod)
                s.commit()
                prod_new += 1

    print(f"Georgia buyers  -> leads:  {leads_new} new, {leads_dup} already present")
    print(f"Iran/UAE sellers -> products: {prod_new} new, {prod_dup} already present")


if __name__ == "__main__":
    run()
