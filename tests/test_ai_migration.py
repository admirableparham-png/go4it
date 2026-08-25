"""Phase 9 — AI-command gate migration + conservative backfill on a disposable DB.

The gate creates the 10 AI tables + partial-unique indexes + additive WorkItem columns idempotently; the
backfill archives existing CommandJobs as IMPORTED conversations (encrypted, never re-executed, no actions/
citations/research), keeps operational counts invariant, and rollback removes only the imported archives."""
import importlib

import pytest
from sqlalchemy import create_engine, inspect, text
from sqlmodel import Session, SQLModel, select

import app.models  # noqa: F401
from app.models import AIConversation, AIMessage, CommandJob, Quote, User

_NEW = ("aipromptversion", "aiconversation", "aimessage", "aicitation", "aitoolinvocation",
        "aiactionproposal", "aiusagerecord", "automationrule", "automationrun", "aievaluationresult")


@pytest.fixture
def diskdb(tmp_path, monkeypatch):
    monkeypatch.setenv("AI_DATA_ENCRYPTION_KEYS", "test-ai-key-strong-0001")
    db = tmp_path / "p9.db"
    engine = create_engine(f"sqlite:///{db}")
    SQLModel.metadata.create_all(engine)
    with engine.begin() as c:
        for t in _NEW:
            c.execute(text(f"DROP TABLE IF EXISTS {t}"))
    with Session(engine) as s:
        s.add(User(email="a@t.local", name="A", role="admin", active=True, password_hash="x")); s.commit()
        s.add(CommandJob(prompt="honey buyers in Georgia", action="harvest_osm", status="ok",
                         note="+12 new leads", owner_id=1)); s.commit()
    import scripts.migrate_gate_p9 as G
    import scripts.backfill_command_history as B
    import app.ai_encryption
    importlib.reload(app.ai_encryption)
    importlib.reload(G); importlib.reload(B)
    monkeypatch.setattr(G, "engine", engine); monkeypatch.setattr(G, "_is_sqlite", True)
    monkeypatch.setattr(B, "engine", engine); monkeypatch.setattr(B, "init_db", lambda: None)
    return G, B, engine


def test_gate_creates_tables_idempotent(diskdb):
    G, B, engine = diskdb
    G.main(dry=True)
    assert "aiconversation" not in inspect(engine).get_table_names()
    G.main(dry=False)
    for t in ("aiconversation", "aimessage", "aiactionproposal", "automationrule"):
        assert t in inspect(engine).get_table_names()
    assert G._plan() == [] and G._verify() == []
    G.main(dry=False)   # idempotent


def test_gate_adds_workitem_columns(diskdb):
    G, B, engine = diskdb
    G.main(dry=False)
    cols = {c["name"] for c in inspect(engine).get_columns("workitem")}
    assert {"related_conversation_id", "related_proposal_id", "related_automation_id"} <= cols


def test_backfill_archives_command_history_conservatively(diskdb):
    G, B, engine = diskdb
    G.main(dry=False)
    B.migrate(dry=True)
    with Session(engine) as s:
        assert s.exec(select(AIConversation)).all() == []          # dry-run created nothing
    B.migrate(dry=False)
    with Session(engine) as s:
        convs = s.exec(select(AIConversation)).all()
        assert len(convs) == 1 and convs[0].imported is True and convs[0].status == "archived"
        msgs = s.exec(select(AIMessage)).all()
        assert len(msgs) == 1 and "honey" not in (msgs[0].content_enc or "").lower()   # encrypted at rest
        assert len(s.exec(select(Quote)).all()) == 0               # operational rows untouched (none here)
    B.migrate(dry=False)   # idempotent — no duplicate archive
    with Session(engine) as s:
        assert len(s.exec(select(AIConversation)).all()) == 1


def test_rollback_removes_imported_only(diskdb):
    G, B, engine = diskdb
    G.main(dry=False); B.migrate(dry=False)
    with Session(engine) as s:
        njobs = len(s.exec(select(CommandJob)).all())
    B.rollback()
    with Session(engine) as s:
        assert s.exec(select(AIConversation)).all() == []          # imported archives gone
        assert s.exec(select(AIMessage)).all() == []
        assert len(s.exec(select(CommandJob)).all()) == njobs       # original CommandJobs preserved
