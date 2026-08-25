"""Phase 8 (A) — intelligence pages: admin-200 / seller-403, the redesigned admin dashboard renders the funnel
without leaking buyer identity, and GETs never mutate business data."""
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine, select

import app.main as main
from app.auth import hash_password
from app.models import AnalyticsSnapshot, Lead, User

PAGES = ["/intelligence", "/intelligence/demand", "/intelligence/opportunities", "/intelligence/performance",
         "/intelligence/reports", "/intelligence/sources"]


@pytest.fixture
def ctx(monkeypatch):
    e = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    SQLModel.metadata.create_all(e)
    monkeypatch.setattr(main, "engine", e)
    with Session(e) as s:
        s.add(User(email="admin@t.local", name="A", role="admin", active=True, password_hash=hash_password("pw")))
        s.add(User(email="seller@t.local", name="S", role="agent", active=True, password_hash=hash_password("pw")))
        s.commit()
        s.add(Lead(product="Zinc", tracking_code="G4-1", buyer_company="SECRET BUYER LLC",
                   contact_name="Jane Secret", email="jane@secretbuyer.com", owner_id=1,
                   reply_outcome="positive"))
        s.commit()
    return e


def _login(c, email):
    assert c.post("/login", data={"email": email, "password": "pw"}, follow_redirects=False).status_code == 303


def test_all_pages_admin_200(ctx):
    c = TestClient(main.app)
    _login(c, "admin@t.local")
    for p in PAGES:
        assert c.get(p).status_code == 200, p


def test_all_pages_seller_403(ctx):
    c = TestClient(main.app)
    _login(c, "seller@t.local")
    for p in PAGES:
        assert c.get(p).status_code == 403, p


def test_admin_dashboard_shows_funnel(ctx):
    c = TestClient(main.app)
    _login(c, "admin@t.local")
    body = c.get("/").text
    assert "Commercial funnel" in body and "Positive interest" in body


def test_intelligence_pages_no_buyer_pii(ctx):
    # admins legitimately see buyer names in the leads panels; but the INTELLIGENCE analytics pages must never
    # surface buyer identity/contact in a chart/label — those are aggregate + demand-signal views.
    c = TestClient(main.app)
    _login(c, "admin@t.local")
    for p in ["/intelligence", "/intelligence/sources", "/intelligence/demand", "/intelligence/performance",
              "/intelligence/opportunities"]:
        body = c.get(p).text
        for pii in ["SECRET BUYER", "Jane Secret", "secretbuyer.com"]:
            assert pii not in body, f"{pii} leaked on {p}"


def test_get_does_not_mutate_business_data(ctx):
    c = TestClient(main.app)
    _login(c, "admin@t.local")
    with Session(ctx) as s:
        before = len(s.exec(select(Lead)).all())
    for p in PAGES + ["/"]:
        c.get(p)
    with Session(ctx) as s:
        assert len(s.exec(select(Lead)).all()) == before   # no leads created/changed by a GET
