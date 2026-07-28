"""Tests for auth (hashing, roles) and pipeline transition rules."""
from app.auth import hash_password, role_at_least, verify_password
from app.main import TRANSITIONS
from app.models import User


def test_hash_and_verify_roundtrip():
    h = hash_password("s3cret!")
    assert h != "s3cret!"                 # never store plaintext
    assert verify_password("s3cret!", h)
    assert not verify_password("wrong", h)


def test_verify_rejects_garbage_hash():
    assert not verify_password("x", "not-a-valid-hash")


def test_role_hierarchy():
    admin = User(email="a", role="admin")
    manager = User(email="m", role="manager")
    agent = User(email="g", role="agent")
    viewer = User(email="v", role="viewer")
    assert role_at_least(admin, "manager")
    assert role_at_least(manager, "manager")
    assert not role_at_least(agent, "manager")     # agent can't approve/send
    assert role_at_least(agent, "agent")
    assert not role_at_least(viewer, "agent")       # viewer is read-only
    assert not role_at_least(None, "viewer")        # anonymous is nothing


def test_pipeline_transitions():
    assert "won" in TRANSITIONS["quoted"]
    assert "won" not in TRANSITIONS["new"]          # can't win before quoting
    assert "lost" in TRANSITIONS["new"]
    assert TRANSITIONS["won"] == set()              # terminal stage
