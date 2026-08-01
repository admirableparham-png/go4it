"""Tests for supplier-management helpers (route behavior is smoke-tested via TestClient)."""
from app.main import _clamp_reliability


def test_clamp_reliability_bounds_and_garbage():
    assert _clamp_reliability(4) == 4
    assert _clamp_reliability(0) == 1          # below range -> 1
    assert _clamp_reliability(9) == 5          # above range -> 5
    assert _clamp_reliability("3") == 3        # form value arrives as str
    assert _clamp_reliability("") == 3         # empty -> sensible default
    assert _clamp_reliability(None) == 3
