"""Phase 5 — product/pricing Work Queue scanners: idempotent, condition-versioned, auto-resolving."""
import pytest
from datetime import datetime, timedelta

from sqlalchemy import text
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine, select

from app import work_queue as WQ
from app.models import CostRate, Product, ProductPriceVersion, User, WorkItem


@pytest.fixture
def db():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    SQLModel.metadata.create_all(engine)
    with engine.connect() as c:
        c.execute(text("CREATE UNIQUE INDEX IF NOT EXISTS uq_workitem_idem_open ON workitem(idempotency_key) "
                       "WHERE idempotency_key != '' AND status IN ('open','in_progress','waiting')"))
        c.commit()
    with Session(engine) as s:
        s.add(User(email="admin@t.local", name="A", role="admin", active=True, password_hash="x")); s.commit()
    return engine


def test_incomplete_product_task_idempotent_and_resolves(db):
    with Session(db) as s:
        p = Product(name="Bare", active=True)               # missing hs/origin/unit/price/supplier/category
        s.add(p); s.commit(); s.refresh(p)
        assert WQ.sync_incomplete_products(s) == 1; s.commit()
        assert WQ.sync_incomplete_products(s) == 0; s.commit()   # idempotent — no duplicate OPEN task
        assert len(s.exec(select(WorkItem).where(WorkItem.type == "product_incomplete")).all()) == 1
        # complete the product → task auto-resolves
        p = s.get(Product, p.id)
        p.hs_code, p.origin_country, p.unit, p.exw_price = "7403", "IR", "tonne", 100
        p.category = "Metals"; p.supplier_id = 1
        s.add(p); s.commit()
        WQ.sync_incomplete_products(s); s.commit()
        wi = s.exec(select(WorkItem).where(WorkItem.type == "product_incomplete")).first()
        assert wi.status == "completed"


def test_incomplete_task_reopens_on_new_missing_field(db):
    with Session(db) as s:
        p = Product(name="P", active=True, hs_code="7403", origin_country="IR", unit="t", exw_price=1,
                    category="M", supplier_id=1)
        s.add(p); s.commit(); s.refresh(p)
        assert WQ.sync_incomplete_products(s) == 0; s.commit()   # complete → no task
        p = s.get(Product, p.id); p.hs_code = ""; s.add(p); s.commit()   # break a field
        assert WQ.sync_incomplete_products(s) == 1; s.commit()          # new condition_version → task


def test_expired_price_and_needs_approval(db):
    with Session(db) as s:
        p = Product(name="P"); s.add(p); s.commit(); s.refresh(p)
        s.add(ProductPriceVersion(product_id=p.id, status="needs_review", version=1))
        s.add(ProductPriceVersion(product_id=p.id, status="approved", version=2,
                                  rate_valid_until=datetime.utcnow() - timedelta(days=1)))
        s.commit()
        WQ.sync_expired_price_versions(s); s.commit()
        types = {w.type for w in s.exec(select(WorkItem)).all()}
        assert "price_needs_approval" in types and "expired_price" in types
        n_before = len(s.exec(select(WorkItem)).all())
        WQ.sync_expired_price_versions(s); s.commit()        # idempotent
        assert len(s.exec(select(WorkItem)).all()) == n_before


def test_expired_cost_rate(db):
    with Session(db) as s:
        s.add(CostRate(rate_type="intl_freight", amount=1000, status="active",
                       valid_until=datetime.utcnow() - timedelta(days=2))); s.commit()
        assert WQ.sync_expired_cost_rates(s) == 1; s.commit()
        assert WQ.sync_expired_cost_rates(s) == 0; s.commit()


def test_scanners_registered(db):
    names = {n for n, _ in WQ._SCANNERS}
    assert {"product_incomplete", "expired_price", "expired_rate", "catalog_generation_failed"} <= names
