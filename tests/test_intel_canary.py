"""Phase 8 — the intelligence canary as a durable test: demand -> opportunity -> transparent score -> alert ->
report, with cross-seller isolation (403/404) and no PII leakage."""
from sqlalchemy.pool import StaticPool
from sqlmodel import create_engine

import scripts.intel_canary as CANARY


def test_intel_canary_passes(tmp_path):
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    report = CANARY.run(engine=engine, files_dir=str(tmp_path / "reports"))
    assert report["result"] == "PASS"
    assert report["counts"]["demand_signals"] >= 1 and report["counts"]["opportunities"] >= 1
    # an authenticated second seller is refused every intelligence surface (never 200)
    assert all(v in (403, 404) for v in report["isolation"].values())
