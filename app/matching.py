"""The matching engine.

Given a buyer's Demand and a seller's Offer, produce a 0-100 score plus a
plain-language reason string. Rule-based + fuzzy so it works today; easy to
extend with semantic/AI matching later without touching the callers.

Text similarity is a *length-aware blend* on purpose. A bare
``fuzz.token_set_ratio`` returns 100 whenever the shorter text's tokens are a
subset of the longer one's, so a generic demand ("steel") would score a perfect
100 against any unrelated longer offer ("steel wire mesh galvanized"). The blend
below (Jaccard + demand-coverage + order-tolerant fuzzy) keeps order/duplication
tolerance while making extra, unmatched tokens actually lower the score.
"""
import re

from rapidfuzz import fuzz

_TOKEN_RE = re.compile(r"[a-z0-9]+")
_WORD_RE = re.compile(r"[a-z]+")


def _text(x) -> str:
    """Descriptive fields combined into one lowercase string."""
    return " ".join(p for p in (x.product, x.category, x.spec) if p).lower()


def _text_similarity(demand, offer) -> float:
    """Length-aware text similarity in 0-100 (see module docstring)."""
    ta, tb = _text(demand), _text(offer)
    a = set(_TOKEN_RE.findall(ta))
    b = set(_TOKEN_RE.findall(tb))
    if not a or not b:
        return 0.0
    inter = len(a & b)
    jaccard = inter / len(a | b)          # penalizes unmatched tokens on either side
    coverage = inter / len(a)             # how much of the demand's terms the offer covers
    fuzzy = fuzz.token_sort_ratio(ta, tb) / 100.0  # order-tolerant, length-aware
    return 100.0 * (0.55 * jaccard + 0.25 * coverage + 0.20 * fuzzy)


def _location_match(a: str, b: str) -> bool:
    """True if the two locations share a whole word token.

    Whole-word (not substring) so 'Oman' never matches inside 'Romania'.
    """
    wa = set(_WORD_RE.findall(a.lower()))
    wb = set(_WORD_RE.findall(b.lower()))
    return bool(wa & wb)


def score_pair(demand, offer):
    """Return (score, reasons) for a demand/offer pair. Score is clamped 0-100."""
    reasons = []

    # 1) Text similarity — the main signal (length-aware blend).
    sim = _text_similarity(demand, offer)
    score = sim * 0.6
    reasons.append(f"text {int(round(sim))}%")

    # 2) Same category confirms the match.
    if demand.category and offer.category and \
            demand.category.strip().lower() == offer.category.strip().lower():
        score += 15
        reasons.append("category match")

    # 3) Quantity — does the seller have enough to cover the buyer? (positive only)
    if demand.quantity > 0 and offer.quantity > 0:
        if offer.quantity >= demand.quantity:
            score += 10
            reasons.append("quantity covered")
        else:
            ratio = offer.quantity / demand.quantity
            score += 10 * ratio
            pct = ratio * 100
            reasons.append("partial qty <1%" if pct < 1 else f"partial qty {int(pct)}%")

    # 4) Price — is the seller within (or near) the buyer's budget? (positive only)
    if demand.target_price > 0 and offer.price > 0:
        if offer.price <= demand.target_price:
            score += 15
            reasons.append("within budget")
        elif (offer.price - demand.target_price) / demand.target_price < 0.10:
            score += 8
            reasons.append("near budget")

    # 5) Location — light bonus for a shared place (whole-word match).
    if demand.location and offer.location and _location_match(demand.location, offer.location):
        score += 5
        reasons.append("location match")

    return max(0.0, min(round(score, 1), 100.0)), ", ".join(reasons)
