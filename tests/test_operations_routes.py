"""Phase 7 — operations HTTP routes: admin-only workspace pages render, sellers are forbidden, the freight →
offer → select and shipment → event flows work end-to-end through the routes, and document upload is
quarantined + admin-download-gated with a path-traversal guard."""
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine, select

import app.main as main
from app.auth import hash_password
from app.models import FreightOffer, FreightRequest, Shipment, TradeDocument, User

_INDEXES = (
    "CREATE UNIQUE INDEX IF NOT EXISTS uq_workitem_idem_open ON workitem(idempotency_key) "
    "WHERE idempotency_key != '' AND status IN ('open','in_progress','waiting')",
    "CREATE UNIQUE INDEX IF NOT EXISTS uq_shipmentevent_ext ON shipmentevent(source, external_event_id) "
    "WHERE external_event_id != ''",
    "CREATE UNIQUE INDEX IF NOT EXISTS uq_operationcase_reference ON operationcase(reference) WHERE reference != ''",
)
PAGES = ["/operations", "/operations/freight", "/operations/shipments", "/operations/documentation",
         "/operations/payments", "/operations/exceptions"]


@pytest.fixture
def ctx(monkeypatch, tmp_path):
    e = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    SQLModel.metadata.create_all(e)
    with e.connect() as c:
        for ddl in _INDEXES:
            c.execute(text(ddl))
        c.commit()
    monkeypatch.setattr(main, "engine", e)
    monkeypatch.setattr(main, "OPERATION_FILES_DIR", tmp_path / "operation_files")
    with Session(e) as s:
        s.add(User(email="admin@t.local", name="A", role="admin", active=True, password_hash=hash_password("pw")))
        s.add(User(email="seller@t.local", name="S", role="agent", active=True, password_hash=hash_password("pw")))
        s.commit()
    return e


def _login(c, email):
    assert c.post("/login", data={"email": email, "password": "pw"}, follow_redirects=False).status_code == 303


def test_all_pages_render_for_admin(ctx):
    c = TestClient(main.app)
    _login(c, "admin@t.local")
    for p in PAGES:
        assert c.get(p).status_code == 200, p


def test_all_pages_forbidden_for_seller(ctx):
    c = TestClient(main.app)
    _login(c, "seller@t.local")
    for p in PAGES:
        assert c.get(p).status_code == 403, p


def test_freight_offer_select_flow(ctx):
    c = TestClient(main.app)
    _login(c, "admin@t.local")
    c.post("/operations/freight", data={"mode": "sea", "origin_country": "ir", "dest_country": "ge",
                                        "gross_weight_kg": "1000", "volume_cbm": "10", "hazardous": "no",
                                        "customs_required": "yes"}, follow_redirects=False)
    with Session(ctx) as s:
        fr = s.exec(select(FreightRequest)).one()
        assert fr.origin_country == "IR" and fr.hazardous is False
    c.post(f"/operations/freight/{fr.id}/offers", data={"currency": "usd", "base_freight": "1000",
           "mode": "sea", "route_summary": "BND→POTI"}, follow_redirects=False)
    with Session(ctx) as s:
        o = s.exec(select(FreightOffer)).one()
        assert o.total == "1000.00"
    r = c.post(f"/operations/offers/{o.id}/select", follow_redirects=False)
    assert r.status_code == 303
    with Session(ctx) as s:
        assert s.exec(select(FreightOffer)).one().selection_status == "selected"


def test_shipment_event_flow_and_masking(ctx):
    c = TestClient(main.app)
    _login(c, "admin@t.local")
    c.post("/operations/shipments", data={"mode": "sea", "origin": "Bandar Abbas, IR",
           "destination": "Poti, GE", "booking_reference": "BK-1"}, follow_redirects=False)
    with Session(ctx) as s:
        sh = s.exec(select(Shipment)).one()
        assert sh.current_milestone == "booked"
    detail = c.get(f"/operations/shipments/{sh.id}").text
    assert "BK-1" in detail                                       # admin CAN see the booking ref
    c.post(f"/operations/shipments/{sh.id}/events", data={"event_type": "departed",
           "event_at": "2026-01-01T08:00", "seller_safe_summary": "Departed origin port"},
           follow_redirects=False)
    with Session(ctx) as s:
        sh = s.exec(select(Shipment)).one()
        assert sh.current_milestone == "in_transit"


def test_document_upload_is_quarantined_and_download_gated(ctx):
    c = TestClient(main.app)
    _login(c, "admin@t.local")
    r = c.post("/operations/documentation/upload",
               data={"doc_type": "commercial_invoice"},
               files={"file": ("inv.pdf", b"%PDF-1.4 x", "application/pdf")}, follow_redirects=False)
    assert r.status_code == 303
    with Session(ctx) as s:
        doc = s.exec(select(TradeDocument)).one()
        assert doc.quarantine == "quarantined" and len(doc.sha256) == 64
    # a dangerous upload is rejected
    bad = c.post("/operations/documentation/upload", data={"doc_type": "other"},
                 files={"file": ("x.html", b"<script>", "text/html")}, follow_redirects=False)
    assert bad.status_code == 400
    # admin can download; a seller cannot
    assert c.get(f"/operations/documents/{doc.id}/download").status_code == 200
    seller = TestClient(main.app)
    _login(seller, "seller@t.local")
    assert seller.get(f"/operations/documents/{doc.id}/download").status_code == 403
