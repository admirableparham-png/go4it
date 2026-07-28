"""Load demo team + catalog + rates + a lead (with matches & an auto quote).

Run with:  make seed   (or:  python -m app.seed)

SAFETY: refuses to wipe an existing database unless SEED_FORCE=1.

Demo logins (change before production!):
    admin@go4it.local / admin123      (admin)
    sara@go4it.local  / manager123    (manager)
    ali@go4it.local   / agent123      (agent)
"""
import hashlib
import os
from datetime import datetime

from sqlmodel import Session, select

from .auth import hash_password
from .config import MATCH_THRESHOLD
from .db import engine, init_db
from .deal_service import create_deal
from .matching import score_lead_product
from .models import (Activity, ComplianceDoc, CostParam, Deal, FxRate,
                     IngestionRun, Lead, Match, Product, Quote, RateCard,
                     Supplier, User)
from .quote_service import create_quote

USERS = [
    dict(email="admin@go4it.local", name="Owner", role="admin", password="admin123"),
    dict(email="sara@go4it.local", name="Sara (manager)", role="manager", password="manager123"),
    dict(email="ali@go4it.local", name="Ali (agent)", role="agent", password="agent123"),
]

SUPPLIERS = [
    dict(name="Isfahan Steel Co", city="Isfahan", reliability=4, payment_terms="30% advance"),
    dict(name="Tehran Cement", city="Tehran", reliability=3),
    dict(name="Pasargad Oil", city="Tabriz", reliability=4),
    dict(name="Kerman Agro", city="Kerman", reliability=5),
]

PRODUCTS = [
    dict(name="Steel rebar 12mm", category="metals", spec="A3 / B500B", hs_code="7214",
         exw_price=590, unit="ton", weight_kg_per_unit=1000, cbm_per_unit=0.13,
         packaging="bundled", min_order_qty=25, origin_region="Isfahan", supplier="Isfahan Steel Co"),
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

COST_PARAMS = [
    ("export_clearance", 250, "USD/shipment"),
    ("coo_fee", 80, "USD/shipment"),
    ("insurance_pct", 0.5, "%"),
    ("financing_pct", 1.0, "%"),
    ("margin_pct", 8, "%"),
    ("margin_floor_pct", 5, "%"),
]
RATE_CARDS = [
    dict(leg="inland", lane_from="Isfahan", lane_to="Bazargan", rate_per_truck=600, truck_capacity_t=25),
    dict(leg="international", lane_from="Bazargan", lane_to="Sadakhlo (GE)", rate_per_truck=800, truck_capacity_t=25),
]

LEAD = dict(product="Steel rebar 12mm", category="metals", spec="grade B500B",
            quantity=100, unit="ton", target_price=700, currency="USD",
            dest_country="GE", dest_city="Tbilisi",
            buyer_company="Kartli Construction LLC", contact_name="G. Beridze",
            email="buyer@kartli.example", source="manual")


def _has_data(session) -> bool:
    for table in (User, Supplier, Product, Lead, Match, Quote, RateCard, CostParam, FxRate):
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
            print("Refusing to seed: database already has data. Use SEED_FORCE=1 to overwrite.")
            return

        for table in (ComplianceDoc, Deal, Quote, Activity, Match, Lead, Product,
                      Supplier, RateCard, CostParam, FxRate, IngestionRun, User):
            for row in s.exec(select(table)).all():
                s.delete(row)
        s.commit()

        # users
        users = {}
        for spec in USERS:
            u = User(email=spec["email"], name=spec["name"], role=spec["role"],
                     password_hash=hash_password(spec["password"]))
            s.add(u)
            s.commit()
            s.refresh(u)
            users[spec["role"]] = u

        # rates
        for key, value, unit in COST_PARAMS:
            s.add(CostParam(key=key, value=value, unit=unit, dest_country="GE"))
        for card in RATE_CARDS:
            s.add(RateCard(**card))
        s.commit()

        # suppliers + products
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

        # lead (owned by the agent) + matches + auto quote
        lead = Lead(**LEAD)
        lead.owner_id = users["agent"].id
        lead.content_hash = _content_hash(lead)
        s.add(lead)
        s.commit()
        s.refresh(lead)
        lead.tracking_code = f"G4-{datetime.utcnow():%Y%m}-{lead.id:04d}"
        s.add(lead)
        s.commit()
        s.refresh(lead)

        matches = []
        for product in s.exec(select(Product).where(Product.active == True)).all():  # noqa: E712
            score, reasons = score_lead_product(lead, product)
            if score >= MATCH_THRESHOLD:
                s.add(Match(lead_id=lead.id, product_id=product.id, score=score, reasons=reasons))
                matches.append((product, score))
        s.commit()

        if matches:
            matches.sort(key=lambda t: t[1], reverse=True)
            create_quote(s, lead, matches[0][0])

        # A second, WON lead -> open Deal + compliance docs (demonstrates the gate:
        # certificate of origin is verified but the commercial invoice is not, so
        # advancing to 'export_cleared' is blocked).
        won = Lead(product="Bitumen 60/70", category="petrochemicals", spec="penetration 60/70",
                   quantity=400, unit="ton", target_price=500, currency="USD",
                   dest_country="TR", dest_city="Istanbul", buyer_company="Istanbul Import Co",
                   contact_name="A. Yilmaz", email="buyer3@example.tr", source="manual",
                   status="won", owner_id=users["agent"].id)
        won.content_hash = _content_hash(won)
        s.add(won)
        s.commit()
        s.refresh(won)
        won.tracking_code = f"G4-{datetime.utcnow():%Y%m}-{won.id:04d}"
        s.add(won)
        s.commit()
        s.refresh(won)
        for product in s.exec(select(Product).where(Product.active == True)).all():  # noqa: E712
            score, reasons = score_lead_product(won, product)
            if score >= MATCH_THRESHOLD:
                s.add(Match(lead_id=won.id, product_id=product.id, score=score, reasons=reasons))
        s.commit()
        bitumen = s.exec(select(Product).where(Product.name == "Bitumen 60/70")).first()
        wq = create_quote(s, won, bitumen) if bitumen else None
        if wq:
            wq.status = "sent"
            s.add(wq)
            s.commit()
            s.refresh(wq)
        deal = create_deal(s, won, wq)
        deal.stage = "supplier_confirmed"
        s.add(deal)
        s.add(ComplianceDoc(deal_id=deal.id, doc_type="certificate_of_origin",
                            reference_no="CoO-TR-9001", issued_by="Tabriz CoC", status="verified"))
        s.add(ComplianceDoc(deal_id=deal.id, doc_type="commercial_invoice",
                            reference_no="INV-2207", status="received"))
        s.commit()

    print(f"Seeded {len(USERS)} users, {len(SUPPLIERS)} suppliers, {len(PRODUCTS)} products, "
          f"2 leads, {len(matches)} matches, 2 quotes, 1 won deal.")
    print("Login: admin@go4it.local/admin123 · sara@go4it.local/manager123 · ali@go4it.local/agent123")


if __name__ == "__main__":
    run()
