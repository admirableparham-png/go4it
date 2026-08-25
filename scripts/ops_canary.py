"""Operations (Phase 7) STAGING canary — exercises the full operational flow end to end against a DISPOSABLE
database, asserts every guarantee, and prints an evidence report. Moves NO real funds and calls NO external
provider (all provider integrations are honestly "Not configured").

    ./.venv/bin/python scripts/ops_canary.py            # temp DB, prints PASS + inventory, exit 0/nonzero

Flow: submit freight request -> OperationCase + freight offer (select) -> book shipment -> request + upload a
seller document (quarantined, admin-attested) -> record tracking + delivery events -> confirm MONOTONIC
Deal-stage advancement -> confirm another seller cannot reach the case/deal/documents -> exercise payment +
remittance status without moving funds. Returns a report dict; raises AssertionError on any failure.
"""
import os
import sys
import tempfile

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)

from fastapi.testclient import TestClient   # noqa: E402
from sqlalchemy import text   # noqa: E402
from sqlmodel import Session, SQLModel, create_engine, func, select   # noqa: E402

import app.main as main   # noqa: E402
from app import customs as CUSTOMS   # noqa: E402
from app import freight as FREIGHT   # noqa: E402
from app import operations as OPS   # noqa: E402
from app import ops_providers as OPSPROV   # noqa: E402
from app import payments as PAY   # noqa: E402
from app import remittance as REMIT   # noqa: E402
from app import shipments as SHIP   # noqa: E402
from app import tradedocs as TDOCS   # noqa: E402
from app.auth import hash_password   # noqa: E402
from app.deal_service import DEAL_STAGES   # noqa: E402
from app.models import (ComplianceDoc, CustomsCase, Deal, DeliveryConfirmation, DocumentRequirement,
                        FreightOffer, FreightRequest, Lead, OperationCase, OperationalException,
                        PaymentMilestone, RemittanceCase, SellerUpdate, ServiceRequest, Shipment,
                        ShipmentEvent, ShipmentLeg, TradeDocument, User, WorkItem)   # noqa: E402

_INDEXES = (
    "CREATE UNIQUE INDEX IF NOT EXISTS uq_workitem_idem_open ON workitem(idempotency_key) "
    "WHERE idempotency_key != '' AND status IN ('open','in_progress','waiting')",
    "CREATE UNIQUE INDEX IF NOT EXISTS uq_shipmentevent_ext ON shipmentevent(source, external_event_id) "
    "WHERE external_event_id != ''",
    "CREATE UNIQUE INDEX IF NOT EXISTS uq_operationcase_reference ON operationcase(reference) WHERE reference != ''",
    "CREATE UNIQUE INDEX IF NOT EXISTS uq_opcase_deal_primary ON operationcase(deal_id) "
    "WHERE case_type = 'deal' AND deal_id IS NOT NULL",
)


def _monotonic(stages):
    idxs = [DEAL_STAGES.index(s) for s in stages]
    return all(b >= a for a, b in zip(idxs, idxs[1:]))


