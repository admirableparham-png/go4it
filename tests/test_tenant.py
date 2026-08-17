"""Multi-tenant isolation — the Phase-0 security gate. A trader sees ONLY their own data; the admin
(founder) sees everything; the buyer-search / config surfaces are admin-only. If any of these fail,
a friend could see another friend's book — the whole point of the rebuild."""
import pytest
from fastapi.testclient import TestClient
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine, select

import app.main as main
from app.auth import hash_password
from app.models import Deal, Lead, Product, Quote, ServiceRequest, User
from app.tenant import is_admin, owns


class _U:
    def __init__(self, uid, role):
        self.id, self.role = uid, role


def test_is_admin_and_owns_unit():
    admin, a = _U(1, "admin"), _U(2, "agent")
    assert is_admin(admin) and not is_admin(a) and not is_admin(None)
    assert owns(2, a) and not owns(3, a)            # a trader owns only their own rows
    assert owns(3, admin) and owns(None, admin)     # admin owns everything, incl. the unowned pool
    assert not owns(None, a)                         # the unowned pool is admin-only
    assert not owns(2, None)                         # no user -> owns nothing (fail closed)


@pytest.fixture
def ctx(monkeypatch):
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    SQLModel.metadata.create_all(engine)
    monkeypatch.setattr(main, "engine", engine)
    ids = {}
    with Session(engine) as s:
        for email, role in [("admin@t.local", "admin"), ("a@t.local", "agent"), ("b@t.local", "agent")]:
            s.add(User(email=email, name=email[0].upper(), role=role, active=True,
                       password_hash=hash_password("pw")))
        s.commit()
        prod = Product(name="Widget", unit="pcs", exw_price=1.0, weight_kg_per_unit=0.1)
        s.add(prod); s.commit(); s.refresh(prod)
        owners = {u.email: u.id for u in s.exec(select(User)).all()}

        def mk(owner_id, company):
            lead = Lead(product="Widget", buyer_company=company, dest_country="IQ",
                        owner_id=owner_id, tracking_code=f"G4-{company}")
            s.add(lead); s.commit(); s.refresh(lead)
            q = Quote(lead_id=lead.id, owner_id=owner_id, product_id=prod.id, quantity=100,
                      delivered_unit=2.0, delivered_total=200, status="draft", tracking_code=f"Q-{company}")
            d = Deal(lead_id=lead.id, owner_id=owner_id, tracking_code=f"D-{company}")
            s.add(q); s.add(d); s.commit(); s.refresh(q); s.refresh(d)
            return lead.id, q.id, d.id

        ids["a"] = mk(owners["a@t.local"], "Acorp")
        ids["b"] = mk(owners["b@t.local"], "Bcorp")
    return TestClient(main.app), engine, ids


def _login(client, email):
    assert client.post("/login", data={"email": email, "password": "pw"},
                       follow_redirects=False).status_code == 303


def test_trader_leads_list_is_scoped(ctx):
    client, _, _ = ctx
    _login(client, "a@t.local")
    body = client.get("/leads").text
    assert "Acorp" in body and "Bcorp" not in body        # A sees only A's buyers


def test_trader_cannot_open_others_records(ctx):
    client, _, ids = ctx
    b_lead, b_quote, b_deal = ids["b"]
    _login(client, "a@t.local")
    assert client.get(f"/leads/{b_lead}", follow_redirects=False).status_code == 404
    assert client.get(f"/quotes/{b_quote}", follow_redirects=False).status_code == 404
    assert client.get(f"/deals/{b_deal}", follow_redirects=False).status_code == 404


def test_trader_cannot_mutate_others_lead(ctx):
    client, _, ids = ctx
    b_lead, _, _ = ids["b"]
    _login(client, "a@t.local")
    assert client.post(f"/leads/{b_lead}/note", data={"body": "x"},
                       follow_redirects=False).status_code == 404


def test_moat_and_config_are_admin_only(ctx):
    client, _, _ = ctx
    _login(client, "a@t.local")
    for path in ("/command", "/research", "/suppliers", "/catalog", "/rates", "/ingest",
                 "/admin/users", "/admin/activity", "/lines/cd-dvd", "/uae", "/intel", "/georgia"):
        assert client.get(path, follow_redirects=False).status_code == 403, path
    assert client.post("/admin/users", data={"email": "x@t.local", "password": "secret123"},
                       follow_redirects=False).status_code == 403


def test_admin_sees_all_and_can_open_any_record(ctx):
    client, _, ids = ctx
    b_lead, b_quote, b_deal = ids["b"]
    _login(client, "admin@t.local")
    body = client.get("/leads").text
    assert "Acorp" in body and "Bcorp" in body            # admin sees everyone
    assert client.get(f"/leads/{b_lead}").status_code == 200
    assert client.get(f"/quotes/{b_quote}").status_code == 200
    assert client.get(f"/deals/{b_deal}").status_code == 200
    assert client.get("/admin/users").status_code == 200
    assert client.get("/admin/activity").status_code == 200


