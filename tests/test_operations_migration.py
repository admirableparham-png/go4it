"""Phase 7 — operations gate migration + conservative backfill on a disposable DB.

The gate creates the 15 operational tables + partial-unique indexes + additive WorkItem columns idempotently;
the backfill makes ONE inferred baseline OperationCase per existing Deal (never inventing shipments/freight/
payments/customs/delivery/remittance), is idempotent, keeps operational counts invariant, and rollback removes
only the inferred cases while preserving every Deal.
"""
import importlib

import pytest
from sqlalchemy import create_engine, inspect, text
from sqlmodel import Session, SQLModel, select

import app.models  # noqa: F401
from app.models import Deal, Lead, OperationCase, Product, Quote, User

_NEW = ("operationcase", "freightrequest", "freightoffer", "shipment", "shipmentleg", "shipmentevent",
        "documentrequirement", "tradedocument", "customscase", "deliveryconfirmation", "operationalexception",
        "paymentmilestone", "remittancecase", "settlement", "settlementadjustment")


@pytest.fixture
def diskdb(tmp_path, monkeypatch):
    db = tmp_path / "p7.db"
    engine = create_engine(f"sqlite:///{db}")
    SQLModel.metadata.create_all(engine)
    # simulate a pre-Phase-7 DB: drop the new tables + the additive workitem columns don't exist there
    with engine.begin() as c:
        for t in _NEW:
            c.execute(text(f"DROP TABLE IF EXISTS {t}"))
    with Session(engine) as s:
        s.add(User(email="a@t.local", name="A", role="admin", active=True, password_hash="x")); s.commit()
        ld = Lead(product="x", tracking_code="G4-1"); s.add(ld); s.commit(); s.refresh(ld)
        p = Product(name="Copper", unit="tonne", currency="USD"); s.add(p); s.commit(); s.refresh(p)
        q = Quote(lead_id=ld.id, product_id=p.id, tracking_code="G4-1-Q1", status="sent", quantity=10,
                  delivered_total=100.0, version=1); s.add(q); s.commit(); s.refresh(q)
        s.add(Deal(lead_id=ld.id, quote_id=q.id, tracking_code="G4-1-D", stage="delivered")); s.commit()
    import scripts.migrate_gate_p7 as G
    import scripts.backfill_operations as B
    importlib.reload(G); importlib.reload(B)
    monkeypatch.setattr(G, "engine", engine); monkeypatch.setattr(G, "_is_sqlite", True)
    monkeypatch.setattr(B, "engine", engine); monkeypatch.setattr(B, "init_db", lambda: None)
    return G, B, engine


def test_gate_creates_tables_idempotent(diskdb):
    G, B, engine = diskdb
    G.main(dry=True)
    assert "operationcase" not in inspect(engine).get_table_names()   # dry-run changed nothing
    G.main(dry=False)
    for t in ("operationcase", "shipment", "paymentmilestone", "settlement"):
        assert t in inspect(engine).get_table_names()
    assert G._plan() == [] and G._verify() == []
    G.main(dry=False)   # idempotent re-run


def test_gate_adds_workitem_columns(diskdb):
    G, B, engine = diskdb
    G.main(dry=False)
    cols = {c["name"] for c in inspect(engine).get_columns("workitem")}
    assert {"related_operation_case_id", "related_shipment_id", "related_payment_id",
            "related_exception_id"} <= cols


def test_backfill_creates_baseline_case_conservatively(diskdb):
    G, B, engine = diskdb
    G.main(dry=False)
    with Session(engine) as s:
        deals_before = len(s.exec(select(Deal)).all())
    B.migrate(dry=True)
    with Session(engine) as s:
        assert s.exec(select(OperationCase)).all() == []             # dry-run created nothing
    B.migrate(dry=False)
    with Session(engine) as s:
        cases = s.exec(select(OperationCase)).all()
        assert len(cases) == 1 and cases[0].inferred and cases[0].case_type == "deal"
        assert cases[0].reference.startswith("OP-")
        # NEVER invents operational execution records
        from app.models import FreightRequest, PaymentMilestone, Shipment
        assert s.exec(select(Shipment)).all() == []
        assert s.exec(select(FreightRequest)).all() == []
        assert s.exec(select(PaymentMilestone)).all() == []
        assert len(s.exec(select(Deal)).all()) == deals_before       # deals untouched
    B.migrate(dry=False)   # idempotent
    with Session(engine) as s:
        assert len(s.exec(select(OperationCase)).all()) == 1


def test_backfill_preserves_deal_stage(diskdb):
    G, B, engine = diskdb
    G.main(dry=False); B.migrate(dry=False)
    with Session(engine) as s:
        assert s.exec(select(Deal)).one().stage == "delivered"       # legacy stage preserved exactly


def test_rollback_removes_inferred_cases_only(diskdb):
    G, B, engine = diskdb
    G.main(dry=False); B.migrate(dry=False)
    with Session(engine) as s:
        nd = len(s.exec(select(Deal)).all())
    B.rollback()
    with Session(engine) as s:
        assert s.exec(select(OperationCase)).all() == []             # inferred cases gone
        assert len(s.exec(select(Deal)).all()) == nd                 # deal preserved
