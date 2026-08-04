"""Tests for the demand scout's pure normalize/dedup/filter helpers."""
import importlib.util
import os

# demand_scout lives in scripts/ (not a package) — load it by path.
_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts", "demand_scout.py")
_spec = importlib.util.spec_from_file_location("demand_scout", _PATH)
demand_scout = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(demand_scout)


def test_year_extracts_recent():
    assert demand_scout._year("active 2026") == 2026
    assert demand_scout._year("posted Jan 2025") == 2025
    assert demand_scout._year("no date here") is None
    assert demand_scout._year("2019 old, updated 2025") == 2025   # largest wins


def test_domain_normalizes():
    assert demand_scout._domain("https://www.altimus.ae/collections/cd") == "altimus.ae"
    assert demand_scout._domain("altimus.ae") == "altimus.ae"
    assert demand_scout._domain("") == ""


def test_norm_buyer_drops_parens_and_punct():
    assert demand_scout._norm_buyer("Altimus Office (altimus.ae)") == "altimus office"
    assert demand_scout._norm_buyer("Music Box International, LLC") == "music box international llc"


def test_key_prefers_domain():
    a = {"buyer": "Altimus Office Supplies LLC", "website": "altimus.ae"}
    b = {"buyer": "Altimus Office (altimus.ae)", "website": "https://www.altimus.ae/x"}
    assert demand_scout._key(a) == demand_scout._key(b)     # same domain -> dedup


def test_key_falls_back_to_buyer_when_no_site():
    a = {"buyer": "Some Buyer Co", "website": ""}
    b = {"buyer": "Some Buyer Co", "website": ""}
    c = {"buyer": "Totally Different", "website": ""}
    assert demand_scout._key(a) == demand_scout._key(b)
    assert demand_scout._key(a) != demand_scout._key(c)


def test_normalize_marks_contact_gated_when_no_contacts():
    row = demand_scout._normalize({"buyer": "X", "product": "Y"}, "AE")
    assert row["contact_gated"] is True and row["country"] == "AE"
    row2 = demand_scout._normalize({"buyer": "X", "email": "a@b.com"}, "AE")
    assert row2["contact_gated"] is False
