"""HTTP/route integration tests via TestClient — the wiring layer the unit tests don't cover:
auth gate, login, role 403s, the public pro-forma accept flow, and the 'won' acceptance gate.

app.main binds `engine` at import (to the real data.db), so we monkeypatch app.main.engine to a
fresh in-memory DB per test and seed against it. Routes create their Session from that module global.
"""
import pytest
from fastapi.testclient import TestClient
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine, select

import app.main as main
from app.auth import hash_password
from app.models import Lead, Product, Quote, User


@pytest.fixture
def ctx(monkeypatch):
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False},
                           poolclass=StaticPool)
    SQLModel.metadata.create_all(engine)
    monkeypatch.setattr(main, "engine", engine)
    with Session(engine) as s:
        s.add(User(email="admin@t.local", name="Admin", role="admin", active=True,
                   password_hash=hash_password("pw")))
        s.add(User(email="viewer@t.local", name="Vi", role="viewer", active=True,
                   password_hash=hash_password("pw")))
        s.commit()
    client = TestClient(main.app)
    return client, engine


def _login(client, email):
    r = client.post("/login", data={"email": email, "password": "pw"}, follow_redirects=False)
    assert r.status_code == 303


def _seed_quote(engine, lead_status="quoted", accepted=False):
    with Session(engine) as s:
        lead = Lead(product="Printable DVD-R", buyer_company="Cairo Imaging", status=lead_status,
                    dest_country="EG", email="buyer@cairo.eg")
        prod = Product(name="Printable DVD-R", unit="pcs", exw_price=0.1, weight_kg_per_unit=0.02)
        s.add(lead); s.add(prod); s.commit(); s.refresh(lead); s.refresh(prod)
        q = Quote(lead_id=lead.id, product_id=prod.id, quantity=5000, delivered_unit=0.12,
                  delivered_total=600, status="approved", share_token="tok-abc",
                  tracking_code="G4-202608-0001-Q1")
        s.add(q); s.commit(); s.refresh(q)
        return lead.id, q.share_token


# --- auth gate + login ---

def test_unauthenticated_redirects_to_login(ctx):
    client, _ = ctx
    r = client.get("/leads", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/login"


def test_login_then_dashboard_ok(ctx):
    client, _ = ctx
    _login(client, "admin@t.local")
    assert client.get("/").status_code == 200
    assert client.get("/research").status_code == 200
    assert client.get("/quotes").status_code == 200


# --- role gate ---

def test_viewer_cannot_write(ctx):
    client, engine = ctx
    lead_id, _ = _seed_quote(engine)
    _login(client, "viewer@t.local")
    r = client.post(f"/leads/{lead_id}/note", data={"body": "hi"}, follow_redirects=False)
    assert r.status_code == 403


# --- public pro-forma accept flow (Phase 3) ---

def test_public_proforma_get_and_accept(ctx):
    client, engine = ctx
    lead_id, token = _seed_quote(engine)
    # public GET works without auth and offers the accept button
    page = client.get(f"/p/{token}")
    assert page.status_code == 200 and "Accept this quotation" in page.text
    # buyer accepts on-platform (public POST)
    r = client.post(f"/p/{token}/respond", data={"action": "accept", "message": "Go ahead"})
    assert r.status_code == 200 and "accepted" in r.text.lower()
    with Session(engine) as s:
        q = s.exec(select(Quote).where(Quote.share_token == token)).first()
        lead = s.get(Lead, lead_id)
        assert q.buyer_response == "accepted" and q.accepted_at is not None
        assert lead.accepted_at is not None and lead.status == "negotiating"


# --- 'won' acceptance gate (Phase 3) ---

def test_won_blocked_without_acceptance_then_allowed(ctx):
    client, engine = ctx
    lead_id, _ = _seed_quote(engine, lead_status="quoted")
    _login(client, "admin@t.local")
    # no acceptance, no reason -> blocked
    r = client.post(f"/leads/{lead_id}/stage", data={"status": "won"}, follow_redirects=False)
    assert r.status_code == 303 and "error=accept" in r.headers["location"]
    with Session(engine) as s:
        assert s.get(Lead, lead_id).status == "quoted"
    # an explicit override reason is accepted
    r2 = client.post(f"/leads/{lead_id}/stage", data={"status": "won", "reason": "signed PO on paper"},
                     follow_redirects=False)
    assert r2.status_code == 303
    with Session(engine) as s:
        assert s.get(Lead, lead_id).status == "won"
