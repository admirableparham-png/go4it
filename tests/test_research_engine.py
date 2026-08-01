"""Tests for the keyless market-research engine (verdict logic, lookups, aggregation)."""
import app.research_engine as re
from app.research_engine import (PARTNERS, country_options, market_report,
                                 product_options, resolve_query)


def _raw(suppliers, cif=44_000_000, total=100_000_000, trend=-11):
    return {"latest_cif": cif, "latest_tonnes": 6000, "latest_year": 2024,
            "trend_pct_first_to_last": trend, "total_cif": total,
            "top_suppliers": suppliers}


def test_assess_iran_cheapest_is_good():
    raw = _raw([
        {"country": "Iran", "code": 364, "cif_usd_total": 10_000_000, "unit_price_usd_kg": 0.22, "tonnes_total": 5000},
        {"country": "India", "code": 699, "cif_usd_total": 45_000_000, "unit_price_usd_kg": 0.55, "tonnes_total": 80000},
    ])
    a = re._assess(raw, (364, 784))
    assert a["tone"] == "good"
    assert a["iran_present"] and a["cheapest"]["code"] == 364
    assert 0 <= a["score"] <= 100


def test_assess_tiny_market_is_bad():
    a = re._assess(_raw([{"country": "X", "code": 1, "cif_usd_total": 100, "unit_price_usd_kg": 1.0, "tonnes_total": 1}],
                        cif=10_000, total=10_000), (364, 784))
    assert a["tone"] == "bad"


def test_assess_ignores_micro_shipment_outlier():
    """A 1-tonne freak-price supplier must NOT become the 'cheapest incumbent'."""
    raw = _raw([
        {"country": "Freakland", "code": 900, "cif_usd_total": 9_000, "unit_price_usd_kg": 0.01, "tonnes_total": 1},
        {"country": "India", "code": 699, "cif_usd_total": 50_000_000, "unit_price_usd_kg": 0.55, "tonnes_total": 90000},
    ], total=50_009_000)
    a = re._assess(raw, (364, 784))
    assert a["cheapest"]["country"] != "Freakland"     # filtered as immaterial (<3% share)


def test_resolve_query_product_and_hs():
    label, codes, key = resolve_query(product="ceramic-tiles")
    assert codes == ["6907"] and key == "ceramic-tiles"
    label2, codes2, _ = resolve_query(hs="6907, 6908")
    assert codes2 == ["6907", "6908"]
    _, codes3, _ = resolve_query(product="something nonexistent")
    assert codes3 == []          # unresolved -> route asks for an HS code


def test_country_and_product_options():
    countries = dict((n, c) for n, c in country_options())
    assert "Georgia" in countries and countries["Georgia"] == 268
    assert all(code != 0 for _, code in country_options())    # World excluded
    assert any(k == "ceramic-tiles" for k, _, _ in product_options())


def test_partners_has_comtrade_legacy_codes():
    assert PARTNERS[699] == "India" and PARTNERS[364] == "Iran"


def test_market_report_aggregates_fetched_rows(monkeypatch, tmp_path):
    monkeypatch.setattr(re, "CACHE_DIR", tmp_path)
    monkeypatch.setattr(re.time, "sleep", lambda *a, **k: None)
    rows = [
        {"partnerCode": 0, "netWgt": 1_000_000, "primaryValue": 500_000},     # World total
        {"partnerCode": 364, "netWgt": 500_000, "primaryValue": 120_000},     # Iran (cheap)
        {"partnerCode": 699, "netWgt": 400_000, "primaryValue": 300_000},     # India
    ]
    monkeypatch.setattr(re, "_fetch", lambda reporter, cmd, year: list(rows))
    rep = market_report(268, ["6907"], years=[2023, 2024])
    c = rep["commodities"][0]
    assert c["latest_cif"] > 0
    assert any(s["code"] == 364 for s in c["top_suppliers"])
    assert 0 <= c["assessment"]["score"] <= 100


def test_market_report_does_not_cache_failed_fetch(monkeypatch, tmp_path):
    """A network failure (None) must not be persisted as a 'dead market'."""
    monkeypatch.setattr(re, "CACHE_DIR", tmp_path)
    monkeypatch.setattr(re.time, "sleep", lambda *a, **k: None)
    monkeypatch.setattr(re, "_fetch", lambda reporter, cmd, year: None)  # all fetches fail
    market_report(268, ["6907"], years=[2023, 2024])
    assert list(tmp_path.glob("*.json")) == []     # nothing cached
