"""Phase 9 — the offline AI canary as a durable test: cited answer (no provider), encrypted storage, research +
action proposals (not executed), payment refused, injection refused, seller 403 on all AI routes, cross-admin
404, and Pause-All halting the provider while deterministic answers keep working."""
from sqlalchemy.pool import StaticPool
from sqlmodel import create_engine

import scripts.ai_canary as CANARY


def test_ai_canary_passes(monkeypatch):
    monkeypatch.setenv("AI_DATA_ENCRYPTION_KEYS", "test-canary-key-strong-0001")
    import importlib
    import app.ai_encryption
    importlib.reload(app.ai_encryption)
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    report = CANARY.run(engine=engine)
    assert report["result"] == "PASS"
    assert all(v == 403 for v in report["isolation"].values())
    assert report["counts"]["proposals"] >= 2 and report["counts"]["messages"] >= 1
