"""Tests for the generic product-line pipeline: scoring, specs, and the shared directory parser
that replaced the per-product harvest/enrich/load/export scripts."""
from app.harvest_lib import parse_uae_cards
from app.line_spec import all_specs, load_spec
from app.scoring import bulk_likely, score_and_fits

CFG = {
    "cat_base": {"A": 80, "B": 70},
    "families_for": {"A": ["Fam1"], "B": ["Fam2"]},
    "boost": ["media", "disc"],
    "penalty": ["printing"],
    "default_base": 55,
}


def test_score_base_from_best_category():
    s, fams = score_and_fits("Neutral Co", ["A"], CFG)
    assert s == 80 and fams == ["Fam1"]


def test_score_boost_and_multi_family():
    s, fams = score_and_fits("Disc Media LLC", ["A", "B"], CFG)   # +media +disc = +12
    assert s == 92 and fams == ["Fam1", "Fam2"]


def test_score_penalty():
    assert score_and_fits("Printing Press", ["B"], CFG)[0] == 58   # 70 - 12


def test_score_clamps_0_100():
    assert score_and_fits("printing printing printing", ["A"], CFG)[0] >= 0
    assert score_and_fits("media disc media disc media", ["A"], CFG)[0] <= 100


def test_score_unknown_category_uses_default_base():
    assert score_and_fits("x", ["Zzz"], CFG)[0] == 55


def test_bulk_likely():
    assert bulk_likely("ABC Distribution FZE", ["distribut"]) is True
    assert bulk_likely("Small Retail Shop", ["distribut", "wholesale"]) is False


def test_all_specs_present_and_valid():
    slugs = {s["slug"] for s in all_specs()}
    assert {"cd-dvd", "decoration"} <= slugs
    for s in all_specs():
        assert s.get("buyers_file") and s.get("scoring") and "families" in s


def test_load_spec_shape():
    spec = load_spec("cd-dvd")
    assert spec["dest"]["iso"] == "AE"
    assert spec["scoring"]["cat_base"]["Medical equipment supplier"] == 90


def test_parse_uae_cards_shared_parser():
    html = ('<a title="Test Trading LLC" class="x" href="/test-trading-12345?p=abc">link</a>'
            '<div>Location : </span><span class="v">Dubai</span>'
            '<a href="tel:+97150111222">call</a>')
    cards = parse_uae_cards(html)
    assert cards and cards[0]["company"] == "Test Trading LLC"
    assert cards[0]["cid"] == "12345" and cards[0]["city"] == "Dubai"
    assert "+97150111222" in cards[0]["phones"]
