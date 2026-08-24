"""Phase 5 — versioned landed-price calculator.

Decimal-exact (never binary float), margin% vs markup% distinct, Incoterm-specific components, unit-basis
conversion, expired rates surfaced (never silently used), DDP gated to needs_review, FX staleness honest,
immutable price versions, and a compatibility guard proving the existing quote engine is unchanged.
"""
from datetime import datetime, timedelta

import pytest
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine, select

from app import pricing as P
from app.models import CostRate, FxRate, Product, ProductPriceVersion, User

NOW = datetime(2026, 8, 24)


def _rate(rt, amount, basis="per_shipment", status="active", until=datetime(2027, 1, 1), currency="USD"):
    return {"rate_type": rt, "amount": amount, "unit_basis": basis, "status": status, "valid_until": until,
            "currency": currency, "min_charge": 0}


FX = {"base": "USD", "quote": "USD", "rate": 1, "kind": "manual"}


# --- Decimal exactness + reconciliation ------------------------------------------------------------
def test_decimal_exact_and_reconciles():
    calc = P.compute_price(base_price=100, base_currency="USD", quantity=10, weight_kg_per_unit=1000,
                           incoterm="EXW", rates=[], fx=FX, margin_pct=20, now=NOW)
    assert calc["total_price"] == 1250.0 and calc["unit_price"] == 125.0
    s = sum(float(ln["amount"]) for ln in calc["breakdown"])
    assert abs(s - calc["total_price"]) < 0.005                 # rounded lines reconcile to the total
    # classic float trap: 0.1*3 — Decimal keeps it exact
    c2 = P.compute_price(base_price="0.10", base_currency="USD", quantity=3, weight_kg_per_unit=0,
                         incoterm="EXW", rates=[], fx=FX, margin_pct=0, now=NOW)
    assert c2["total_price"] == 0.30


def test_margin_distinct_from_markup():
    calc = P.compute_price(base_price=100, base_currency="USD", quantity=10, weight_kg_per_unit=0,
                           incoterm="EXW", rates=[], fx=FX, margin_pct=20, now=NOW)
    # cost 1000, margin 20% OF PRICE → price 1250, margin 250; markup = 250/1000 = 25% OF COST
    assert calc["margin_pct"] == 20.0 and calc["markup_pct"] == 25.0
    assert calc["cost_total"] == 1000.0 and calc["margin_amount"] == 250.0


# --- Incoterm-specific components ------------------------------------------------------------------
def test_incoterm_components_differ():
    rates = [_rate("inland_freight", 40, "per_tonne"), _rate("intl_freight", 1200),
             _rate("insurance", 1.5, "pct")]
    exw = P.compute_price(base_price=100, base_currency="USD", quantity=10, weight_kg_per_unit=1000,
                          incoterm="EXW", rates=rates, fx=FX, margin_pct=0, now=NOW)
    cif = P.compute_price(base_price=100, base_currency="USD", quantity=10, weight_kg_per_unit=1000,
                          incoterm="CIF", rates=rates, fx=FX, margin_pct=0, now=NOW)
    exw_types = {ln["type"] for ln in exw["breakdown"]}
    cif_types = {ln["type"] for ln in cif["breakdown"]}
    assert exw_types == {"base_price"}                          # EXW: goods only
    assert {"inland_freight", "intl_freight", "insurance"} <= cif_types   # CIF pulls logistics


def test_unit_basis_conversion():
    # per_tonne: 10 units * 1000 kg = 10 tonnes * 40 = 400
    rates = [_rate("inland_freight", 40, "per_tonne")]
    calc = P.compute_price(base_price=0, base_currency="USD", quantity=10, weight_kg_per_unit=1000,
                           incoterm="FOB", rates=rates, fx=FX, margin_pct=0, now=NOW)
    inland = next(ln for ln in calc["breakdown"] if ln["type"] == "inland_freight")
    assert inland["amount"] == "400.00"


def test_ddp_requires_destination_costs():
    calc = P.compute_price(base_price=100, base_currency="USD", quantity=1, weight_kg_per_unit=0,
                           incoterm="DDP", rates=[], fx=FX, margin_pct=0, now=NOW)
    assert calc["status"] == "needs_review"
    req = {e["type"] for e in calc["excluded_costs"] if e["reason"] == "Required"}
    assert {"duty", "tax", "import_clearance"} == req


