"""Seed the ZINC SULPHATE offer into go4it so the app can quote it: one priced Product + regional
freight corridors + per-market cost params. Idempotent.

    ./.venv/bin/python scripts/migrate.py          # adds ratecard.dest_country (already run for honey)
    ./.venv/bin/python scripts/seed_zinc_offer.py

Offer (founder-supplied 2026-08-11): Zinc Sulphate Monohydrate, min 33% Zn, agricultural/feed grade,
two independent ISO-17025 lab CoAs (Cd 4-6 ppm, Pb 20 ppm, As nd), 25 kg bags, any quantity,
EXW ~$1.00/kg Iran (shipping not included). Payment LC / SWIFT / crypto; CoO + CoA available.

WHY THE FREIGHT NUMBERS ARE PLACEHOLDERS: the quote engine (app/quote_service.build_params) is
DESTINATION-scoped, not product-scoped — there is one freight lane per country, shared across products.
Honey already seeded IQ / AE / QA / PK and the legacy GE lane, so this script does NOT re-seed those
five (that would create ambiguous duplicate lanes or overwrite honey). Zinc quotes to IQ/AE/QA/PK/GE
reuse the existing lane; tune per-product at /rates if a precise zinc freight matters there. The cold
email itself (app/outreach.zinc_message) carries NO price — the delivered CPT figure is produced by the
quote engine on demand — so these lanes only need to be roughly sane, not exact. All USD/tonne.
"""
import os
import sys

from sqlmodel import Session, select

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from app.db import engine, init_db  # noqa: E402
from app.models import CostParam, Product, RateCard, Supplier  # noqa: E402

SUPPLIER = "Iran Zinc Sulphate (go4it sourcing)"
PRODUCT = "Zinc Sulphate Monohydrate 33% Zn (agri/feed grade) - Iran"
CATEGORY = "chem-zinc-sulfate"          # keep identical to what any gen_zinc_offer_sheet.py queries

# Dests honey/legacy already own (dest-scoped engine = one lane per country). Never re-seed these.
SHARED = {"IQ", "AE", "QA", "PK", "GE"}

# Zinc-exclusive corridors (USD/tonne): iso -> (name, inland lane, intl lane, inland_pt, intl_pt).
# Regional-first: strongest fit is the landlocked/neighbor cells where Chinese product lands expensive.
CORRIDORS = {
    "AF": ("Afghanistan", "Mashhad -> Dogharoun/Milak border", "-> Herat/Kabul/Kandahar", 70, 110),
    "TR": ("Turkey", "Tabriz -> Bazargan border", "-> Erzurum/Istanbul/Adana", 90, 130),
    "AZ": ("Azerbaijan", "Ardabil -> Astara border", "-> Baku/Ganja", 70, 110),
    "AM": ("Armenia", "Tabriz -> Nordooz/Meghri border", "-> Yerevan", 90, 150),
    "TM": ("Turkmenistan", "Mashhad -> Sarakhs/Incheh Borun border", "-> Ashgabat", 70, 120),
    "UZ": ("Uzbekistan", "Mashhad -> Sarakhs (via Turkmenistan)", "-> Tashkent/Samarkand", 90, 190),
    "TJ": ("Tajikistan", "Mashhad -> transit via UZ/AF", "-> Dushanbe/Khujand", 100, 230),
    "KG": ("Kyrgyzstan", "transit via UZ/KZ", "-> Bishkek/Osh", 110, 250),
    "KZ": ("Kazakhstan", "Gorgan -> Inche Borun / Caspian", "-> Aktau/Almaty/Shymkent", 100, 210),
    "SA": ("Saudi Arabia", "Tehran -> Bandar Abbas", "sea -> Dammam/Jeddah", 90, 180),
    "OM": ("Oman", "Tehran -> Bandar Abbas", "sea -> Sohar/Muscat", 90, 160),
    "KW": ("Kuwait", "Tehran -> Bandar Abbas/Khorramshahr", "sea/land -> Shuwaikh", 90, 170),
    "BH": ("Bahrain", "Tehran -> Bandar Abbas", "sea -> Khalifa Bin Salman", 90, 200),
}
# Per-market cost params (commodity: leaner than honey; founder tunes final margin/price).
COST = {"export_clearance": 120, "coo_fee": 60, "insurance_pct": 0.4,
        "financing_pct": 0.8, "margin_pct": 12, "margin_floor_pct": 6}


def run():
    init_db()
    with Session(engine) as s:
        # 1) supplier (NOTE: Supplier model has no `note` field — do not pass one)
        sup = s.exec(select(Supplier).where(Supplier.name == SUPPLIER)).first()
        if not sup:
            sup = Supplier(name=SUPPLIER, country="IR", payment_terms="LC / SWIFT / crypto")
            s.add(sup); s.commit(); s.refresh(sup)

        # 2) product (upsert by name)
        p = s.exec(select(Product).where(Product.name == PRODUCT)).first() or Product(name=PRODUCT)
        p.category = CATEGORY
        p.spec = ("Zinc Sulphate Monohydrate (ZnSO4.H2O), min 33% Zn, agricultural / feed grade. "
                  "Two independent ISO-17025 lab CoAs (low heavy metals: Cd 4-6 ppm, Pb 20 ppm, As nd). "
                  "25 kg bags; any quantity, from a 25 kg trial to full containers. Certificate of Origin, "
                  "CoA, commercial invoice and packing list provided. Payment: LC / SWIFT / crypto.")
        p.hs_code = "283329"
        p.exw_price = 1.0
        p.currency = "USD"
        p.unit = "kg"
        p.weight_kg_per_unit = 1.0
        p.packaging = "25 kg bags"
        p.min_order_qty = 25000        # 25 t (a container load) for representative default quoting
        p.origin_region = "Iran"
        p.supplier_id = sup.id
        p.active = True
        s.add(p); s.commit(); s.refresh(p)

        # 3) zinc-exclusive corridors (idempotent: clear this set, re-add). SHARED dests are left to honey.
        seed_isos = [iso for iso in CORRIDORS if iso not in SHARED]
        for c in s.exec(select(RateCard).where(RateCard.dest_country.in_(seed_isos))).all():
            s.delete(c)
        s.commit()
        for iso in seed_isos:
            name, inland_lane, intl_lane, inland_pt, intl_pt = CORRIDORS[iso]
            s.add(RateCard(leg="inland", dest_country=iso, lane_from=inland_lane.split(" -> ")[0],
                           lane_to=inland_lane, rate_per_tonne=inland_pt, truck_capacity_t=25, active=True))
            s.add(RateCard(leg="international", dest_country=iso, lane_from=inland_lane,
                           lane_to=f"{name} ({intl_lane})", rate_per_tonne=intl_pt, truck_capacity_t=25,
                           active=True))
        s.commit()

        # 4) per-market cost params (upsert by key+dest_country) for the zinc-exclusive dests
        for iso in seed_isos:
            for key, val in COST.items():
                row = s.exec(select(CostParam).where(CostParam.key == key,
                                                     CostParam.dest_country == iso)).first()
                if not row:
                    row = CostParam(key=key, dest_country=iso)
                row.value = float(val)
                row.unit = "%" if key.endswith("_pct") else "USD"
                s.add(row)
        s.commit()

        print(f"seeded: supplier #{sup.id}, product #{p.id} (EXW ${p.exw_price}/{p.unit}, {CATEGORY}), "
              f"{len(seed_isos)} zinc corridors ({', '.join(seed_isos)}); "
              f"reused existing lanes for {', '.join(sorted(SHARED))} (tune at /rates if needed).")


if __name__ == "__main__":
    run()
