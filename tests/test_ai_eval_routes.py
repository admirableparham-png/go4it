"""Phase 9 (B) — the evaluation suite (all scenarios pass; regression raises a WQ item), the copilot B-routes
(approve/decline/brief/automation admin-only + seller-403), and budget/pause-all behavior."""
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine, select

import app.main as main
from app import ai_command as CMD
from app import ai_eval as EVAL
from app import ai_provider as PROV
from app.auth import hash_password
from app.models import AIActionProposal, User, WorkItem

_IDX = (
    "CREATE UNIQUE INDEX IF NOT EXISTS uq_workitem_idem_open ON workitem(idempotency_key) "
    "WHERE idempotency_key != '' AND status IN ('open','in_progress','waiting')",
    "CREATE UNIQUE INDEX IF NOT EXISTS uq_aiproposal_idem ON aiactionproposal(idempotency_key) "
    "WHERE idempotency_key != ''",
)


@pytest.fixture
def ctx(monkeypatch):
    e = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    SQLModel.metadata.create_all(e)
    with e.connect() as c:
        for ddl in _IDX:
            c.execute(text(ddl))
        c.commit()
    monkeypatch.setattr(main, "engine", e)
    with Session(e) as s:
        s.add(User(email="admin@t.local", name="A", role="admin", active=True, password_hash=hash_password("pw")))
        s.add(User(email="seller@t.local", name="S", role="agent", active=True, password_hash=hash_password("pw")))
        s.commit()
    return e


def _login(c, email):
    assert c.post("/login", data={"email": email, "password": "pw"}, follow_redirects=False).status_code == 303


def test_eval_suite_all_pass(ctx):
    run = EVAL.run_suite()
    assert run["all_passed"], [r for r in run["results"] if not r["passed"]]
    assert run["total"] >= 10


def test_eval_regression_raises_workitem(ctx):
    with Session(ctx) as s:
        fake = {"suite_version": "e1", "all_passed": False,
                "results": [{"scenario": "x", "passed": False, "detail": "boom"}], "passed": 0, "total": 1}
        EVAL.persist(s, fake)
        s.commit()
        assert s.exec(select(WorkItem).where(WorkItem.type == "ai_evaluation_regression")).first() is not None


def test_b_routes_admin_only(ctx):
    seller = TestClient(main.app); _login(seller, "seller@t.local")
    for p in ["/command/brief", "/command/automation"]:
        assert seller.get(p).status_code == 403, p
    assert seller.post("/command/automation", data={"name": "x", "trigger_type": "work_queue_overdue",
                       "action_type": "create_work_item"}, follow_redirects=False).status_code == 403
    admin = TestClient(main.app); _login(admin, "admin@t.local")
    assert admin.get("/command/brief").status_code == 200
    assert admin.get("/command/automation").status_code == 200


def test_approve_route_admin_only_and_wrong_nonce(ctx):
    # seed a proposal owned by admin
    with Session(ctx) as s:
        admin = s.exec(select(User).where(User.email == "admin@t.local")).one()
        conv = CMD.new_conversation(s, admin); s.commit()
        from app import ai_actions as ACT
        p, _ = ACT.propose(s, conv, action_type="create_work_item", payload={"title": "x"}, actor=admin)
        s.commit()
        pid = p.id
    seller = TestClient(main.app); _login(seller, "seller@t.local")
    assert seller.post(f"/command/proposals/{pid}/approve", data={"nonce": "x"},
                       follow_redirects=False).status_code == 403
    # admin with a wrong nonce is redirected (not executed); the proposal stays proposed
    admin_c = TestClient(main.app); _login(admin_c, "admin@t.local")
    admin_c.post(f"/command/proposals/{pid}/approve", data={"nonce": "wrong"}, follow_redirects=False)
    with Session(ctx) as s:
        assert s.get(AIActionProposal, pid).status == "proposed"


def test_pause_all_halts_provider_but_deterministic_still_answers(ctx, monkeypatch):
    # even with a (mock) provider configured, Pause-All stops LLM use; deterministic answers keep working
    import app.config as cfg
    monkeypatch.setattr(cfg, "AI_PROVIDER", "mock", raising=False)
    PROV.pause_all(True)
    try:
        with Session(ctx) as s:
            admin = s.exec(select(User).where(User.email == "admin@t.local")).one()
            conv = CMD.new_conversation(s, admin); s.commit()
            res = CMD.answer(s, conv, "What needs my attention today?", admin); s.commit()
            assert res["text"] and not res["refused"]           # deterministic answer despite pause
            from app.models import AIMessage
            assert s.exec(select(AIMessage).where(AIMessage.role == "assistant")).first().provider == ""
    finally:
        PROV.pause_all(False)


def test_budget_status_never_blocks_deterministic(ctx):
    with Session(ctx) as s:
        admin = s.exec(select(User).where(User.email == "admin@t.local")).one()
        b = PROV.budget_status(s, owner_id=admin.id)
        assert b["within"] is True and "daily_limit" in b
