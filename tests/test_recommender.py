"""Tests for the destination-country recommender + the unit-basis ($/kg vs $/unit) fix.
Offline: recommend_destinations against the committed Georgia market cache (targets=[268])."""
from app.research_engine import _assess, recommend_destinations


def test_recommend_returns_ranked_rows_offline():
    # 268/2715 is cached (docs/research/market/268_2715_2023-2024.json) -> no network
    rows = recommend_destinations(["2715"], targets=[268])
    assert rows and rows[0]["country"] == "Georgia"
    r0 = rows[0]
    for k in ("code", "country", "score", "tone", "latest_cif", "unit_basis",
              "iran_present", "uae_present"):
        assert k in r0
    assert 0 <= r0["score"] <= 100


def test_recommend_sorts_by_score_desc():
    rows = recommend_destinations(["2715"], targets=[268])
    scores = [r["score"] for r in rows]
    assert scores == sorted(scores, reverse=True)


def test_assess_unit_basis_defaults_to_kg():
    a = _assess({"top_suppliers": [], "latest_cif": 100000}, (364, 784))
    assert a["unit_basis"] == "kg"


def test_assess_unit_basis_passthrough():
    a = _assess({"top_suppliers": [], "latest_cif": 100000, "unit_basis": "unit"}, (364, 784))
    assert a["unit_basis"] == "unit"
