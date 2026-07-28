"""Regression tests for the lead->product matching engine."""
from app.matching import on_corridor, score_lead_product, text_similarity
from app.models import Lead, Product


def _lead(**kw):
    base = dict(product="", category="", spec="", quantity=0, unit="",
                target_price=0, currency="USD", dest_country="")
    base.update(kw)
    return Lead(**base)


def _product(**kw):
    base = dict(name="", category="", spec="", exw_price=0, currency="USD",
                unit="", min_order_qty=0)
    base.update(kw)
    return Product(**base)


def test_subset_text_no_longer_scores_100():
    """A generic short lead must NOT perfectly match an unrelated longer product."""
    sim = text_similarity("steel metals", "steel wire mesh galvanized metals")
    assert sim < 80, f"expected length-aware similarity well below 100, got {sim}"


def test_true_match_outranks_spurious_one():
    lead = _lead(product="steel rebar 12mm", category="metals", spec="grade B500B",
                 quantity=100, target_price=700, dest_country="GE")
    true_p = _product(name="Steel rebar 12mm", category="metals", spec="A3 B500B",
                      exw_price=590, min_order_qty=25)
    spurious = _product(name="steel wire mesh galvanized", category="metals",
                        exw_price=590, min_order_qty=25)
    assert score_lead_product(lead, true_p)[0] > score_lead_product(lead, spurious)[0]


def test_corridor_destination_ranks_higher():
    product = _product(name="Steel rebar 12mm", category="metals", exw_price=590)
    on = _lead(product="rebar", category="metals", dest_country="GE")
    off = _lead(product="rebar", category="metals", dest_country="BR")
    on_score, on_reasons = score_lead_product(on, product)
    off_score, _ = score_lead_product(off, product)
    assert on_score > off_score
    assert "corridor" in on_reasons


def test_on_corridor_helper():
    assert on_corridor("GE") and on_corridor("tr") and on_corridor("Georgia")
    assert not on_corridor("BR") and not on_corridor("")


def test_negative_quantity_and_price_are_safe():
    lead = _lead(product="rebar", category="metals", quantity=-50, target_price=-100)
    product = _product(name="rebar", category="metals", exw_price=590, min_order_qty=25)
    score, reasons = score_lead_product(lead, product)
    assert score >= 0.0
    assert "MOQ" not in reasons          # non-positive quantity is skipped
    assert "budget" not in reasons       # non-positive target is skipped


def test_score_is_clamped_0_100():
    lead = _lead(product="steel rebar 12mm", category="metals", spec="grade B500B",
                 quantity=100, target_price=700, dest_country="GE")
    product = _product(name="Steel rebar 12mm", category="metals", spec="grade B500B",
                       exw_price=590, min_order_qty=25)
    score, _ = score_lead_product(lead, product)
    assert 0.0 <= score <= 100.0
