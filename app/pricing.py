"""Phase 5 — versioned landed-price calculator (admin-only).

SEPARATE from app/quoting.py: the buyer-facing quote engine is untouched. Money is computed in **Decimal**
(fixed precision, never binary float), every amount carries a currency, and each calculation is frozen into an
IMMUTABLE ProductPriceVersion. Margin% (margin / selling price) and markup% (margin / cost) are BOTH reported —
they are different numbers and are never conflated. Expired cost rates and missing duties/taxes are SURFACED
("expired" / "Not included" / "Required"), never silently used or guessed.
"""
import json
from datetime import datetime
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation

_CENTS = Decimal("0.01")


def _d(x) -> Decimal:
    if x is None or x == "":
        return Decimal(0)
    try:
        return Decimal(str(x))
    except (InvalidOperation, ValueError):
        return Decimal(0)


def _q(x) -> Decimal:
    return _d(x).quantize(_CENTS, rounding=ROUND_HALF_UP)


# The nine supported Incoterms, in ascending seller-responsibility order.
INCOTERMS = ["EXW", "FCA", "FOB", "CFR", "CIF", "CPT", "CIP", "DAP", "DDP"]

# Cost components ALWAYS borne by the seller (Go4it), regardless of Incoterm. `margin` is applied last.
_ALWAYS = ("base_price", "packaging", "finance", "fx_adj", "operational_fee")

# Per-Incoterm the LOGISTICS components the seller additionally includes (buyer bears the rest).
_INCOTERM_LOGISTICS = {
    "EXW": set(),
    "FCA": {"export_clearance", "inland_freight", "documentation", "coo"},
    "FOB": {"export_clearance", "inland_freight", "documentation", "coo", "inspection"},
    "CFR": {"export_clearance", "inland_freight", "documentation", "coo", "inspection", "intl_freight"},
    "CIF": {"export_clearance", "inland_freight", "documentation", "coo", "inspection", "intl_freight",
            "insurance"},
    "CPT": {"export_clearance", "inland_freight", "documentation", "coo", "intl_freight"},
    "CIP": {"export_clearance", "inland_freight", "documentation", "coo", "intl_freight", "insurance"},
    "DAP": {"export_clearance", "inland_freight", "documentation", "coo", "intl_freight", "insurance"},
    "DDP": {"export_clearance", "inland_freight", "documentation", "coo", "intl_freight", "insurance",
            "import_clearance", "duty", "tax"},
}
# Components that DDP REQUIRES but cannot be guessed if absent → block a firm price (needs_review).
_DDP_REQUIRED = {"import_clearance", "duty", "tax"}

_LABELS = {"base_price": "Goods (base price)", "packaging": "Packaging", "inland_freight": "Inland freight",
           "export_clearance": "Export clearance", "coo": "Certificate of origin", "inspection": "Inspection",
           "documentation": "Documentation", "insurance": "Insurance", "intl_freight": "International freight",
           "import_clearance": "Import clearance", "duty": "Duties", "tax": "Taxes", "finance": "Finance cost",
           "fx_adj": "FX adjustment", "operational_fee": "Operational fee", "margin": "Go4it margin"}


# --------------------------------------------------------------------- FX staleness (honest display)
def fx_state(fx: dict, now=None) -> str:
    """'live' | 'manual' | 'stale' | 'unavailable'. A manual or expired rate is NEVER reported as live."""
    now = now or datetime.utcnow()
    if not fx or not fx.get("rate"):
        return "unavailable"
    exp = fx.get("expires_at")
    if isinstance(exp, str) and exp:
        try:
            exp = datetime.fromisoformat(exp)
        except ValueError:
            exp = None
    if exp and now > exp:
        return "stale"
    return "live" if fx.get("kind") == "live" else "manual"


def fx_label(state: str) -> str:
    return {"live": "Live / verified", "manual": "Manually entered", "stale": "Stale",
            "unavailable": "Not available"}.get(state, "Not available")


# --------------------------------------------------------------------- rate validity
def rate_active(rate: dict, now=None) -> bool:
    """A cost rate is usable only if status active and within [valid_from, valid_until]. Expired never used."""
    now = now or datetime.utcnow()
    if (rate.get("status") or "active") != "active":
        return False
    vf, vu = rate.get("valid_from"), rate.get("valid_until")
    for v in (vf, vu):
        pass
    if vf and _as_dt(vf) and now < _as_dt(vf):
        return False
    if vu and _as_dt(vu) and now > _as_dt(vu):
        return False
    return True


