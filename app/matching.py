"""The matching engine — score a buyer Lead against a catalog Product.

Text similarity is a *length-aware blend* (Jaccard + lead-coverage + order-tolerant
fuzzy) rather than a bare ``fuzz.token_set_ratio``, whose subset behavior returns
100 whenever the shorter text's tokens are a subset of the longer one's (so a
generic "steel" lead would score a perfect 100 against any longer product string).

Matching is geography-aware: our lanes run Iran → Georgia/Turkey, so a lead whose
destination is on that corridor gets a ranking bonus over an off-corridor one.
"""
import re

from rapidfuzz import fuzz

_TOKEN_RE = re.compile(r"[a-z0-9]+")

# Destinations we actively serve first (Iran -> here). Normalized lowercase.
PRIMARY_CORRIDOR = {"ge", "geo", "georgia", "tr", "turkey", "türkiye", "turkiye"}


def _tokens(text: str) -> set:
    return set(_TOKEN_RE.findall((text or "").lower()))


def text_similarity(a: str, b: str) -> float:
    """Length-aware text similarity in 0-100 (see module docstring)."""
    ta, tb = (a or "").lower(), (b or "").lower()
    sa, sb = _tokens(ta), _tokens(tb)
    if not sa or not sb:
        return 0.0
    inter = len(sa & sb)
    jaccard = inter / len(sa | sb)     # unmatched tokens on either side lower this
    coverage = inter / len(sa)         # how much of the lead's terms the product covers
    fuzzy = fuzz.token_sort_ratio(ta, tb) / 100.0
    return 100.0 * (0.55 * jaccard + 0.25 * coverage + 0.20 * fuzzy)


def on_corridor(country: str) -> bool:
    return (country or "").strip().lower() in PRIMARY_CORRIDOR


def _combine(*parts) -> str:
    return " ".join(p for p in parts if p)


def score_lead_product(lead, product):
    """Return (score, reasons) for a lead/product pair. Score is clamped 0-100.

    Weights (max 100): text 55, category 12, MOQ 8, price 12, corridor 13.
    """
    reasons = []

    sim = text_similarity(
        _combine(lead.product, lead.category, lead.spec),
        _combine(product.name, product.category, product.spec),
    )
    score = sim * 0.55
    reasons.append(f"text {int(round(sim))}%")

    if lead.category and product.category and \
            lead.category.strip().lower() == product.category.strip().lower():
        score += 12
        reasons.append("category match")

    # Minimum order quantity — can the buyer's volume clear the supplier's MOQ?
    if lead.quantity > 0 and product.min_order_qty > 0:
        if lead.quantity >= product.min_order_qty:
            score += 8
            reasons.append("meets MOQ")
        else:
            reasons.append(f"below MOQ ({product.min_order_qty:g} {product.unit})".strip())

    # Price — EXW vs the buyer's target (delivered pricing arrives in Phase 2).
    if lead.target_price > 0 and product.exw_price > 0:
        if product.exw_price <= lead.target_price:
            score += 12
            reasons.append("EXW within budget")
        elif (product.exw_price - lead.target_price) / lead.target_price < 0.10:
            score += 6
            reasons.append("EXW near budget")

    # Geography — prioritize destinations on our Iran -> GE/TR corridor.
    if on_corridor(lead.dest_country):
        score += 13
        reasons.append(f"on {lead.dest_country.strip().upper()} corridor")

    return max(0.0, min(round(score, 1), 100.0)), ", ".join(reasons)
