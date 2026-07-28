"""Database glue for quoting: resolve rate/cost params from the DB into a
snapshot, then build and persist a Quote. Used by both the web routes and seed.
"""
import json
from datetime import datetime

from sqlmodel import select

from .models import CostParam, FxRate, Lead, Product, Quote, RateCard
from .quoting import compute_quote

# Fallbacks if the DB has no rows yet, so the engine always produces something.
DEFAULT_PARAMS = {
    "truck_capacity_t": 25,
    "inland_freight_per_truck": 0,
    "intl_freight_per_truck": 0,
    "export_clearance": 0,
    "coo_fee": 0,
    "insurance_pct": 0,
    "financing_pct": 0,
    "margin_pct": 8,
    "margin_floor_pct": 5,
}


def fx_rate(session, base: str, quote: str = "USD") -> float:
    if not base or base == quote:
        return 1.0
    row = session.exec(
        select(FxRate).where(FxRate.base == base, FxRate.quote == quote)
    ).first()
    return float(row.rate) if row else 1.0


def build_params(session, quote_currency: str = "USD") -> dict:
    """Assemble the pricing-parameter snapshot from the DB (CostParams + RateCards)."""
    params = dict(DEFAULT_PARAMS)
    for cp in session.exec(select(CostParam)).all():
        params[cp.key] = float(cp.value)

    inland = session.exec(
        select(RateCard).where(RateCard.leg == "inland", RateCard.active == True)  # noqa: E712
    ).first()
    intl = session.exec(
        select(RateCard).where(RateCard.leg == "international", RateCard.active == True)  # noqa: E712
    ).first()
    if inland:
        params["inland_freight_per_truck"] = float(inland.rate_per_truck)
    if intl:
        params["intl_freight_per_truck"] = float(intl.rate_per_truck)
        params["truck_capacity_t"] = float(intl.truck_capacity_t or 25)

    params["quote_currency"] = quote_currency
    params["dest_border"] = intl.lane_to if intl else ""
    return params


def create_quote(session, lead: Lead, product: Product, incoterm: str = "DAP") -> Quote:
    """Compute and persist a draft Quote for a lead/product, freezing the params."""
    params = build_params(session)
    fx = fx_rate(session, product.currency, "USD")
    quantity = lead.quantity or product.min_order_qty or 1

    result = compute_quote(
        exw_price=product.exw_price,
        quantity=quantity,
        weight_kg_per_unit=product.weight_kg_per_unit,
        incoterm=incoterm,
        params=params,
        fx=fx,
    )

    version = len(session.exec(select(Quote).where(Quote.lead_id == lead.id)).all()) + 1
    quote = Quote(
        lead_id=lead.id,
        product_id=product.id,
        quantity=result["quantity"],
        incoterm=result["incoterm"],
        dest_border=params.get("dest_border", ""),
        quote_currency="USD",
        exw_unit=result["exw_unit"],
        exw_total=result["exw_total"],
        delivered_unit=result["delivered_unit"],
        delivered_total=result["delivered_total"],
        margin_pct=result["margin_pct"],
        breakdown=json.dumps(result["breakdown"]),
        params_snapshot=json.dumps(params),
        fx_snapshot=json.dumps({"base": product.currency, "quote": "USD", "rate": fx}),
        status="draft",
        version=version,
    )
    session.add(quote)
    session.commit()
    session.refresh(quote)
    quote.tracking_code = f"{lead.tracking_code}-Q{version}"
    session.add(quote)
    session.commit()
    session.refresh(quote)
    return quote
