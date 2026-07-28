"""Tests for the pure quotation engine (no DB, fixed params)."""
from decimal import Decimal

from app.quoting import compute_quote

PARAMS = {
    "truck_capacity_t": 25,
    "inland_freight_per_truck": 600,
    "intl_freight_per_truck": 800,
    "export_clearance": 250,
    "coo_fee": 80,
    "insurance_pct": 0.5,
    "financing_pct": 1.0,
    "margin_pct": 8,
    "margin_floor_pct": 5,
    "quote_currency": "USD",
}


def _quote(**kw):
    base = dict(exw_price=590, quantity=100, weight_kg_per_unit=1000,
                incoterm="DAP", params=PARAMS)
    base.update(kw)
    return compute_quote(**base)


def _breakdown_sum(result):
    return sum(Decimal(row["amount"]) for row in result["breakdown"])


def test_breakdown_reconciles_to_total():
    r = _quote()
    assert abs(_breakdown_sum(r) - Decimal(str(r["delivered_total"]))) < Decimal("0.005")


def test_bigger_order_has_lower_delivered_unit():
    """Per-shipment costs amortize, so a larger order is cheaper per unit."""
    small = _quote(quantity=25)
    big = _quote(quantity=100)
    assert big["delivered_unit"] < small["delivered_unit"]


def test_delivered_is_above_exw():
    r = _quote()
    assert r["delivered_unit"] > r["exw_unit"]
    assert r["delivered_total"] > r["exw_total"]


def test_exw_incoterm_has_no_freight_lines():
    r = _quote(incoterm="EXW")
    labels = " ".join(row["label"].lower() for row in r["breakdown"])
    assert "freight" not in labels and "clearance" not in labels


def test_engine_is_deterministic():
    assert _quote() == _quote()


def test_margin_floor_flag():
    r = _quote(params=dict(PARAMS, margin_pct=3, margin_floor_pct=5))
    assert r["below_margin_floor"] is True


def test_fx_converts_exw():
    r = _quote(exw_price=1000, quantity=10, weight_kg_per_unit=0, incoterm="EXW", fx=0.5)
    assert r["exw_unit"] == 500.00
