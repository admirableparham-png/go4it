"""Phase 6 (B) — deal commercial layer: idempotent deal from accepted version, Phase-7 handoff readiness
(work items, no fake shipments), and quotes CSV export authz. Existing deal stages/gates unchanged."""
import pytest
from sqlalchemy import text
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine, select

import app.main as main
import app.db as db
from app import deal_service as DS, quote_service as QS, quote_workflow as QW
from app.auth import hash_password
from app.models import Deal, Lead, Product, Quote, QuoteVersion, User, WorkItem


@pytest.fixture
def ctx(monkeypatch):
    e = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    SQLModel.metadata.create_all(e)
    with e.connect() as c:
        for ddl in ("CREATE UNIQUE INDEX IF NOT EXISTS uq_deal_quote_version ON deal(quote_version_id) WHERE quote_version_id IS NOT NULL",
                    "CREATE UNIQUE INDEX IF NOT EXISTS uq_workitem_idem_open ON workitem(idempotency_key) WHERE idempotency_key != '' AND status IN ('open','in_progress','waiting')"):
            c.execute(text(ddl))
        c.commit()
    monkeypatch.setattr(main, "engine", e); monkeypatch.setattr(db, "engine", e)
    with Session(e) as s:
        s.add(User(email="admin@t.local", name="A", role="admin", active=True, password_hash=hash_password("pw")))
        s.add(User(email="kim@t.local", name="K", role="agent", active=True, password_hash=hash_password("pw")))
        s.commit()
    return e


def _accepted_quote(engine):
    with Session(engine) as s:
        ld = Lead(product="x", tracking_code="G4-1", dest_country="GE"); s.add(ld); s.commit(); s.refresh(ld)
        p = Product(name="Copper", exw_price=8000, weight_kg_per_unit=1000, min_order_qty=25, currency="USD",
                    unit="tonne", origin_country="IR"); s.add(p); s.commit(); s.refresh(p)
        q = QS.create_quote(s, ld, p); s.commit()
        ver = QS.ensure_version(s, q); s.commit()
        QW.transition(s, q, "approved"); QW.transition(s, q, "sent")
        q.status = "viewed"; QW.transition(s, q, "accepted"); s.commit()
        return q.id, ver.id


def test_deal_from_version_idempotent_and_handoff_tasks(ctx):
    qid, vid = _accepted_quote(ctx)
    with Session(ctx) as s:
        ver = s.get(QuoteVersion, vid)
        d1, created1 = DS.ensure_deal_for_quote_version(s, ver, actor=None)
        d2, created2 = DS.ensure_deal_for_quote_version(s, ver, actor=None)
        assert created1 is True and created2 is False and d1.id == d2.id      # exactly one deal
        assert len(s.exec(select(Deal)).all()) == 1
        assert d1.quote_version_id == vid and d1.stage == "won"               # not shipped — Phase 7 does that
        # a handoff task is created (no fake shipment)
        assert s.exec(select(WorkItem).where(WorkItem.type == "deal_missing_contract")).first() is not None


def test_ready_for_ops_blocked_without_contracts(ctx):
    qid, vid = _accepted_quote(ctx)
    with Session(ctx) as s:
        ver = s.get(QuoteVersion, vid)
        deal, _ = DS.ensure_deal_for_quote_version(s, ver, actor=None); s.commit()
        ready, blockers = DS.deal_ready_for_ops(s, deal)
        assert ready is False and "buyer contract" in blockers and "supplier contract" in blockers


def test_quotes_export_admin_only(ctx):
    from fastapi.testclient import TestClient
    _accepted_quote(ctx)
    admin = TestClient(main.app); admin.post("/login", data={"email": "admin@t.local", "password": "pw"},
                                             follow_redirects=False)
    r = admin.get("/quotes/export.csv")
    assert r.status_code == 200 and "ref,version,status" in r.text
    seller = TestClient(main.app); seller.post("/login", data={"email": "kim@t.local", "password": "pw"},
                                               follow_redirects=False)
    assert seller.get("/quotes/export.csv").status_code == 403
