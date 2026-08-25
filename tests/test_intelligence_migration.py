"""Phase 8 — intelligence gate migration + conservative backfill on a disposable DB.

The gate creates the 7 tables + partial-unique indexes + additive WorkItem columns idempotently; the backfill
creates demand signals + opportunities ONLY from deterministic evidence (an accepted quote here), marks them
inferred, keeps operational counts invariant, and rollback removes the backfill-only rows while preserving
every operational row."""
import importlib
from datetime import datetime

import pytest
from sqlalchemy import create_engine, inspect, text
from sqlmodel import Session, SQLModel, select

import app.models  # noqa: F401
from app.models import DemandSignal, Lead, Opportunity, Product, Quote, User

_NEW = ("demandsignal", "opportunity", "opportunitysignal", "opportunitymatch", "analyticssnapshot",
        "intelalert", "analyticsreport")


@pytest.fixture
def diskdb(tmp_path, monkeypatch):
    db = tmp_path / "p8.db"
    engine = create_engine(f"sqlite:///{db}")
    SQLModel.metadata.create_all(engine)
    with engine.begin() as c:
        for t in _NEW:
            c.execute(text(f"DROP TABLE IF EXISTS {t}"))
    with Session(engine) as s:
        s.add(User(email="a@t.local", name="A", role="admin", active=True, password_hash="x")); s.commit()
        lead = Lead(product="Zinc Sulphate", category="chemicals", dest_country="GE", tracking_code="G4-1",
                    owner_id=1)
        s.add(lead); s.commit(); s.refresh(lead)
        p = Product(name="Zinc Sulphate Monohydrate", category="chemicals", exw_price=590, status="active",
                    verification_status="verified", completeness_score=80)
        s.add(p); s.commit(); s.refresh(p)
        q = Quote(lead_id=lead.id, product_id=p.id, owner_id=1, status="accepted", buyer_response="accepted",
                  tracking_code="G4-1-Q1", accepted_at=datetime.utcnow(), current_version_id=1)
        s.add(q); s.commit()
    import scripts.migrate_gate_p8 as G
    import scripts.backfill_intelligence as B
    importlib.reload(G); importlib.reload(B)
    monkeypatch.setattr(G, "engine", engine); monkeypatch.setattr(G, "_is_sqlite", True)
    monkeypatch.setattr(B, "engine", engine); monkeypatch.setattr(B, "init_db", lambda: None)
    return G, B, engine


def test_gate_creates_tables_idempotent(diskdb):
    G, B, engine = diskdb
    G.main(dry=True)
    assert "demandsignal" not in inspect(engine).get_table_names()   # dry-run changed nothing
    G.main(dry=False)
    for t in ("demandsignal", "opportunity", "intelalert", "analyticsreport"):
        assert t in inspect(engine).get_table_names()
    assert G._plan() == [] and G._verify() == []
    G.main(dry=False)   # idempotent re-run


def test_gate_adds_workitem_columns(diskdb):
    G, B, engine = diskdb
    G.main(dry=False)
    cols = {c["name"] for c in inspect(engine).get_columns("workitem")}
    assert {"related_opportunity_id", "related_alert_id"} <= cols


def test_backfill_from_deterministic_evidence_only(diskdb):
    G, B, engine = diskdb
    G.main(dry=False)
    B.migrate(dry=True)
    with Session(engine) as s:
        assert s.exec(select(DemandSignal)).all() == []              # dry-run created nothing
    B.migrate(dry=False)
    with Session(engine) as s:
        sigs = s.exec(select(DemandSignal)).all()
        assert len(sigs) == 1 and sigs[0].signal_type == "accepted_quote" and sigs[0].inferred is True
        assert len(s.exec(select(Opportunity)).all()) == 1
        assert len(s.exec(select(Quote)).all()) == 1                 # operational rows untouched
    B.migrate(dry=False)   # idempotent — no duplicate signal
    with Session(engine) as s:
        assert len(s.exec(select(DemandSignal)).all()) == 1


def test_rollback_removes_backfill_only(diskdb):
    G, B, engine = diskdb
    G.main(dry=False); B.migrate(dry=False)
    with Session(engine) as s:
        nq = len(s.exec(select(Quote)).all())
    B.rollback()
    with Session(engine) as s:
        assert s.exec(select(Opportunity)).all() == []              # backfill-only opportunity removed
        assert s.exec(select(DemandSignal).where(DemandSignal.inferred == True)).all() == []  # noqa: E712
        assert len(s.exec(select(Quote)).all()) == nq               # operational rows preserved
