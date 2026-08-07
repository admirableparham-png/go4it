"""Database glue for quoting: resolve rate/cost params from the DB into a
snapshot, then build and persist a Quote. Used by both the web routes and seed.
"""
import json
import logging
from datetime import datetime

from sqlmodel import select

from .models import CostParam, FxRate, Lead, Product, Quote, RateCard
from .quoting import compute_quote

logger = logging.getLogger("go4it")

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
    if not row:
        # Fail loud (in logs): silently using 1:1 for a real non-USD price is an
        # order-of-magnitude quoting error. Set a rate in /rates for this currency.
        logger.warning("fx_rate: no FX rate for %s->%s; defaulting to 1.0 — quote will be WRONG "
                       "for a %s-priced product. Add the rate in Rates.", base, quote, base)
        return 1.0
    return float(row.rate)


def _pick_card(session, leg: str, dest_country: str):
    """Active RateCard for this leg: prefer one scoped to dest_country, else fall back to the first
    active lane (legacy single-corridor behavior for markets without a dedicated lane)."""
    cards = session.exec(
        select(RateCard).where(RateCard.leg == leg, RateCard.active == True)  # noqa: E712
    ).all()
    if dest_country:
        for c in cards:
            if c.dest_country == dest_country:
                return c
    return cards[0] if cards else None


def build_params(session, quote_currency: str = "USD", dest_country: str = "") -> dict:
    """Assemble the pricing-parameter snapshot from the DB (CostParams + RateCards), scoped to the
    destination country when a dedicated corridor exists. Backward-compatible: a destination with no
    dedicated CostParams/RateCards falls back to the legacy global set (first active lane, all params)."""
    params = dict(DEFAULT_PARAMS)
    cps = session.exec(select(CostParam)).all()
    dest_cps = [c for c in cps if dest_country and c.dest_country == dest_country]
    for cp in (dest_cps or cps):            # scoped params if this market has them, else legacy: all
        params[cp.key] = float(cp.value)

    inland = _pick_card(session, "inland", dest_country)
    intl = _pick_card(session, "international", dest_country)
    if inland:
        params["inland_freight_per_truck"] = float(inland.rate_per_truck)
        params["inland_freight_per_tonne"] = float(inland.rate_per_tonne)
    if intl:
        params["intl_freight_per_truck"] = float(intl.rate_per_truck)
        params["intl_freight_per_tonne"] = float(intl.rate_per_tonne)
        params["truck_capacity_t"] = float(intl.truck_capacity_t or 25)

    params["quote_currency"] = quote_currency
    params["dest_border"] = intl.lane_to if intl else ""
    return params


def create_quote(session, lead: Lead, product: Product, incoterm: str = "DAP") -> Quote:
    """Compute and persist a draft Quote for a lead/product, freezing the params."""
    params = build_params(session, dest_country=lead.dest_country)
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
