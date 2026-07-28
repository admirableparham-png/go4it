"""Regression tests for the matching engine — pin the Phase 0 fixes in place."""
from app.matching import _location_match, _text_similarity, score_pair
from app.models import Demand, Offer


def _demand(**kw):
    base = dict(product="", category="", spec="", quantity=0, unit="",
                target_price=0, currency="USD", location="", contact="")
    base.update(kw)
    return Demand(**base)


def _offer(**kw):
    base = dict(product="", category="", spec="", quantity=0, unit="",
                price=0, currency="USD", location="", contact="")
    base.update(kw)
    return Offer(**base)


def test_subset_text_no_longer_scores_100():
    """A generic short demand must NOT score a perfect text match against an
    unrelated longer offer that merely contains its words (the token_set_ratio bug)."""
    d = _demand(product="steel", category="metals")
    o = _offer(product="steel wire mesh galvanized", category="metals")
    sim = _text_similarity(d, o)
    assert sim < 80, f"expected length-aware similarity well below 100, got {sim}"


def test_true_match_outranks_spurious_one():
    d = _demand(product="steel rebar 12mm", category="metals", spec="grade B500B",
                quantity=50, target_price=650)
    true_offer = _offer(product="rebar steel 12mm", category="metals", spec="B500B grade",
                        quantity=80, price=630)
    spurious = _offer(product="steel wire mesh galvanized", category="metals",
                     quantity=80, price=630)
    assert score_pair(d, true_offer)[0] > score_pair(d, spurious)[0]


def test_negative_offer_quantity_never_negative_or_covered():
    d = _demand(product="steel rebar", category="metals", quantity=1)
    o = _offer(product="rebar steel", category="metals", quantity=-1000)
    score, reasons = score_pair(d, o)
    assert score >= 0.0
    assert "quantity covered" not in reasons
    assert "partial qty" not in reasons  # non-positive quantity is skipped entirely


def test_score_is_clamped_0_100():
    d = _demand(product="steel rebar 12mm", category="metals", spec="grade B500B",
                quantity=50, target_price=650, location="Dubai")
    o = _offer(product="steel rebar 12mm", category="metals", spec="grade B500B",
              quantity=80, price=600, location="Dubai")
    score, _ = score_pair(d, o)
    assert 0.0 <= score <= 100.0


def test_negative_price_gets_no_budget_bonus():
    d = _demand(product="widget", target_price=650)
    o = _offer(product="widget", price=-100)
    _, reasons = score_pair(d, o)
    assert "within budget" not in reasons
    assert "near budget" not in reasons


def test_location_whole_word_not_substring():
    assert _location_match("Dubai", "Dubai / UAE") is True
    assert _location_match("Oman", "Romania") is False   # 'oman' is inside 'romania'
    d = _demand(product="x", location="Oman")
    o = _offer(product="x", location="Romania")
    assert "location match" not in score_pair(d, o)[1]


def test_partial_qty_sub_one_percent_label():
    d = _demand(product="steel", category="metals", quantity=1000)
    o = _offer(product="steel", category="metals", quantity=1)
    assert "partial qty <1%" in score_pair(d, o)[1]
