"""Tests for the command-box intent router + directory parsers."""
from app.command_service import _osm_selectors, _parse_cards, parse_command, slugify


def test_parse_uae_routes_to_yellowpages():
    r = parse_command("find hotel suppliers in UAE")
    assert r["action"] == "harvest_uae" and r["iso"] == "AE" and r["slug"] == "hotel-supplies"


def test_parse_other_country_routes_to_osm():
    r = parse_command("furniture shops in Georgia")
    assert r["action"] == "harvest_osm" and r["iso"] == "GE"
    assert r["selectors"] == ['nwr["shop"="furniture"]']


def test_parse_singular_maps_to_osm_plural_tag():
    r = parse_command("tile shops in Qatar")
    assert r["action"] == "harvest_osm"
    assert 'nwr["shop"="tiles"]' in r["selectors"]     # 'tile' -> 'tiles' tag


def test_parse_no_country_is_unknown():
    assert parse_command("random gibberish")["action"] == "unknown"


def test_parse_country_without_category_is_unknown():
    r = parse_command("buyers in Georgia")
    assert r["action"] == "unknown" and r["iso"] == "GE"


def test_osm_selectors_unknown_keyword_guesses_shop():
    assert _osm_selectors("widget") == ['nwr["shop"="widget"]']


def test_slugify():
    assert slugify("Home Decor & Gifts!") == "home-decor-gifts"


def test_parse_cards_extracts_from_yellowpages_html():
    html = (
        '<a title="Test Trading LLC" class="x" href="/test-trading-12345?p=abc">link</a>'
        '<div>Location : </span><span class="v">Dubai</span>'
        '<a href="tel:+97150111222">call</a>'
    )
    cards = _parse_cards(html)
    assert cards and cards[0]["company"] == "Test Trading LLC"
    assert cards[0]["cid"] == "12345" and cards[0]["city"] == "Dubai"
    assert "+97150111222" in cards[0]["phones"]
