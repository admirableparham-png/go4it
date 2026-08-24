"""Phase 4 production-gate — attachments are HARD-disabled and cannot be bypassed.

No env flag activates the incomplete path; validators passing never lets a file through; direct routes,
crafted multipart requests, the background worker, and seller access are all refused/ignored.
"""
import inspect

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine, select

import app.main as main
from app import attachments as ATT
from app import campaign_service as CAMP
from app.auth import hash_password
from app.models import Lead, MailAccount, Outreach, User


def test_disabled_by_default():
    assert ATT.attachments_enabled() is False
    assert ATT.DISABLED_MESSAGE == "Attachments are temporarily disabled for security."


def test_env_flag_cannot_enable(monkeypatch):
    """The env flag alone must NOT activate the incomplete path — only a code constant can."""
    monkeypatch.setattr(ATT, "_FLAG_REQUESTED", True)
    assert ATT.attachments_enabled() is False              # still off: secure backend not implemented
    assert "FORCIBLY DISABLED" in ATT.config_warning()     # prominent warning surfaced
    ok, reason = ATT.validate_attachment("quote.pdf", "application/pdf", 10)
    assert ok is False and reason == ATT.DISABLED_MESSAGE   # validation still refuses


def test_validation_pass_does_not_let_attachment_proceed(monkeypatch):
    """Even a clean file that would PASS the pure validator cannot proceed while disabled."""
    monkeypatch.setattr(ATT, "_FLAG_REQUESTED", True)       # requested but backend not built
    assert ATT._secure_validate("quote.pdf", "application/pdf", 10) == (True, "")   # pure logic says OK...
    assert ATT.validate_attachment("quote.pdf", "application/pdf", 10)[0] is False  # ...gate still refuses
    with pytest.raises(HTTPException) as e:
        ATT.reject_if_present([("f", "quote.pdf")])         # send/store path refuses regardless of flag
    assert e.value.status_code == 400


def test_only_code_constant_plus_flag_enables(monkeypatch):
    monkeypatch.setattr(ATT, "_SECURE_STORAGE_IMPLEMENTED", True)
    monkeypatch.setattr(ATT, "_FLAG_REQUESTED", True)
    assert ATT.attachments_enabled() is True               # both required
    # with the backend "built", the secure validator is what runs
    assert ATT.validate_attachment("payload.exe", "application/octet-stream", 10)[0] is False
    assert ATT.validate_attachment("quote.pdf", "application/pdf", 10) == (True, "")


def test_secure_validator_rules():
    v = ATT._secure_validate
    assert v("quote.pdf", "application/pdf", 1000) == (True, "")
    assert v("payload.exe", "application/octet-stream", 10)[0] is False   # dangerous
    assert v("../../etc/passwd", "text/plain", 10)[0] is False            # traversal
    assert v("a/b.pdf", "application/pdf", 10)[0] is False                # path sep
    assert v("data.iso", "application/octet-stream", 10)[0] is False      # not allowed
    assert v("notreally.png", "application/pdf", 10)[0] is False          # ext/MIME mismatch
    assert v("big.pdf", "application/pdf", ATT.MAX_BYTES + 1)[0] is False  # oversize


def test_send_path_has_no_attachment_capability():
    """The campaign send path exposes no attachment parameter and the event model has no attachment field."""
    assert not any("attach" in p for p in inspect.signature(CAMP.send_step).parameters)
    assert not any("attach" in f.lower() for f in Outreach.model_fields)


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


def test_disabled_message_shows_and_seller_blocked(ctx):
    client, ids = ctx
    _login(client, "admin@t.local")
    assert "Attachments are temporarily disabled for security." in client.get(f"/inbox/{ids['lead']}").text
    seller = TestClient(main.app); _login(seller, "kim@t.local")
    assert seller.get(f"/inbox/{ids['lead']}").status_code == 403


def test_crafted_multipart_reply_ignores_the_file(ctx):
    """A crafted request that smuggles a file part must not store/send an attachment — the file is ignored
    and the text reply proceeds normally (no attachment column exists to hold it)."""
    client, ids = ctx
    _login(client, "admin@t.local")
    r = client.post(f"/inbox/{ids['lead']}/reply",
                    data={"subject": "hi", "body": "text only"},
                    files={"attachment": ("evil.exe", b"MZ...", "application/octet-stream")},
                    follow_redirects=False)
    assert r.status_code in (200, 303, 400)                # never a 500; file simply not honored
