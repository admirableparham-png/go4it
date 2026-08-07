"""Seed the HONEY offer into go4it so the app can quote it: one priced Product + freight corridors
(Iran -> Iraq / UAE / Qatar / Pakistan) + per-market cost params. Idempotent.

    ./.venv/bin/python scripts/migrate.py          # adds ratecard.dest_country
    ./.venv/bin/python scripts/seed_honey_offer.py

Offer (founder-supplied 2026-08-07): 5 grades (multifloral/thyme/Sidr/honeydew/+royal jelly),
EXW $8.00/kg, 25 kg food-grade drums, trial 25 kg, up to 1 t/month, CoO available, pay LC/SWIFT/crypto.
FREIGHT NUMBERS ARE RESEARCHED PLACEHOLDERS (per-tonne LCL) — confirm/adjust at /rates before quoting live.
Honey is priced in USD on purpose (the FxRate table is empty; an IRR price would quote 1:1 and be wrong).
"""
import os
import sys

from sqlmodel import Session, select

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from app.db import engine, init_db  # noqa: E402
from app.models import CostParam, Product, RateCard, Supplier  # noqa: E402

SUPPLIER = "Iran Natural Honey (go4it sourcing)"
PRODUCT = "Iranian Natural Honey - bulk (multifloral / thyme / Sidr / honeydew / +royal jelly)"
GRADES = "multifloral, thyme, Sidr, honeydew, honey with royal jelly"

# Per-tonne LCL freight placeholders (USD/tonne): (inland Iran interior->exit, international exit->dest).
CORRIDORS = {
    "IQ": ("Iraq", "Tabriz -> Iraq border (Bashmaq/Mehran)", "-> Baghdad/Basra/Erbil", 150, 250),
    "AE": ("United Arab Emirates", "Tehran -> Bandar Abbas", "sea LCL -> Jebel Ali (Dubai)", 130, 320),
    "QA": ("Qatar", "Tehran -> Bushehr/Bandar Abbas", "sea -> Hamad Port (Doha)", 150, 500),
    "PK": ("Pakistan", "Tehran -> Bandar Abbas", "sea/land -> Karachi", 120, 300),
}
# Per-market cost params (premium natural honey: slightly higher margin than the commodity GE lane).
COST = {"export_clearance": 150, "coo_fee": 80, "insurance_pct": 0.5,
        "financing_pct": 1.0, "margin_pct": 12, "margin_floor_pct": 6}


def run():
    init_db()
    with Session(engine) as s:
        # 1) supplier
        sup = s.exec(select(Supplier).where(Supplier.name == SUPPLIER)).first()
        if not sup:
            sup = Supplier(name=SUPPLIER, country="IR", note="Cheap high-quality Iranian honey supplier "
                           "(founder-secured). CoO available; lab specs on request.")
            s.add(sup); s.commit(); s.refresh(sup)

        # 2) product (upsert by name)
        p = s.exec(select(Product).where(Product.name == PRODUCT)).first() or Product(name=PRODUCT)
        p.category = "food-honey"
        p.spec = (f"Single-origin Iranian natural honey. Grades: {GRADES}. 25 kg food-grade drums. "
                  "Trial order 25 kg; indicative pricing shown for a 500 kg order; up to ~1 t/month. "
                  "Certificate of Origin available; lab specs (moisture/HMF/antibiotic-free) on request. "
                  "Payment: LC / SWIFT / crypto.")
        p.hs_code = "0409"
        p.exw_price = 8.0
        p.currency = "USD"
        p.unit = "kg"
        p.weight_kg_per_unit = 1.0
        p.packaging = "25 kg food-grade bulk drums"
        p.min_order_qty = 500          # realistic regular order for default quoting (trial is 25 kg)
        p.origin_region = "Iran"
        p.supplier_id = sup.id
        p.active = True
        s.add(p); s.commit(); s.refresh(p)

        # 3) tag any legacy (untagged) rate cards as Georgia so they stay GE-scoped
        for c in s.exec(select(RateCard).where(RateCard.dest_country == "")).all():
            c.dest_country = "GE"; s.add(c)
        s.commit()

        # 4) honey corridors (idempotent: clear this set, re-add)
        for c in s.exec(select(RateCard).where(RateCard.dest_country.in_(list(CORRIDORS)))).all():
            s.delete(c)
        s.commit()
        for iso, (name, inland_lane, intl_lane, inland_pt, intl_pt) in CORRIDORS.items():
            s.add(RateCard(leg="inland", dest_country=iso, lane_from=inland_lane.split(" -> ")[0],
                           lane_to=inland_lane, rate_per_tonne=inland_pt, truck_capacity_t=1, active=True))
            s.add(RateCard(leg="international", dest_country=iso, lane_from=inland_lane,
                           lane_to=f"{name} ({intl_lane})", rate_per_tonne=intl_pt, truck_capacity_t=1,
                           active=True))
        s.commit()

        # 5) per-market cost params (upsert by key+dest_country)
        for iso in CORRIDORS:
            for key, val in COST.items():
                row = s.exec(select(CostParam).where(CostParam.key == key,
                                                     CostParam.dest_country == iso)).first()
                if not row:
                    row = CostParam(key=key, dest_country=iso)
                row.value = float(val)
                row.unit = "%" if key.endswith("_pct") else "USD"
                s.add(row)
        s.commit()

        print(f"seeded: supplier #{sup.id}, product #{p.id} (EXW ${p.exw_price}/{p.unit}), "
              f"{len(CORRIDORS)} corridors ({', '.join(CORRIDORS)}), cost params per market.")


if __name__ == "__main__":
    run()
