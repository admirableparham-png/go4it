"""Tests for the command-box intent router + directory parsers."""
from app.command_service import _osm_selectors, _parse_cards, parse_command, slugify
from geo_en import translit_any


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


def test_osm_selectors_validated_guess_only_for_real_shop_values():
    # a real OSM shop value is guessed; a made-up one is NOT (returns [] -> caller gives guidance)
    assert _osm_selectors("butcher") == ['nwr["shop"="butcher"]']
    assert _osm_selectors("widget") == []


def test_parse_unmappable_osm_keyword_is_unknown_with_guidance():
    r = parse_command("widget shops in Georgia")
    assert r["action"] == "unknown" and r["iso"] == "GE"
    assert "OpenStreetMap category" in r["note"]


def test_translit_any_handles_arabic_persian_cyrillic():
    assert translit_any("مبلمان") and translit_any("مبلمان").isascii()      # Persian
    assert translit_any("Мебель").isascii()                                 # Cyrillic
    assert translit_any("Furniture World") == "Furniture World"             # Latin passthrough
    assert translit_any("株式会社") == ""                                    # non-mappable -> empty (skip)


def test_translit_any_folds_latin_diacritics_not_drops():
    # Azerbaijani/Turkish/European names must keep their letters (fold), not lose them
    assert translit_any("Çağ Mağaza") == "Cag Magaza"
    assert translit_any("Şişecam") == "Sisecam"
    assert translit_any("Bakı Ticarət") == "Baki Ticaret"      # ı, ə have no NFKD decomposition
    assert translit_any("İstanbul") == "Istanbul"


def test_parse_non_latin_country_and_category():
    # Cyrillic category word + Latin country -> still routes to OSM with a real selector
    r = parse_command("мебель in Georgia")
    assert r["iso"] == "GE"
    assert r["action"] in ("harvest_osm", "unknown")   # transliterates; may or may not map, never crashes


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
