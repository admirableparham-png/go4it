"""Phase 11 — the gate (scripts/migrate_gate_p11.py) is additive, idempotent and count-invariant, reports existing
duplicate enrolments instead of crashing or deleting; and the deploy artifact never carries buyer PII / ops notes."""
import importlib
import pathlib
import shutil
import subprocess

import pytest
from sqlalchemy import create_engine, inspect, text
from sqlmodel import Session, SQLModel, select

import app.models  # noqa: F401 — register models
from app.models import Campaign, CampaignRecipient, User

ROOT = pathlib.Path(__file__).resolve().parents[1]


@pytest.fixture
def gate(tmp_path, monkeypatch):
    """A disposable file DB with the pre-Phase-11 schema."""
    eng = create_engine(f"sqlite:///{tmp_path / 'g11.db'}")
    SQLModel.metadata.create_all(eng)
    with eng.begin() as c:
        c.execute(text("DROP TABLE IF EXISTS inboundseen"))
        for table, col in (("mailaccount", "sender_company"), ("mailaccount", "postal_address"),
                           ("campaignstep", "body_html")):
            try:
                c.execute(text(f"ALTER TABLE {table} DROP COLUMN {col}"))
            except Exception:  # noqa: BLE001 — very old SQLite: the column check is then covered by create_all
                pass
    with Session(eng) as s:
        s.add(User(email="a@t.local", name="A", role="admin", active=True, password_hash="x")); s.commit()
        s.add(Campaign(name="C", status="paused")); s.commit()
        s.add(CampaignRecipient(campaign_id=1, lead_id=1, to_email="b@x.example")); s.commit()
    import scripts.migrate_gate_p11 as G
    importlib.reload(G)
    monkeypatch.setattr(G, "engine", eng)
    monkeypatch.setattr(G, "_is_sqlite", True)
    return G, eng


def _indexes(eng):
    return {i["name"] for i in inspect(eng).get_indexes("campaignrecipient")}


def test_dry_run_changes_nothing(gate, capsys):
    G, eng = gate
    G.main(dry=True)
    assert "inboundseen" not in inspect(eng).get_table_names()
    assert "uq_camprcpt_campaign_lead" not in _indexes(eng)
    assert "CREATE TABLE inboundseen" in capsys.readouterr().out


def test_apply_then_rerun_is_a_no_op(gate, capsys):
    G, eng = gate
    G.main(dry=False)
    insp = inspect(eng)
    assert "inboundseen" in insp.get_table_names()
    assert {"sender_company", "postal_address"} <= {c["name"] for c in insp.get_columns("mailaccount")}
    assert "body_html" in {c["name"] for c in insp.get_columns("campaignstep")}
    assert {"uq_camprcpt_campaign_lead", "uq_camprcpt_campaign_email"} <= _indexes(eng)
    capsys.readouterr()
    G.main(dry=False)
    assert "none (already up to date)" in capsys.readouterr().out
    with Session(eng) as s:
        assert len(s.exec(select(CampaignRecipient)).all()) == 1       # history untouched


def test_existing_duplicates_are_reported_not_deleted(gate, capsys):
    G, eng = gate
    with Session(eng) as s:
        s.add(CampaignRecipient(campaign_id=1, lead_id=1, to_email="b@x.example")); s.commit()
    with pytest.raises(SystemExit) as ex:
        G.main(dry=False)
    assert ex.value.code == 4
    assert "uq_camprcpt_campaign_lead" not in _indexes(eng)
    with Session(eng) as s:
        assert len(s.exec(select(CampaignRecipient)).all()) == 2       # nothing deleted
    assert "duplicate enrolments exist" in capsys.readouterr().out


def test_migrate_adds_the_phase11_columns():
    from scripts.migrate import MIGRATIONS
    for entry in (("mailaccount", "sender_company"), ("mailaccount", "postal_address"), ("campaignstep", "body_html")):
        assert entry in {(t, c) for t, c, _ in MIGRATIONS}


# ---- deploy hygiene --------------------------------------------------------------------------------------------
def test_image_and_archive_exclude_buyer_pii_and_ops_notes():
    dockerignore = (ROOT / ".dockerignore").read_text()
    assert "docs/prospects/" in dockerignore and "docs/*.md" in dockerignore
    attrs = (ROOT / ".gitattributes").read_text()
    for line in ("docs/prospects/** export-ignore", "docs/HANDOFF.md export-ignore", "CLAUDE.md export-ignore"):
        assert line in attrs
    for py in (ROOT / "app").rglob("*.py"):                            # the running app never reads these paths
        assert "docs/prospects" not in py.read_text(), py


@pytest.mark.skipif(shutil.which("git") is None, reason="git not installed")
def test_git_marks_the_files_export_ignore():
    out = subprocess.run(["git", "check-attr", "export-ignore", "docs/prospects/buyers_trsharks.json",
                          "docs/HANDOFF.md", "docs/research/anchors_buyers_by_country.json"],
                         cwd=ROOT, capture_output=True, text=True).stdout
    assert "buyers_trsharks.json: export-ignore: set" in out and "HANDOFF.md: export-ignore: set" in out
    assert "anchors_buyers_by_country.json: export-ignore: unspecified" in out   # runtime research data stays
