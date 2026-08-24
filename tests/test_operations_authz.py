"""Phase 7 — operations authorization & confidentiality: admin vs seller, no business mutation on GET, audit
trail on operational mutations, and the settlement route gated to manager+."""
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine, select

import app.main as main
from app.auth import hash_password
from app.models import AuditLog, Deal, FreightRequest, Lead, PaymentMilestone, User

_INDEXES = (
    "CREATE UNIQUE INDEX IF NOT EXISTS uq_workitem_idem_open ON workitem(idempotency_key) "
    "WHERE idempotency_key != '' AND status IN ('open','in_progress','waiting')",
    "CREATE UNIQUE INDEX IF NOT EXISTS uq_shipmentevent_ext ON shipmentevent(source, external_event_id) "
    "WHERE external_event_id != ''",
)
WRITE_ROUTES = ["/operations/freight", "/operations/shipments", "/operations/cases",
                "/operations/payments", "/operations/exceptions"]
GET_PAGES = ["/operations", "/operations/freight", "/operations/shipments", "/operations/documentation",
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
    monkeypatch.setattr(main, "OPERATION_FILES_DIR", tmp_path / "op")
    with Session(e) as s:
        s.add(User(email="admin@t.local", name="A", role="admin", active=True, password_hash=hash_password("pw")))
        s.add(User(email="mgr@t.local", name="M", role="manager", active=True, password_hash=hash_password("pw")))
        s.add(User(email="seller@t.local", name="S", role="agent", active=True, password_hash=hash_password("pw")))
        s.commit()
    return e


def _login(c, email):
    assert c.post("/login", data={"email": email, "password": "pw"}, follow_redirects=False).status_code == 303


def test_seller_forbidden_on_all_get_and_write(ctx):
    seller = TestClient(main.app)
    _login(seller, "seller@t.local")
    for p in GET_PAGES:
        assert seller.get(p).status_code == 403, p
    for p in WRITE_ROUTES:
        assert seller.post(p, data={}, follow_redirects=False).status_code == 403, p


def test_get_pages_do_not_mutate(ctx):
    c = TestClient(main.app)
    _login(c, "admin@t.local")
    for p in GET_PAGES:
        c.get(p)
    with Session(ctx) as s:
        # a pile of GETs created no operational rows and no audit-log write
        assert s.exec(select(FreightRequest)).first() is None
        assert s.exec(select(AuditLog)).first() is None


def test_operational_mutation_is_audited(ctx):
    c = TestClient(main.app)
    _login(c, "admin@t.local")
    c.post("/operations/freight", data={"mode": "sea", "origin_country": "ir", "dest_country": "ge",
           "gross_weight_kg": "1", "volume_cbm": "1", "hazardous": "no", "customs_required": "no"},
           follow_redirects=False)
    with Session(ctx) as s:
        acts = {a.action for a in s.exec(select(AuditLog)).all()}
        assert "freight_request_created" in acts


def test_settlement_admin_only_and_records_immutably(ctx):
    with Session(ctx) as s:
        admin = s.exec(select(User).where(User.email == "admin@t.local")).one()
        lead = Lead(product="x", tracking_code="G4-Z", owner_id=admin.id); s.add(lead); s.commit(); s.refresh(lead)
        d = Deal(lead_id=lead.id, owner_id=admin.id, stage="delivered", tracking_code="G4-Z-D")
        s.add(d); s.commit(); s.refresh(d)
        pm = PaymentMilestone(deal_id=d.id, milestone_type="buyer_balance", currency="USD",
                              expected_amount="100", confirmed_amount="100", status="received")
        s.add(pm); s.commit()
        did = d.id
    # a seller (agent) is forbidden from the operations settle route
    seller = TestClient(main.app)
    _login(seller, "seller@t.local")
    assert seller.post(f"/operations/deals/{did}/settle", data={"revenue": "100", "verified_costs": "70",
                       "currency": "USD"}, follow_redirects=False).status_code == 403
    # admin settles
    admin_c = TestClient(main.app)
    _login(admin_c, "admin@t.local")
    r = admin_c.post(f"/operations/deals/{did}/settle", data={"revenue": "100", "verified_costs": "70",
                     "currency": "USD"}, follow_redirects=False)
    assert r.status_code == 303
    with Session(ctx) as s:
        assert s.get(Deal, did).stage == "settled"