def run(engine=None, files_dir=None):
    report, steps = {}, []
    tmp = None
    if engine is None:
        tmp = tempfile.mkdtemp(prefix="ops_canary_")
        engine = create_engine(f"sqlite:///{os.path.join(tmp, 'canary.db')}")
        files_dir = files_dir or os.path.join(tmp, "files")
    SQLModel.metadata.create_all(engine)
    with engine.connect() as c:
        for ddl in _INDEXES:
            c.execute(text(ddl))
        c.commit()
    saved_engine, saved_files = main.engine, getattr(main, "OPERATION_FILES_DIR", None)
    main.engine = engine
    import pathlib
    main.OPERATION_FILES_DIR = pathlib.Path(files_dir)
    try:
        # --- seed: admin + two sellers + a deal for seller A (with an originating request) ---
        with Session(engine) as s:
            for email, role in [("admin@canary", "admin"), ("sellera@canary", "agent"),
                                ("sellerb@canary", "agent")]:
                s.add(User(email=email, name=email, role=role, active=True, password_hash=hash_password("pw")))
            s.commit()
            admin = s.exec(select(User).where(User.email == "admin@canary")).one()
            a = s.exec(select(User).where(User.email == "sellera@canary")).one()
            req = ServiceRequest(tracking_code="SR-CAN", requester_id=a.id, owner_id=a.id,
                                 request_type="freight", status="running")
            s.add(req); s.commit(); s.refresh(req)
            lead = Lead(product="Zinc Sulphate", tracking_code="G4-CAN", dest_country="GE",
                        buyer_company="ACME Baghdad", owner_id=a.id, request_id=req.id)
            s.add(lead); s.commit(); s.refresh(lead)
            deal = Deal(lead_id=lead.id, owner_id=a.id, stage="won", tracking_code="G4-CAN-D")
            s.add(deal); s.commit(); s.refresh(deal)
            admin_id, a_id, deal_id, req_id = admin.id, a.id, deal.id, req.id

        stage_track = ["won"]
        with Session(engine) as s:
            admin = s.get(User, admin_id); deal = s.get(Deal, deal_id)

            # 1. submit a (complete) freight request
            fr, missing = FREIGHT.create_freight_request(
                s, actor=admin, tenant_id=a_id, deal_id=deal_id, mode="sea", origin_country="IR",
                dest_country="GE", cargo_description="Zinc Sulphate Monohydrate", quantity="20", unit="tonne",
                gross_weight_kg="20000", volume_cbm="28", hazardous=False, customs_required=True)
            assert missing == [], f"freight request should be complete, missing={missing}"
            steps.append("freight request submitted")

            # 2. OperationCase + freight offer (selected)
            case, made = OPS.ensure_case_for_deal(s, deal, actor=admin)
            assert made and case.case_type == "deal"
            fr.operation_case_id = case.id; s.add(fr)
            offer = FREIGHT.add_offer(s, fr, actor=admin, provider_name_cache="Blue Sea Lines", mode="sea",
                                      route_summary="Bandar Abbas -> Poti", currency="USD",
                                      base_freight="1800", surcharges="200", lead_time_days=18)
            ok, err = FREIGHT.select_offer(s, offer, actor=admin)
            assert ok, err
            steps.append("operation case + freight offer selected")

            # 3. book a test shipment
            sh = SHIP.book_shipment(s, case=case, offer=offer, actor=admin, deal_id=deal_id,
                                    mode="sea", origin="Bandar Abbas, IR", destination="Poti, GE",
                                    booking_reference="CANARY-BK-1", carrier_name_cache="Blue Sea Lines")
            SHIP.add_leg(s, sh, actor=admin, mode="road", origin="Isfahan", destination="Bandar Abbas")
            SHIP.add_leg(s, sh, actor=admin, mode="sea", origin="Bandar Abbas", destination="Poti")
            steps.append("shipment booked with 2 legs")

            # 4. request + upload a seller document (quarantined -> admin-attested; never labelled "scanned")
            docreq = TDOCS.create_requirement(s, doc_type="certificate_of_origin", required_from="seller",
                                              request_id=req_id, operation_case_id=case.id, deal_id=deal_id,
                                              tenant_id=a_id, actor=admin)
            ok, err = TDOCS.request_seller_document(s, docreq, instructions="Please upload your certificate of origin.",
                                                    actor=admin)
            assert ok, err
            doc, derr = TDOCS.store_document(s, files_dir=main.OPERATION_FILES_DIR, data=b"%PDF-1.4 canary",
                                             original_filename="coo.pdf", content_type="application/pdf",
                                             doc_type="certificate_of_origin", uploaded_by_role="seller",
                                             requirement=docreq, tenant_id=a_id, actor=admin)
            assert doc and derr == "" and doc.quarantine == "quarantined"
            _d, aerr = TDOCS.admin_attest(s, doc, actor=admin)
            assert aerr == "" and doc.quarantine == "admin_attested"
            label, _ = TDOCS.scan_status(doc)
            assert label == "Admin-attested" and "scan" not in label.lower(), "attestation must not read as scanned"
            steps.append("seller document requested + uploaded (quarantined) + admin-attested")

            # verified compliance docs so the deal can pass its non-bypassable gates
            for dt in ("certificate_of_origin", "commercial_invoice", "bill_of_lading", "packing_list",
                       "customs_declaration"):
                s.add(ComplianceDoc(deal_id=deal_id, doc_type=dt, status="verified"))
            s.commit()

            # buyer payment received (evidence-gated) so payment_received can project
            pm = PAY.create_milestone(s, milestone_type="buyer_deposit", currency="USD", expected_amount="24000",
                                      deal_id=deal_id, tenant_id=a_id, seller_visible=True, actor=admin)
            ok, err = PAY.confirm_payment(s, pm, confirmed_amount="24000", reference_code="CANARY-WIRE",
                                          actor=admin)
            assert ok, err
            s.commit()

            def project():
                OPS.project_deal_stage(s, deal, actor=admin); s.commit(); stage_track.append(deal.stage)

            # 5+6. record tracking + delivery events, projecting after each milestone; assert MONOTONIC
            project()  # supplier_confirmed / payment_received / freight_booked
            assert deal.stage == "freight_booked", deal.stage

            CUSTOMS.set_customs_status(s, CUSTOMS.create_customs_case(s, case=case, side="export", country="IR",
                                       actor=admin), "cleared", actor=admin); s.commit()
            project()
            assert deal.stage == "export_cleared", deal.stage

            SHIP.record_event(s, sh, event_type="departed", source="manual", location="Bandar Abbas",
                              seller_safe_summary="Departed origin port", actor=admin); s.commit()
            project()
            assert deal.stage == "in_transit", deal.stage

            CUSTOMS.set_customs_status(s, CUSTOMS.create_customs_case(s, case=case, side="import", country="GE",
                                       actor=admin), "cleared", actor=admin); s.commit()
            project()
            assert deal.stage == "import_cleared", deal.stage

            dc, exc = SHIP.confirm_delivery(s, sh, source="pod_document", recipient_role="buyer", actor=admin)
            assert exc is None and sh.current_milestone == "delivered"
            s.commit()
            project()
            assert deal.stage == "delivered", deal.stage
            assert _monotonic(stage_track), f"deal stage was not monotonic: {stage_track}"
            steps.append(f"deal advanced monotonically: {' -> '.join(stage_track)}")

            # re-projecting is idempotent + never regresses
            before = deal.stage
            OPS.project_deal_stage(s, deal, actor=admin); s.commit()
            assert deal.stage == before, "re-projection regressed the deal"
            steps.append("re-projection idempotent (no regression)")

            # 8. payment + remittance status WITHOUT moving funds
            assert OPSPROV.remittance_status()["configured"] is False
            rc = REMIT.create_remittance(s, source_currency="USD", dest_currency="IRR", source_amount="1000",
                                         route_method_category="exchange_house", deal_id=deal_id,
                                         operation_case_id=case.id, tenant_id=a_id, actor=admin)
            REMIT.set_status(s, rc, "compliance_review", compliance_reason="screening", actor=admin); s.commit()
            assert rc.status == "compliance_review"
            steps.append("payment confirmed (evidence) + remittance status advanced — no real funds moved")

            sh_id, doc_id = sh.id, doc.id

        # 7. cross-seller isolation over HTTP — authenticated seller B can reach NOTHING of seller A's.
        # follow_redirects=False so an isolation refusal (403/404) is never confused with a login redirect.
        client_b = TestClient(main.app)
        assert client_b.post("/login", data={"email": "sellerb@canary", "password": "pw"},
                             follow_redirects=False).status_code == 303
        assert client_b.get("/", follow_redirects=False).status_code == 200, "seller B is not authenticated"
        iso = {
            "deal_detail": client_b.get(f"/deals/{deal_id}", follow_redirects=False).status_code,  # -> 404
            "ops_overview": client_b.get("/operations", follow_redirects=False).status_code,        # -> 403
            "documentation": client_b.get("/operations/documentation", follow_redirects=False).status_code,
            "doc_download": client_b.get(f"/operations/documents/{doc_id}/download",
                                         follow_redirects=False).status_code,
            "shipment_detail": client_b.get(f"/operations/shipments/{sh_id}", follow_redirects=False).status_code,
        }
        assert iso["deal_detail"] == 404, iso
        assert all(v in (403, 404) for v in iso.values()), iso
        steps.append(f"cross-seller isolation enforced (authenticated seller B): {iso}")

        # --- inventory ---
        with Session(engine) as s:
            cnt = lambda m: s.exec(select(func.count()).select_from(m)).one()   # noqa: E731
            report["counts"] = {
                "operation_cases": cnt(OperationCase), "freight_requests": cnt(FreightRequest),
                "freight_offers": cnt(FreightOffer), "shipments": cnt(Shipment), "shipment_legs": cnt(ShipmentLeg),
                "shipment_events": cnt(ShipmentEvent), "document_requirements": cnt(DocumentRequirement),
                "trade_documents": cnt(TradeDocument), "customs_cases": cnt(CustomsCase),
                "delivery_confirmations": cnt(DeliveryConfirmation), "operational_exceptions": cnt(OperationalException),
                "payment_milestones": cnt(PaymentMilestone), "remittance_cases": cnt(RemittanceCase),
            }
            report["work_items"] = {t: c for t, c in
                                    s.exec(select(WorkItem.type, func.count()).group_by(WorkItem.type)).all()}
            report["seller_updates"] = {"published": len(s.exec(select(SellerUpdate).where(
                SellerUpdate.published == True)).all()),  # noqa: E712
                "drafts": len(s.exec(select(SellerUpdate).where(SellerUpdate.published == False)).all())}  # noqa: E712
            report["stage_path"] = stage_track
            report["isolation"] = iso
        report["steps"] = steps
        report["result"] = "PASS"
        return report
    finally:
        main.engine = saved_engine
        if saved_files is not None:
            main.OPERATION_FILES_DIR = saved_files


def _print(report):
    print("=== Operations staging canary ===")
    for st in report["steps"]:
        print(f"  ok  {st}")
    print("--- inventory ---")
    for k, v in report["counts"].items():
        print(f"  {k:24s} {v}")
    print("  work items:", report["work_items"])
    print("  seller updates:", report["seller_updates"])
    print("RESULT:", report["result"])


if __name__ == "__main__":
    try:
        _print(run())
    except AssertionError as e:
        print("CANARY FAILED:", e)
        sys.exit(1)
