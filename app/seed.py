"""Load a demo catalog + a buyer lead so you can see matching work.

Run with:  make seed   (or:  python -m app.seed)

SAFETY: this WIPES existing rows, so it refuses to run when the database already
has data unless you opt in with SEED_FORCE=1 — protecting real catalog/lead data.
"""
import hashlib
import os
from datetime import datetime

from sqlmodel import Session, select

from .config import MATCH_THRESHOLD
from .db import engine, init_db
from .matching import score_lead_product
from .models import Lead, Match, Product, Supplier

SUPPLIERS = [
    dict(name="Isfahan Steel Co", city="Isfahan", reliability=4, payment_terms="30% advance"),
    dict(name="Tehran Cement", city="Tehran", reliability=3),
    dict(name="Pasargad Oil", city="Tabriz", reliability=4),
    dict(name="Kerman Agro", city="Kerman", reliability=5),
]

# products keyed by supplier name
PRODUCTS = [
    dict(name="Steel rebar 12mm", category="metals", spec="A3 / B500B", hs_code="7214",
         exw_price=590, unit="ton", weight_kg_per_unit=1000, cbm_per_unit=0.13,
         packaging="bundled", min_order_qty=25, origin_region="Isfahan",
         supplier="Isfahan Steel Co"),
    dict(name="Steel I-beam IPE160", category="metals", spec="ST37", hs_code="7216",
         exw_price=680, unit="ton", weight_kg_per_unit=1000, min_order_qty=20,
         origin_region="Isfahan", supplier="Isfahan Steel Co"),
    dict(name="Portland cement 42.5", category="construction", spec="Type II", hs_code="2523",
         exw_price=55, unit="ton", weight_kg_per_unit=1000, packaging="50kg bags",
         min_order_qty=100, origin_region="Tehran", supplier="Tehran Cement"),
    dict(name="Bitumen 60/70", category="petrochemicals", spec="penetration 60/70", hs_code="2713",
         exw_price=380, unit="ton", packaging="steel drums", min_order_qty=20,
         origin_region="Tabriz", supplier="Pasargad Oil"),
    dict(name="Pistachios Akbari", category="food", spec="grade A, 22-24 caliber", hs_code="0802",
         exw_price=7500, unit="ton", packaging="10kg vacuum", min_order_qty=5,
         origin_region="Kerman", supplier="Kerman Agro"),
]

# a buyer lead that should match the rebar product (Georgia = on-corridor)
LEAD = dict(product="Steel rebar 12mm", category="metals", spec="grade B500B",
            quantity=100, unit="ton", target_price=700, currency="USD",
            dest_country="GE", dest_city="Tbilisi",
            buyer_company="Kartli Construction LLC", contact_name="G. Beridze",
            email="buyer@kartli.example", source="manual")


def _has_data(session) -> bool:
    for table in (Supplier, Product, Lead, Match):
        if session.exec(select(table)).first() is not None:
            return True
    return False


def _content_hash(lead: Lead) -> str:
    parts = [lead.product, lead.category, lead.spec, lead.quantity, lead.unit,
             lead.target_price, lead.currency, lead.dest_country,
             lead.buyer_company, lead.contact_name, lead.email]
    return hashlib.sha1("|".join(str(p).strip().lower() for p in parts).encode()).hexdigest()


def run() -> None:
    init_db()
    with Session(engine) as s:
        if _has_data(s) and os.getenv("SEED_FORCE") != "1":
            print(
                "Refusing to seed: the database already contains data.\n"
                "Seeding would DELETE it. Re-run with SEED_FORCE=1 to overwrite:\n"
                "    SEED_FORCE=1 make seed"
            )
            return

        for table in (Match, Lead, Product, Supplier):
            for row in s.exec(select(table)).all():
                s.delete(row)
        s.commit()

        suppliers = {}
        for spec in SUPPLIERS:
            sup = Supplier(name_normalized=spec["name"].lower(), **spec)
            s.add(sup)
            s.commit()
            s.refresh(sup)
            suppliers[spec["name"]] = sup

        for spec in PRODUCTS:
            data = dict(spec)
            sup = suppliers.get(data.pop("supplier", ""))
            s.add(Product(supplier_id=sup.id if sup else None, **data))
        s.commit()

        lead = Lead(**LEAD)
        lead.content_hash = _content_hash(lead)
        s.add(lead)
        s.commit()
        s.refresh(lead)
        lead.tracking_code = f"G4-{datetime.utcnow():%Y%m}-{lead.id:04d}"
        s.add(lead)
        s.commit()
        s.refresh(lead)

        n = 0
        for product in s.exec(select(Product).where(Product.active == True)).all():  # noqa: E712
            score, reasons = score_lead_product(lead, product)
            if score >= MATCH_THRESHOLD:
                s.add(Match(lead_id=lead.id, product_id=product.id, score=score, reasons=reasons))
                n += 1
        s.commit()

    print(f"Seeded {len(SUPPLIERS)} suppliers, {len(PRODUCTS)} products, 1 lead, {n} matches.")


if __name__ == "__main__":
    run()