def test_reassign_cascades_owner_to_children(ctx):
    """Admin reassigns a lead A→B: its quotes + deals must follow to B (else A keeps reaching them
    and B is locked out). Regression for the audit's one finding."""
    client, engine, ids = ctx
    a_lead, a_quote, a_deal = ids["a"]
    with Session(engine) as s:
        b_id = s.exec(select(User).where(User.email == "b@t.local")).first().id
    _login(client, "admin@t.local")
    assert client.post(f"/leads/{a_lead}/assign", data={"owner_id": b_id},
                       follow_redirects=False).status_code == 303
    with Session(engine) as s:
        assert s.get(Quote, a_quote).owner_id == b_id and s.get(Deal, a_deal).owner_id == b_id
    _login(client, "a@t.local")          # former owner locked out
    assert client.get(f"/quotes/{a_quote}", follow_redirects=False).status_code == 404
    assert client.get(f"/deals/{a_deal}", follow_redirects=False).status_code == 404
    _login(client, "b@t.local")          # new owner can reach them
    assert client.get(f"/quotes/{a_quote}").status_code == 200
    assert client.get(f"/deals/{a_deal}").status_code == 200


def test_trader_dashboard_shows_request_form_not_moat(ctx):
    client, _, _ = ctx
    _login(client, "a@t.local")
    body = client.get("/").text
    assert "Request a buyer search" in body               # the concierge entry point
    assert "open Research" not in body and "What we offer" not in body   # founder-internal, hidden


def test_request_flow_submit_scope_and_approve(ctx):
    client, engine, _ = ctx
    _login(client, "a@t.local")
    assert client.post("/requests", data={"product": "Zinc sulphate", "market": "Iraq",
                                          "details": "33% agri grade"},
                       follow_redirects=False).status_code == 303
    with Session(engine) as s:
        sr = s.exec(select(ServiceRequest)).first()
        assert sr and sr.status == "submitted" and sr.product == "Zinc sulphate"
        assert sr.owner_id == sr.requester_id and sr.tracking_code.startswith("SR-")
        rid, code = sr.id, sr.tracking_code       # tracking code is unique (won't collide w/ placeholders)
    assert code in client.get("/requests").text          # A sees their request
    _login(client, "b@t.local")
    assert code not in client.get("/requests").text      # B does not
    assert client.get(f"/requests/{rid}/status", follow_redirects=False).status_code == 404
    assert client.post(f"/admin/requests/{rid}/approve",
                       follow_redirects=False).status_code == 403    # traders can't approve
    _login(client, "admin@t.local")
    assert code in client.get("/admin/requests").text     # in the founder's queue
    assert client.post(f"/admin/requests/{rid}/approve", follow_redirects=False).status_code == 303
    with Session(engine) as s:
        assert s.get(ServiceRequest, rid).status == "approved"


def test_services_page_lists_all(ctx):
    client, _, _ = ctx
    _login(client, "a@t.local")
    body = client.get("/services").text
    for label in ("Find buyers", "Remittance", "Contract drafting", "Freight", "Documentation"):
        assert label in body


def test_service_file_delivery_is_owner_only(ctx, tmp_path, monkeypatch):
    client, engine, _ = ctx
    monkeypatch.setattr(main, "REQUEST_FILES_DIR", tmp_path)      # keep the test off the real FS
    _login(client, "a@t.local")
    assert client.post("/requests", data={"request_type": "contract", "product": "Sale contract",
                                          "market": "Iraq", "details": "std terms"},
                       follow_redirects=False).status_code == 303
    with Session(engine) as s:
        rid = s.exec(select(ServiceRequest)).first().id
    _login(client, "admin@t.local")
    client.post(f"/admin/requests/{rid}/approve", follow_redirects=False)
    assert client.post(f"/admin/requests/{rid}/done", data={"result": "Contract ready"},
                       files={"file": ("contract.pdf", b"%PDF-1.4 fake", "application/pdf")},
                       follow_redirects=False).status_code == 303
    with Session(engine) as s:
        sr = s.get(ServiceRequest, rid)
        assert sr.status == "done" and sr.result_file_path
    _login(client, "a@t.local")                                   # owner downloads
    dl = client.get(f"/requests/{rid}/result/file")
    assert dl.status_code == 200 and b"PDF" in dl.content
    _login(client, "b@t.local")                                   # other trader cannot
    assert client.get(f"/requests/{rid}/result/file", follow_redirects=False).status_code == 404


def test_admin_can_create_user_no_public_signup(ctx):
    client, engine, _ = ctx
    _login(client, "admin@t.local")
    # no public signup route exists (checked while authenticated so the auth-gate 303 doesn't mask it)
    assert client.post("/register", data={}, follow_redirects=False).status_code in (404, 405)
    assert client.post("/admin/users",
                       data={"email": "new@t.local", "name": "New", "role": "agent", "password": "secret123"},
                       follow_redirects=False).status_code == 303
    with Session(engine) as s:
        u = s.exec(select(User).where(User.email == "new@t.local")).first()
        assert u is not None and u.role == "agent" and u.active
