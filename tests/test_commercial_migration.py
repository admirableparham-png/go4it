"""Phase 6 (B) — commercial gate migration + conservative backfill on a disposable DB.

Gate creates the 13 tables + unique constraints idempotently; backfill makes one inferred QuoteVersion per
existing quote (never approved/sent/tokened/dealed), links deals conservatively, is idempotent, and rollback
preserves every quote + deal + total.
"""
import importlib

import pytest
from sqlalchemy import create_engine, inspect, text
from sqlmodel import Session, SQLModel, select

import app.models  # noqa: F401
from app.models import Deal, Lead, Product, Quote, QuoteVersion, User


@pytest.fixture
def diskdb(tmp_path, monkeypatch):
    db = tmp_path / "p6.db"
    engine = create_engine(f"sqlite:///{db}")
    SQLModel.metadata.create_all(engine)
    with engine.begin() as c:
        for t in ("quoteversion", "quotelineitem", "quotestatusevent", "quoteapproval", "quoteaccesstoken",
                  "quotedocument", "contract", "contractversion", "contractparty", "contractstatusevent",
                  "contracttemplate", "contractdocument", "signatureevent"):
            c.execute(text(f"DROP TABLE IF EXISTS {t}"))
    with Session(engine) as s:
        s.add(User(email="a@t.local", name="A", role="admin", active=True, password_hash="x")); s.commit()
        ld = Lead(product="x", tracking_code="G4-1"); s.add(ld); s.commit(); s.refresh(ld)
        p = Product(name="Copper", unit="tonne", currency="USD"); s.add(p); s.commit(); s.refresh(p)
        # two existing quotes + one deal linked to the first quote
        q1 = Quote(lead_id=ld.id, product_id=p.id, tracking_code="G4-1-Q1", status="sent", quantity=10,
                   delivered_total=100.0, margin_pct=8, version=1); s.add(q1)
        q2 = Quote(lead_id=ld.id, product_id=p.id, tracking_code="G4-1-Q2", status="draft", quantity=5,
                   delivered_total=50.0, version=2); s.add(q2); s.commit(); s.refresh(q1)
        s.add(Deal(lead_id=ld.id, quote_id=q1.id, tracking_code="G4-1-D", stage="won")); s.commit()
    import scripts.migrate_gate_p6 as G
    import scripts.backfill_commercial as B
    importlib.reload(G); importlib.reload(B)
    monkeypatch.setattr(G, "engine", engine); monkeypatch.setattr(G, "_is_sqlite", True)
    monkeypatch.setattr(B, "engine", engine); monkeypatch.setattr(B, "init_db", lambda: None)
    return G, B, engine


def test_gate_creates_tables_idempotent(diskdb):
    G, B, engine = diskdb
    G.main(dry=True)
    assert "quoteversion" not in inspect(engine).get_table_names()   # dry-run changed nothing
    G.main(dry=False)
    for t in ("quoteversion", "quoteaccesstoken", "contract", "signatureevent"):
        assert t in inspect(engine).get_table_names()
    assert G._plan() == [] and G._verify() == []
    G.main(dry=False)   # idempotent re-run


def test_backfill_inferred_versions_preserve_data(diskdb):
    G, B, engine = diskdb
    G.main(dry=False)
    with Session(engine) as s:
        before = {q.id: q.delivered_total for q in s.exec(select(Quote)).all()}
    B.migrate(dry=False)
    with Session(engine) as s:
        vers = s.exec(select(QuoteVersion)).all()
        assert len(vers) == 2 and all(v.inferred for v in vers)
        # a draft quote's inferred version is NOT elevated to approved
        q2ver = next(v for v in vers if v.status == "draft")
        assert q2ver.status == "draft"
        # the deal is linked to its quote's version
        deal = s.exec(select(Deal)).one()
        assert deal.quote_version_id is not None
        # totals preserved
        assert {q.id: q.delivered_total for q in s.exec(select(Quote)).all()} == before
    B.migrate(dry=False)   # idempotent
    with Session(engine) as s:
        assert len(s.exec(select(QuoteVersion)).all()) == 2


def test_backfill_never_tokens_or_creates_deal(diskdb):
    G, B, engine = diskdb
    G.main(dry=False); B.migrate(dry=False)
    with Session(engine) as s:
        from app.models import QuoteAccessToken
        assert s.exec(select(QuoteAccessToken)).all() == []      # never mints a buyer token
        assert len(s.exec(select(Deal)).all()) == 1              # never creates a new deal


def test_rollback_preserves_quotes_and_deal(diskdb):
    G, B, engine = diskdb
    G.main(dry=False); B.migrate(dry=False)
    with Session(engine) as s:
        nq, nd = len(s.exec(select(Quote)).all()), len(s.exec(select(Deal)).all())
    B.rollback()
    with Session(engine) as s:
        assert len(s.exec(select(Quote)).all()) == nq and len(s.exec(select(Deal)).all()) == nd
        assert s.exec(select(QuoteVersion).where(QuoteVersion.inferred == True)).all() == []  # noqa: E712
        assert s.exec(select(Deal)).one().quote_version_id is None   # unlinked, deal preserved