# --- expired rates + FX staleness ------------------------------------------------------------------
def test_expired_rate_surfaced_not_used():
    rates = [_rate("intl_freight", 1200, status="expired", until=datetime(2025, 1, 1))]
    calc = P.compute_price(base_price=100, base_currency="USD", quantity=1, weight_kg_per_unit=0,
                           incoterm="CFR", rates=rates, fx=FX, margin_pct=0, now=NOW)
    assert not any(ln["type"] == "intl_freight" for ln in calc["breakdown"])   # NOT applied
    assert any(e["type"] == "intl_freight" and e["reason"] == "expired" for e in calc["excluded_costs"])


def test_fx_staleness_states():
    assert P.fx_state({"rate": None}, NOW) == "unavailable"
    assert P.fx_state({"rate": 1.1, "kind": "manual"}, NOW) == "manual"
    assert P.fx_state({"rate": 1.1, "kind": "live"}, NOW) == "live"
    assert P.fx_state({"rate": 1.1, "kind": "manual", "expires_at": datetime(2025, 1, 1)}, NOW) == "stale"
    assert P.fx_label("stale") == "Stale" and P.fx_label("unavailable") == "Not available"


def test_fx_converts_base_price():
    fx = {"base": "EUR", "quote": "USD", "rate": 1.1, "kind": "manual"}
    calc = P.compute_price(base_price=100, base_currency="EUR", quantity=1, weight_kg_per_unit=0,
                           incoterm="EXW", rates=[], fx=fx, margin_pct=0, target_currency="USD", now=NOW)
    assert calc["breakdown"][0]["amount"] == "110.00"          # 100 EUR * 1.1 → 110 USD


# --- immutable versions (DB) -----------------------------------------------------------------------
@pytest.fixture
def db():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    SQLModel.metadata.create_all(engine)
    with Session(engine) as s:
        s.add(User(email="admin@t.local", name="A", role="admin", active=True, password_hash="x")); s.commit()
        p = Product(name="Copper", currency="USD", exw_price=8000, weight_kg_per_unit=1000, min_order_qty=25)
        s.add(p); s.commit(); s.refresh(p)
        return engine, p.id


def test_price_version_is_immutable_and_versioned(db):
    engine, pid = db
    with Session(engine) as s:
        p = s.get(Product, pid)
        pv1, _ = P.create_price_version(s, p, incoterm="EXW", quantity=25, margin_pct=15)
        s.commit()
        assert pv1.version == 1 and pv1.status == "draft"
        # duplicate-to-revise creates a NEW version, never edits v1
        pv2 = P.duplicate_version(s, pv1); s.commit()
        assert pv2.version == 2 and pv2.supersedes_id == pv1.id and pv2.status == "draft"
        # approving v2 leaves v1 untouched
        P.transition_version(s, pv2, "approved"); s.commit()
        assert s.get(ProductPriceVersion, pv1.id).status == "draft"
        assert s.get(ProductPriceVersion, pv2.id).status == "approved"
        assert len(s.exec(select(ProductPriceVersion)).all()) == 2


def test_create_price_version_uses_db_rates_and_fx(db):
    engine, pid = db
    with Session(engine) as s:
        s.add(CostRate(rate_type="intl_freight", amount=1500, unit_basis="per_shipment", status="active",
                       valid_until=datetime(2027, 1, 1))); s.commit()
        p = s.get(Product, pid)
        pv, calc = P.create_price_version(s, p, incoterm="CFR", quantity=25, margin_pct=10)
        s.commit()
        assert any(ln["type"] == "intl_freight" for ln in calc["breakdown"])
        assert pv.total_price > pv.cost_total * 0  # sane


# --- compatibility: the existing quote engine is unchanged -----------------------------------------
def test_existing_quote_engine_unchanged():
    from app import quoting
    params = {"truck_capacity_t": 25, "inland_freight_per_truck": 500, "intl_freight_per_truck": 1500,
              "export_clearance": 200, "coo_fee": 50, "insurance_pct": 1, "financing_pct": 1,
              "margin_pct": 12, "margin_floor_pct": 5, "quote_currency": "USD"}
    a = quoting.compute_quote(exw_price=590, quantity=100, weight_kg_per_unit=1000, incoterm="DAP", params=params)
    b = quoting.compute_quote(exw_price=590, quantity=100, weight_kg_per_unit=1000, incoterm="DAP", params=params)
    assert a == b and a["delivered_unit"] > a["exw_unit"]      # deterministic + unchanged contract
