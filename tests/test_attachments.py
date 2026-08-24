"""Phase 4 hardening — attachments are DISABLED (no partial/unprotected handling).

The single choke point app/attachments.py refuses every attachment while disabled, the compose UIs show the
reason, and the outreach compose pages stay admin-only (seller-access blocked). The secure-path validator is
exercised (dangerous format, path traversal, extension/MIME mismatch, oversize, authorization) so the logic is
real the day it's switched on — it is just inactive while ATTACHMENTS_ENABLED is off.
"""
import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine, select

import app.main as main
from app import attachments as ATT
from app.auth import hash_password
from app.models import Lead, MailAccount, User


def test_attachments_disabled_by_default():
    assert ATT.attachments_enabled() is False
    assert ATT.DISABLED_MESSAGE == "Attachments are temporarily disabled for security."


def test_validate_refuses_everything_while_disabled():
    for name, mime, size in [("quote.pdf", "application/pdf", 100), ("a.png", "image/png", 1),
                             ("x.exe", "application/octet-stream", 1)]:
        ok, reason = ATT.validate_attachment(name, mime, size)
        assert ok is False and reason == ATT.DISABLED_MESSAGE


def test_reject_if_present_raises_when_disabled():
    with pytest.raises(HTTPException) as e:
        ATT.reject_if_present([("f", "x.pdf")])
    assert e.value.status_code == 400 and "disabled" in e.value.detail.lower()
    ATT.reject_if_present([])          # nothing present → no raise


def test_secure_validator_logic_when_enabled(monkeypatch):
    """The future secure path (inactive while disabled) actually enforces its rules."""
    monkeypatch.setattr(ATT, "ATTACHMENTS_ENABLED", True)
    assert ATT.validate_attachment("quote.pdf", "application/pdf", 1000) == (True, "")
    # dangerous format
    assert ATT.validate_attachment("payload.exe", "application/octet-stream", 10)[0] is False
    assert ATT.validate_attachment("run.sh", "text/x-sh", 10)[0] is False
    # path traversal / unsafe filename
    assert ATT.validate_attachment("../../etc/passwd", "text/plain", 10)[0] is False
    assert ATT.validate_attachment("a/b.pdf", "application/pdf", 10)[0] is False
    # extension not allowed
    assert ATT.validate_attachment("data.iso", "application/octet-stream", 10)[0] is False
    # extension/MIME mismatch (claims PDF, isn't)
    assert ATT.validate_attachment("notreally.png", "application/pdf", 10)[0] is False
    # oversize
    assert ATT.validate_attachment("big.pdf", "application/pdf", ATT.MAX_BYTES + 1)[0] is False


@pytest.fixture
def ctx(monkeypatch):
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    SQLModel.metadata.create_all(engine)
    monkeypatch.setattr(main, "engine", engine)
    with Session(engine) as s:
        s.add(User(email="admin@t.local", name="A", role="admin", active=True, password_hash=hash_password("pw")))
        s.add(User(email="kim@t.local", name="K", role="agent", active=True, password_hash=hash_password("pw")))
        s.commit()
        ids = {u.email.split("@")[0]: u.id for u in s.exec(select(User)).all()}
        s.add(MailAccount(user_id=ids["admin"], email="hunt@go4it.vip", admin_owned=True, active=True))
        ld = Lead(product="copper", managed=True, seller_id=ids["kim"], email="b@x.com")
        s.add(ld); s.commit(); s.refresh(ld)
        ids["lead"] = ld.id
    return TestClient(main.app), ids


def _login(c, email):
    assert c.post("/login", data={"email": email, "password": "pw"}, follow_redirects=False).status_code == 303


def test_disabled_message_shows_on_compose_and_seller_blocked(ctx):
    client, ids = ctx
    _login(client, "admin@t.local")
    body = client.get(f"/inbox/{ids['lead']}").text
    assert "Attachments are temporarily disabled for security." in body
    # seller-access: the outreach compose is admin-only
    seller = TestClient(main.app); _login(seller, "kim@t.local")
    assert seller.get(f"/inbox/{ids['lead']}").status_code == 403
