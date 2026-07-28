"""The quotation engine — pure functions, no database.

Turns an EXW unit price + quantity into an **EXW** price and a **delivered**
price (CPT/DAP to the destination border), with a transparent cost breakdown.

Design guarantees:
- All arithmetic is in ``Decimal`` and quantized to 2 dp, so no float rounding
  error reaches a buyer-facing pro-forma.
- The breakdown **reconciles exactly** to the delivered total (the total is the
  sum of the rounded line amounts).
- Per-shipment costs (freight, clearance, certificate of origin) are amortized
  over the quantity, so a **bigger order yields a lower delivered unit price**.

``params`` is a plain dict snapshot (so a quote can be frozen and reproduced):
    truck_capacity_t, inland_freight_per_truck, intl_freight_per_truck,
    export_clearance, coo_fee, insurance_pct, financing_pct,
    margin_pct, margin_floor_pct, quote_currency
"""
import math
from decimal import ROUND_HALF_UP, Decimal

_CENTS = Decimal("0.01")
_DELIVERED_TERMS = {"CPT", "CIP", "DAP", "DAF", "DDP"}


def _d(x) -> Decimal:
    return Decimal(str(x if x is not None else 0))


def _q(x) -> Decimal:
    return _d(x).quantize(_CENTS, rounding=ROUND_HALF_UP)


def compute_quote(*, exw_price, quantity, weight_kg_per_unit, incoterm, params, fx=1):
    """Return a quote result dict. See module docstring for ``params`` keys."""
    incoterm = (incoterm or "DAP").upper()
    exw = _d(exw_price) * _d(fx)              # EXW in the quote currency
    qty = _d(quantity)
    wpu = _d(weight_kg_per_unit)

    goods = exw * qty
    tonnes = (qty * wpu) / _d(1000) if wpu > 0 and qty > 0 else _d(0)

    capacity = _d(params.get("truck_capacity_t") or 25)
    trucks = _d(max(1, math.ceil(tonnes / capacity))) if tonnes > 0 else _d(1)

    # (label, basis, raw amount) — goods first, then logistics for delivered terms.
    lines = [("Goods (EXW)", "per-unit", goods)]

    if incoterm in _DELIVERED_TERMS:
        buckets = [
            ("Inland freight (Iran)", "per-shipment",
             _d(params.get("inland_freight_per_truck", 0)) * trucks),
            ("International freight to border", "per-shipment",
             _d(params.get("intl_freight_per_truck", 0)) * trucks),
            ("Export clearance", "per-shipment", _d(params.get("export_clearance", 0))),
            ("Certificate of origin", "per-shipment", _d(params.get("coo_fee", 0))),
            ("Insurance", "per-unit", goods * _d(params.get("insurance_pct", 0)) / _d(100)),
            ("Financing / FX", "per-unit", goods * _d(params.get("financing_pct", 0)) / _d(100)),
        ]
        lines.extend((lbl, basis, amt) for (lbl, basis, amt) in buckets if amt > 0)

    subtotal = sum((amt for _, _, amt in lines), _d(0))
    margin_pct = _d(params.get("margin_pct", 0))
    margin = subtotal * margin_pct / _d(100)
    lines.append((f"Margin ({margin_pct:g}%)", "per-unit", margin))

    # Round each line, then derive totals from the rounded lines so the breakdown
    # reconciles to the total to the cent.
    rounded = [(lbl, basis, _q(amt)) for (lbl, basis, amt) in lines]
    delivered_total = sum((amt for _, _, amt in rounded), _d(0))
    exw_total = _q(goods)
    exw_unit = _q(exw)
    delivered_unit = _q(delivered_total / qty) if qty > 0 else _q(delivered_total)

    breakdown = [
        {
            "label": lbl,
            "basis": basis,
            "amount": f"{amt:.2f}",
            "per_unit": f"{(amt / qty if qty > 0 else amt):.2f}",
        }
        for (lbl, basis, amt) in rounded
    ]

    floor = _d(params.get("margin_floor_pct", 0))
    return {
        "incoterm": incoterm,
        "quantity": float(qty),
        "trucks": int(trucks),
        "tonnes": float(tonnes),
        "quote_currency": params.get("quote_currency", "USD"),
        "exw_unit": float(exw_unit),
        "exw_total": float(exw_total),
        "delivered_unit": float(delivered_unit),
        "delivered_total": float(delivered_total),
        "margin_pct": float(margin_pct),
        "below_margin_floor": margin_pct < floor,
        "breakdown": breakdown,
    }
