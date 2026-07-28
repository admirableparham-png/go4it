"""The matching engine.

Given a buyer's Demand and a seller's Offer, produce a 0-100 score plus a
plain-language reason string. Starts rule-based + fuzzy text so it works today;
easy to extend with semantic/AI matching later without touching the callers.
"""
from rapidfuzz import fuzz


def _text(x) -> str:
    """Combine the descriptive fields into one lowercase string for comparison."""
    return " ".join(p for p in (x.product, x.category, x.spec) if p).lower()


def score_pair(demand, offer):
    """Return (score, reasons) for a demand/offer pair. Score is 0-100."""
    reasons = []

    # 1) Text similarity — the main signal. token_set_ratio is order/duplication
    #    tolerant, so "steel rebar 12mm" ~ "rebar steel 12mm grade" scores high.
    sim = fuzz.token_set_ratio(_text(demand), _text(offer))
    score = sim * 0.6
    reasons.append(f"text {int(sim)}%")

    # 2) Same category is a strong confirmation.
    if demand.category and offer.category and demand.category.lower() == offer.category.lower():
        score += 15
        reasons.append("category match")

    # 3) Quantity — does the seller have enough to cover the buyer?
    if demand.quantity and offer.quantity:
        if offer.quantity >= demand.quantity:
            score += 10
            reasons.append("quantity covered")
        else:
            ratio = offer.quantity / demand.quantity
            score += 10 * ratio
            reasons.append(f"partial qty {int(ratio * 100)}%")

    # 4) Price — is the seller within (or near) the buyer's budget?
    if demand.target_price and offer.price:
        if offer.price <= demand.target_price:
            score += 15
            reasons.append("within budget")
        elif (offer.price - demand.target_price) / demand.target_price < 0.10:
            score += 8
            reasons.append("near budget")

    # 5) Location — a light bonus when the seller is where the buyer wants.
    if demand.location and offer.location and demand.location.lower() in offer.location.lower():
        score += 5
        reasons.append("location match")

    return min(round(score, 1), 100.0), ", ".join(reasons)