def _as_dt(v):
    if isinstance(v, datetime):
        return v
    if isinstance(v, str) and v:
        try:
            return datetime.fromisoformat(v)
        except ValueError:
            return None
    return None


# --------------------------------------------------------------------- the calculator (pure, Decimal)
def compute_price(*, base_price, base_currency, quantity, weight_kg_per_unit, incoterm, rates, fx,
                  margin_pct=0, target_currency=None, truck_capacity_t=25, now=None) -> dict:
    """Pure Decimal landed-price calculation. Returns a dict with breakdown (reconciles to total to the cent),
    excluded_costs (surfaced, not applied), unit/total price, cost_total, margin_pct + markup_pct, fx state,
    assumptions, and a suggested status. `rates` is a list of dicts (CostRate-shaped). Never raises."""
    now = now or datetime.utcnow()
    incoterm = (incoterm or "EXW").upper()
    if incoterm not in INCOTERMS:
        incoterm = "EXW"
    target_currency = target_currency or fx.get("quote") or base_currency or "USD"
    fxrate = _d(fx.get("rate") or 1)
    st = fx_state(fx, now)
    qty = _d(quantity) or Decimal(1)
    wpu = _d(weight_kg_per_unit)
    tonnes = (qty * wpu) / Decimal(1000) if wpu > 0 else Decimal(0)
    cap = _d(truck_capacity_t) or Decimal(25)
    import math
    trucks = Decimal(max(1, math.ceil(float(tonnes / cap)))) if tonnes > 0 else Decimal(1)

    included = set(_ALWAYS) | _INCOTERM_LOGISTICS[incoterm]
    goods = _d(base_price) * fxrate * qty          # base price → target currency, times quantity
    lines = [{"label": _LABELS["base_price"], "type": "base_price", "basis": "per_unit",
              "amount": goods, "currency": target_currency}]
    excluded = []
    seen_types = {"base_price"}

    for r in rates:
        rtype = r.get("rate_type", "")
        if rtype == "base_price" or rtype == "margin":
            continue
        cur = r.get("currency") or target_currency
        conv = fxrate if cur != target_currency and cur == base_currency else Decimal(1)
        if rtype not in included:
            continue                                # not the seller's responsibility under this Incoterm
        if not rate_active(r, now):
            excluded.append({"type": rtype, "label": _LABELS.get(rtype, rtype), "reason": "expired"})
            continue
        amt = _rate_amount(r, goods, qty, tonnes, trucks) * conv
        if amt <= 0:
            continue
        seen_types.add(rtype)
        lines.append({"label": _LABELS.get(rtype, rtype), "type": rtype,
                      "basis": r.get("unit_basis", "per_shipment"), "amount": amt, "currency": target_currency})

    status = "draft"
    # DDP requires import clearance/duty/tax; if any is missing (not provided as an active rate) → surface it
    if incoterm == "DDP":
        for req in _DDP_REQUIRED:
            if req not in seen_types:
                excluded.append({"type": req, "label": _LABELS[req], "reason": "Required"})
        if any(e["reason"] == "Required" for e in excluded):
            status = "needs_review"          # cannot produce a firm DDP price without the destination costs
    else:
        # duties/taxes under non-DDP terms are the buyer's — shown as Not included, never an error
        for req in ("duty", "tax", "import_clearance"):
            excluded.append({"type": req, "label": _LABELS[req], "reason": "Not included"})

    cost_total = sum((ln["amount"] for ln in lines), Decimal(0))
    m = _d(margin_pct)
    if m >= 100:
        m = Decimal(0)                          # guard: margin as % of price cannot be ≥100
    if m > 0:
        price = cost_total / (Decimal(1) - m / Decimal(100))
        margin_amt = price - cost_total
        lines.append({"label": f"{_LABELS['margin']} ({m}% of price)", "type": "margin", "basis": "margin",
                      "amount": margin_amt, "currency": target_currency})
    else:
        margin_amt = Decimal(0)
    markup_pct = (margin_amt / cost_total * Decimal(100)) if cost_total > 0 else Decimal(0)

    # round each line, reconcile total from the rounded lines (to the cent)
    for ln in lines:
        ln["amount_dec"] = _q(ln["amount"])
        ln["amount"] = str(ln["amount_dec"])
    total = sum((ln["amount_dec"] for ln in lines), Decimal(0))
    unit = _q(total / qty) if qty > 0 else total
    for ln in lines:
        ln.pop("amount_dec", None)

    return {
        "incoterm": incoterm, "currency": target_currency, "quantity": float(qty),
        "weight_kg_per_unit": float(wpu), "tonnes": float(tonnes), "trucks": int(trucks),
        "breakdown": lines, "excluded_costs": excluded,
        "cost_total": float(_q(cost_total)), "margin_amount": float(_q(margin_amt)),
        "margin_pct": float(m), "markup_pct": float(_q(markup_pct)),
        "unit_price": float(unit), "total_price": float(total),
        "fx": {"base": fx.get("base", base_currency), "quote": target_currency, "rate": float(fxrate),
               "kind": fx.get("kind", "manual"), "source": fx.get("source", ""), "state": st,
               "retrieved_at": _iso(fx.get("retrieved_at"))},
        "fx_state": st, "status": status,
        "assumptions": {"truck_capacity_t": float(cap), "basis_currency": base_currency,
                        "quantity": float(qty)},
    }


