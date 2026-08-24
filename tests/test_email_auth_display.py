"""Phase 4 hardening — SPF/DKIM/DMARC are displayed honestly.

No live probe exists, so a saved DB value is NEVER shown as passing/healthy. Empty → 'Not checked'. A live
result (added later, with checked_at) is shown verbatim and flagged stale past its freshness window.
"""
from datetime import datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine, select

import app.main as main
from app.auth import hash_password
from app.send_guard import mail_auth_display
from app.models import MailAccount, User


def test_helper_never_trusts_bare_db_value():
    assert mail_auth_display("") == ("Not checked", "unknown")
    assert mail_auth_display("pass", None) == ("Not checked", "unknown")     # saved-only → NOT shown as pass
    now = datetime(2026, 1, 10)
    assert mail_auth_display("pass", now, now=now) == ("Pass", "pass")        # only with a live check
    assert mail_auth_display("pass", now - timedelta(days=30), now=now)[1] == "stale"
    assert mail_auth_display("fail", now, now=now) == ("Fail", "fail")


@pytest.fixture
def ctx(monkeypatch):
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    SQLModel.metadata.create_all(engine)
    monkeypatch.setattr(main, "engine", engine)
    with Session(engine) as s:
        s.add(User(email="admin@t.local", name="A", role="admin", active=True, password_hash=hash_password("pw")))
        s.commit()
        uid = s.exec(select(User.id)).first()
        # a mailbox whose DB row CLAIMS spf=pass — the page must still say "Not checked"
        s.add(MailAccount(user_id=uid, email="hunt@go4it.vip", admin_owned=True, active=True,
                          spf_status="pass", dkim_status="pass", dmarc_status="pass"))
        s.commit()
    return TestClient(main.app)


def test_mail_page_shows_not_checked_not_pass(ctx):
    assert ctx.post("/login", data={"email": "admin@t.local", "password": "pw"},
                    follow_redirects=False).status_code == 303
    body = ctx.get("/mail").text
    assert "SPF:" in body and "Not checked" in body
    # the saved "pass" must NOT surface as a healthy claim
    assert "SPF: <b" in body and ">Pass<" not in body
