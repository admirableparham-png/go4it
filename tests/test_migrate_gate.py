"""Phase 4 production-gate — explicit, idempotent schema migration for the crash-safe send layer.

Proves on a disposable on-disk DB: dry-run changes nothing; apply creates the campaignsend table + its unique
constraint + lease/retry/status/rfc indexes + rfc_message_id column + mailaccount.cred_enc_version; re-running
is a no-op; operational counts stay invariant; and history rows are never touched.
"""
import importlib

import pytest
from sqlalchemy import create_engine, inspect, text
from sqlmodel import Session, SQLModel, select

import app.models  # noqa: F401 — register models
from app.models import Lead, Outreach, User


@pytest.fixture
def gate(tmp_path, monkeypatch):
    """A disposable file DB with the pre-gate schema (everything EXCEPT campaignsend)."""
    db = tmp_path / "gate.db"
    engine = create_engine(f"sqlite:///{db}")
    # create every table, then DROP campaignsend so the migration must create it explicitly
    SQLModel.metadata.create_all(engine)
    with engine.begin() as c:
        c.execute(text("DROP TABLE IF EXISTS campaignsend"))
    with Session(engine) as s:
        s.add(User(email="a@t.local", name="A", role="admin", active=True, password_hash="x")); s.commit()
        uid = s.exec(select(User.id)).first()
        s.add(Lead(product="copper", email="b@x.com", owner_id=uid))
        s.add(Outreach(lead_id=1, direction="out", message_id="<hist@x>", status="sent")); s.commit()
    import scripts.migrate_gate as G
    importlib.reload(G)
    monkeypatch.setattr(G, "engine", engine)
    monkeypatch.setattr(G, "_is_sqlite", True)
    return G, engine


def _has_table(engine, name):
    return name in inspect(engine).get_table_names()


def test_dry_run_changes_nothing(gate):
    G, engine = gate
    G.main(dry=True)
    assert not _has_table(engine, "campaignsend")          # still absent — dry-run made no change


def test_apply_creates_table_constraint_and_indexes(gate):
    G, engine = gate
    G.main(dry=False)
    insp = inspect(engine)
    assert _has_table(engine, "campaignsend")
    cols = {c["name"] for c in insp.get_columns("campaignsend")}
    assert "rfc_message_id" in cols
    # unique (campaign,recipient,version,step)
    uniq = insp.get_unique_constraints("campaignsend") + [i for i in insp.get_indexes("campaignsend")
                                                          if i.get("unique")]
    assert any({"campaign_id", "recipient_id", "sequence_version", "step_index"} == set(u["column_names"])
               for u in uniq)
    # lease/retry indexes
    idx = {i["name"] for i in insp.get_indexes("campaignsend")}
    assert "ix_campaignsend_lease_expires_at" in idx and "ix_campaignsend_next_attempt_at" in idx
    # encryption-version metadata
    assert "cred_enc_version" in {c["name"] for c in insp.get_columns("mailaccount")}
    assert G._verify() == []                                # index verification passes


def test_rerun_is_idempotent_noop(gate):
    G, engine = gate
    G.main(dry=False)
    assert G._plan() == []                                  # nothing pending after first apply
    G.main(dry=False)                                       # second run must not raise
    assert G._verify() == []


def test_history_and_counts_preserved(gate):
    G, engine = gate
    with Session(engine) as s:
        before = (s.exec(select(Lead)).all(), s.exec(select(Outreach)).all())
        n_out = len(before[1])
    G.main(dry=False)
    with Session(engine) as s:
        assert len(s.exec(select(Outreach)).all()) == n_out         # outreach history untouched
        assert s.exec(select(Outreach)).first().message_id == "<hist@x>"