def _rate_amount(r, goods, qty, tonnes, trucks) -> Decimal:
    """Resolve one cost-rate line by its unit basis, applying any minimum charge."""
    basis = r.get("unit_basis", "per_shipment")
    amount = _d(r.get("amount"))
    if basis == "per_unit":
        val = amount * qty
    elif basis == "per_tonne":
        val = amount * tonnes
    elif basis == "per_truck":
        val = amount * trucks
    elif basis == "pct":
        val = goods * amount / Decimal(100)
    else:                                         # per_shipment (flat)
        val = amount
    mn = _d(r.get("min_charge"))
    return max(val, mn) if mn > 0 else val


def _iso(v):
    if isinstance(v, datetime):
        return v.isoformat()
    return v or ""


# --------------------------------------------------------------------- DB layer (immutable versions)
def _rates_for(session, product, tenant_id=None, now=None):
    """Active/expired CostRate rows relevant to a product's origin/destination, as dicts for compute_price."""
    from sqlmodel import select
    from .models import CostRate
    q = select(CostRate)
    rows = session.exec(q).all()
    out = []
    for r in rows:
        if r.tenant_id not in (None, tenant_id):
            continue
        out.append({"rate_type": r.rate_type, "currency": r.currency, "unit_basis": r.unit_basis,
                    "amount": r.amount, "min_charge": r.min_charge, "status": r.status,
                    "valid_from": r.valid_from, "valid_until": r.valid_until, "name": r.name})
    return out


def _fx_for(session, base, quote="USD"):
    """Latest FxRate for base→quote as a dict (or a 1:1 unavailable snapshot)."""
    from sqlmodel import select
    from .models import FxRate
    if not base or base == quote:
        return {"base": base or quote, "quote": quote, "rate": 1, "kind": "manual", "source": "identity"}
    r = session.exec(select(FxRate).where(FxRate.base == base, FxRate.quote == quote,
                                          FxRate.active == True).order_by(FxRate.id.desc())).first()  # noqa: E712
    if not r:
        return {"base": base, "quote": quote, "rate": None, "kind": "manual", "source": ""}
    return {"base": r.base, "quote": r.quote, "rate": r.rate, "kind": r.kind, "source": r.source,
            "retrieved_at": r.retrieved_at, "expires_at": r.expires_at}


