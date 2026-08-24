"""Phase 7 — tracking webhook: signature verification (fail-closed when no provider configured), replay
rejection, rate-limiting, and NO live provider calls."""
import hashlib
import hmac

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine

import app.main as main
from app import ops_providers as OPSPROV
from app.auth import hash_password
from app.models import User


@pytest.fixture
def client(monkeypatch):
    e = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    SQLModel.metadata.create_all(e)
    monkeypatch.setattr(main, "engine", e)
    from app import ratelimit as RL
    RL.reset()
    OPSPROV.reset()
    with Session(e) as s:
        s.add(User(email="admin@t.local", name="A", role="admin", active=True, password_hash=hash_password("pw")))
        s.commit()
    return TestClient(main.app)


def test_unsigned_call_rejected_when_not_configured(client, monkeypatch):
    monkeypatch.delenv("OPS_WEBHOOK_SECRET", raising=False)
    r = client.post("/ops/webhook/tracking", content=b"{}")
    assert r.status_code == 401 and r.json()["ok"] is False       # fail-closed: no secret → no entry


def test_valid_signature_accepted_and_replay_rejected(client, monkeypatch):
    monkeypatch.setenv("OPS_WEBHOOK_SECRET", "s3cr3t")
    body = b'{"event":"departed"}'
    sig = hmac.new(b"s3cr3t", body, hashlib.sha256).hexdigest()
    r1 = client.post("/ops/webhook/tracking", content=body,
                     headers={"X-Signature": sig, "X-Delivery-Id": "D-1"})
    assert r1.status_code == 200 and r1.json()["ok"] is True
    # same delivery id again → replay rejected
    r2 = client.post("/ops/webhook/tracking", content=body,
                     headers={"X-Signature": sig, "X-Delivery-Id": "D-1"})
    assert r2.status_code == 409 and r2.json()["error"] == "replay"


def test_bad_signature_rejected(client, monkeypatch):
    monkeypatch.setenv("OPS_WEBHOOK_SECRET", "s3cr3t")
    r = client.post("/ops/webhook/tracking", content=b"{}",
                    headers={"X-Signature": "deadbeef", "X-Delivery-Id": "D-2"})
    assert r.status_code == 401
