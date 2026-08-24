"""Database glue for quoting: resolve rate/cost params from the DB into a
snapshot, then build and persist a Quote. Used by both the web routes and seed.
"""
import json
import logging
from datetime import datetime, timedelta

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
        owner_id=lead.owner_id,            # tenant scope: a quote belongs to whoever owns the lead
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


# --------------------------------------------------------------------- Phase 6: immutable versions
def _snapshot_fields(session, quote) -> dict:
    """The buyer-facing + internal frozen terms of a quote, for the immutable QuoteVersion."""
    product = session.get(Product, quote.product_id) if quote.product_id else None
    lead = session.get(Lead, quote.lead_id) if quote.lead_id else None
    prod_snap = {}
    if product:
        prod_snap = {"id": product.id, "name": product.name, "sku": product.sku,
                     "description": product.short_description or product.spec, "spec": product.spec,
                     "hs_code": product.hs_code, "origin": product.origin_country or product.origin_region,
                     "packaging": product.packaging, "lead_time_days": product.lead_time_days,
                     "unit": product.unit, "updated_at": product.updated_at.isoformat() if product.updated_at else ""}
    return {
        "quote_id": quote.id, "version": quote.version, "status": quote.status,
        "product_id": quote.product_id, "product_snapshot": prod_snap,
        "quantity": quote.quantity, "unit": (product.unit if product else ""),
        "unit_price": quote.delivered_unit, "currency": quote.quote_currency, "incoterm": quote.incoterm,
        "origin": (product.origin_country or product.origin_region) if product else "",
        "destination": quote.dest_border or (lead.dest_country if lead else ""),
        "total": quote.delivered_total, "margin_pct": quote.margin_pct,
        "fx_snapshot": quote.fx_snapshot, "params_snapshot": quote.params_snapshot,
        "breakdown": quote.breakdown,
    }


def content_hash(fields: dict) -> str:
    import hashlib
    payload = json.dumps({k: fields.get(k) for k in
                          ("quote_id", "version", "unit_price", "total", "currency", "incoterm",
                           "quantity", "destination", "product_snapshot")}, sort_keys=True, default=str)
    return hashlib.sha256(payload.encode()).hexdigest()


def ensure_version(session, quote, *, inferred=False, actor=None):
    """Get-or-create the IMMUTABLE QuoteVersion for a Quote row (1:1 — the legacy model already makes a new
    Quote per version). Sets quote.current_version_id. Idempotent. Never edits an existing version."""
    from .models import QuoteVersion
    existing = session.exec(select(QuoteVersion).where(QuoteVersion.quote_id == quote.id)
                            .order_by(QuoteVersion.version.desc())).first()
    if existing:
        if quote.current_version_id != existing.id:
            quote.current_version_id = existing.id; session.add(quote)
        return existing
    f = _snapshot_fields(session, quote)
    # legacy quoting treats margin_pct as a MARKUP on the cost subtotal (delivered = subtotal*(1+pct/100)),
    # so record markup = that value and derive the true gross MARGIN (% of price) — distinct numbers.
    legacy_markup = f.get("margin_pct") or 0.0
    gross_margin = round(legacy_markup / (100.0 + legacy_markup) * 100.0, 2) if legacy_markup else 0.0
    ver = QuoteVersion(
        quote_id=quote.id, version=quote.version, status=quote.status, product_id=quote.product_id,
        product_snapshot=json.dumps(f["product_snapshot"]), quantity=f["quantity"], unit=f["unit"],
        unit_price=f["unit_price"], currency=f["currency"], incoterm=f["incoterm"], origin=f["origin"],
        destination=f["destination"], total=f["total"], margin_pct=gross_margin, markup_pct=legacy_markup,
        fx_snapshot=f["fx_snapshot"], params_snapshot=f["params_snapshot"],
        validity_at=((quote.created_at + timedelta(days=quote.validity_days)) if quote.created_at else None),
        content_hash=content_hash(f), created_by=getattr(actor, "email", "") or quote.created_by,
        inferred=inferred)
    session.add(ver); session.flush()
    quote.current_version_id = ver.id; session.add(quote)
    return ver


def revise_quote(session, quote, actor=None) -> Quote:
    """Duplicate a quote into a NEW draft version (never edits the accepted/sent/approved/expired one).
    Creates a fresh Quote row (version+1) + its immutable QuoteVersion. Returns the new draft Quote."""
    lead = session.get(Lead, quote.lead_id)
    version = len(session.exec(select(Quote).where(Quote.lead_id == quote.lead_id)).all()) + 1
    dup = Quote(lead_id=quote.lead_id, owner_id=quote.owner_id, product_id=quote.product_id,
                quantity=quote.quantity, incoterm=quote.incoterm, dest_border=quote.dest_border,
                quote_currency=quote.quote_currency, exw_unit=quote.exw_unit, exw_total=quote.exw_total,
                delivered_unit=quote.delivered_unit, delivered_total=quote.delivered_total,
                margin_pct=quote.margin_pct, breakdown=quote.breakdown, params_snapshot=quote.params_snapshot,
                fx_snapshot=quote.fx_snapshot, validity_days=quote.validity_days, status="draft",
                version=version, created_by=getattr(actor, "email", "") or "")
    session.add(dup); session.commit(); session.refresh(dup)
    dup.tracking_code = f"{lead.tracking_code}-Q{version}" if lead else f"Q{version}"
    session.add(dup); session.commit(); session.refresh(dup)
    ver = ensure_version(session, dup, actor=actor)
    ver.supersedes_id = quote.current_version_id; session.add(ver)
    session.commit()
    return dup