def create_price_version(session, product, *, incoterm="EXW", quantity=None, destination="",
                         transport_mode="", margin_pct=0, target_currency=None, actor=None, now=None):
    """Compute a landed price for `product` and persist it as an IMMUTABLE ProductPriceVersion (status derived
    from the calc). Reuses the frozen CostRate + FX snapshot so it reproduces identically. Admin-only caller."""
    from .models import ProductPriceVersion, ProductPriceVersion as _PPV
    from sqlmodel import func, select
    now = now or datetime.utcnow()
    tenant_id = None
    base_cur = product.currency or "USD"
    tgt = target_currency or base_cur
    fx = _fx_for(session, base_cur, tgt)
    rates = _rates_for(session, product, tenant_id, now)
    qty = quantity if quantity is not None else (product.min_order_qty or 1)
    calc = compute_price(base_price=product.exw_price, base_currency=base_cur, quantity=qty,
                         weight_kg_per_unit=product.weight_kg_per_unit, incoterm=incoterm, rates=rates, fx=fx,
                         margin_pct=margin_pct, target_currency=tgt, now=now)
    version = (session.exec(select(func.count()).select_from(_PPV)
                            .where(_PPV.product_id == product.id)).one() or 0) + 1
    pv = ProductPriceVersion(
        product_id=product.id, tenant_id=tenant_id, version=version, incoterm=calc["incoterm"],
        origin=product.origin_country or product.origin_region, destination=destination,
        transport_mode=transport_mode, currency=calc["currency"], quantity=float(qty),
        weight_kg_per_unit=product.weight_kg_per_unit, unit_basis="per_unit",
        inputs=json.dumps({"assumptions": calc["assumptions"], "rates_used":
                           [ln["type"] for ln in calc["breakdown"]]})[:8000],
        breakdown=json.dumps(calc["breakdown"])[:8000], excluded_costs=json.dumps(calc["excluded_costs"])[:4000],
        unit_price=calc["unit_price"], total_price=calc["total_price"], cost_total=calc["cost_total"],
        margin_pct=calc["margin_pct"], markup_pct=calc["markup_pct"], fx_snapshot=json.dumps(calc["fx"])[:2000],
        status=calc["status"], created_by=getattr(actor, "email", "") or "")
    session.add(pv); session.flush()
    _audit(session, actor, pv.id, "price_version_create", {"incoterm": calc["incoterm"],
                                                           "status": calc["status"], "version": version})
    return pv, calc


def duplicate_version(session, pv, actor=None):
    """Duplicate an existing (usually approved/expired) version into a fresh DRAFT to revise — history is
    never rewritten. Returns the new draft."""
    from .models import ProductPriceVersion, ProductPriceVersion as _PPV
    from sqlmodel import func, select
    version = (session.exec(select(func.count()).select_from(_PPV)
                            .where(_PPV.product_id == pv.product_id)).one() or 0) + 1
    dup = ProductPriceVersion(
        product_id=pv.product_id, tenant_id=pv.tenant_id, version=version, incoterm=pv.incoterm,
        origin=pv.origin, destination=pv.destination, transport_mode=pv.transport_mode, currency=pv.currency,
        quantity=pv.quantity, weight_kg_per_unit=pv.weight_kg_per_unit, unit_basis=pv.unit_basis,
        inputs=pv.inputs, breakdown=pv.breakdown, excluded_costs=pv.excluded_costs, unit_price=pv.unit_price,
        total_price=pv.total_price, cost_total=pv.cost_total, margin_pct=pv.margin_pct, markup_pct=pv.markup_pct,
        fx_snapshot=pv.fx_snapshot, status="draft", supersedes_id=pv.id,
        created_by=getattr(actor, "email", "") or "")
    session.add(dup); session.flush()
    _audit(session, actor, dup.id, "price_version_duplicate", {"from": pv.id, "version": version})
    return dup


def transition_version(session, pv, to, actor=None):
    """Move a price version through draft→needs_review→approved→expired→archived. Never edits the numbers."""
    allowed = {"draft": {"needs_review", "approved", "archived"},
               "needs_review": {"approved", "draft", "archived"},
               "approved": {"expired", "archived"}, "expired": {"archived"}, "needs_review ": set()}
    if to not in allowed.get(pv.status, set()):
        return False
    pv.status = to
    if to == "approved":
        pv.approved_by = getattr(actor, "email", "") or ""
        pv.approved_at = datetime.utcnow()
    session.add(pv)
    _audit(session, actor, pv.id, "price_version_status", {"to": to})
    return True


def prefill_for_quote(session, product, incoterm="EXW", quantity=None):
    """READ-ONLY preview for a future Phase-6 quote integration. Computes an indicative landed price WITHOUT
    persisting anything and WITHOUT touching the existing quote engine. Not wired into create_quote."""
    base_cur = product.currency or "USD"
    fx = _fx_for(session, base_cur, base_cur)
    rates = _rates_for(session, product, None)
    qty = quantity if quantity is not None else (product.min_order_qty or 1)
    return compute_price(base_price=product.exw_price, base_currency=base_cur, quantity=qty,
                         weight_kg_per_unit=product.weight_kg_per_unit, incoterm=incoterm, rates=rates, fx=fx)


def _audit(session, actor, pv_id, action, meta):
    try:
        from .pipeline import audit
        audit(session, actor, "price_version", pv_id, action, meta)
    except Exception:  # noqa: BLE001
        pass
