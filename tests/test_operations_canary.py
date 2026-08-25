"""Phase 7 — the Operations staging canary as a durable test: the full flow (freight → case → offer → shipment
→ seller document → tracking/delivery → monotonic Deal-stage → cross-seller isolation → payment/remittance
without moving funds) passes end to end, with no external provider called."""
from sqlalchemy.pool import StaticPool
from sqlmodel import create_engine

import scripts.ops_canary as CANARY
from app.deal_service import DEAL_STAGES


def test_ops_canary_passes(tmp_path):
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    report = CANARY.run(engine=engine, files_dir=str(tmp_path / "canary_files"))
    assert report["result"] == "PASS"
    # monotonic deal-stage advancement to delivered
    path = report["stage_path"]
    idxs = [DEAL_STAGES.index(s) for s in path]
    assert idxs == sorted(idxs) and path[-1] == "delivered"
    # cross-seller isolation: authenticated seller B is refused everywhere (404/403, never 200)
    assert report["isolation"]["deal_detail"] == 404
    assert all(v in (403, 404) for v in report["isolation"].values())
    # every operational entity was created by the flow
    c = report["counts"]
    for k in ("operation_cases", "freight_requests", "freight_offers", "shipments", "shipment_legs",
              "shipment_events", "document_requirements", "trade_documents", "customs_cases",
              "delivery_confirmations", "payment_milestones", "remittance_cases"):
        assert c[k] >= 1, k
    # automatic seller-safe progress updates were published (allowlisted milestones)
    assert report["seller_updates"]["published"] >= 1
