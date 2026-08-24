"""Phase 5 — product CSV import: preview, conservative identity (never name-only), idempotent apply,
ambiguous → review task, category-alias matching, supplier→Company linking."""
import pytest
from sqlalchemy import text
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine, select

from app import product_import as PI
from app.models import Company, Product, ProductSupplier, User, WorkItem

CSV = ("sku,name,category,hs_code,origin,unit,exw_price,moq,supplier\n"
       "CU-1,Copper cathode,Metals,7403,IR,tonne,8500,25,Nat Copper\n"
       "ZN-1,Zinc ingot,Metals,7901,IR,tonne,2600,20,Zinc Co\n")


@pytest.fixture
def db():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    SQLModel.metadata.create_all(engine)
    with engine.connect() as c:
        c.execute(text("CREATE UNIQUE INDEX IF NOT EXISTS uq_workitem_idem_open ON workitem(idempotency_key) "
                       "WHERE idempotency_key != '' AND status IN ('open','in_progress','waiting')"))
        c.execute(text("CREATE UNIQUE INDEX IF NOT EXISTS uq_productsupplier_pc "
                       "ON productsupplier(product_id, company_id)"))
        c.commit()
    with Session(engine) as s:
        s.add(User(email="admin@t.local", name="A", role="admin", active=True, password_hash="x")); s.commit()
    return engine


def test_parse_maps_aliases(db):
    rows, errors, mapping = PI.parse(CSV)
    assert len(rows) == 2 and not errors
    assert rows[0]["sku"] == "CU-1" and rows[0]["exw_price"] == 8500.0 and rows[0]["min_order_qty"] == 25.0


def test_parse_requires_name_or_sku(db):
    rows, errors, _ = PI.parse("category,price\nMetals,10\n")
    assert rows == [] and errors


def test_preview_classifies_without_writing(db):
    with Session(db) as s:
        prev = PI.preview(s, PI.parse(CSV)[0])
        assert prev["new"] == 2 and prev["update"] == 0
        assert s.exec(select(Product)).all() == []          # nothing written


def test_apply_creates_then_idempotent_update(db):
    with Session(db) as s:
        counts, _ = PI.apply(s, PI.parse(CSV)[0], actor=None); s.commit()
        assert counts["created"] == 2
        # re-run the SAME file → matched by SKU → updates, never duplicates
        counts2, _ = PI.apply(s, PI.parse(CSV)[0], actor=None); s.commit()
        assert counts2["created"] == 0 and counts2["updated"] == 2
        assert len(s.exec(select(Product)).all()) == 2


def test_never_matches_by_name_alone(db):
    with Session(db) as s:
        # two existing products share a NAME but differ by SKU → a new SKU is a NEW product, not a merge
        s.add(Product(name="Widget", sku="W-1")); s.add(Product(name="Widget", sku="W-2")); s.commit()
        counts, _ = PI.apply(s, PI.parse("sku,name\nW-3,Widget\n")[0], actor=None); s.commit()
        assert counts["created"] == 1                        # by-name would have matched; by-SKU it's new
        assert len(s.exec(select(Product)).all()) == 3


def test_ambiguous_match_goes_to_review(db):
    with Session(db) as s:
        # two products with the SAME hs+origin and no sku → an inbound hs+origin row is ambiguous
        s.add(Product(name="A", hs_code="7403", origin_country="IR"))
        s.add(Product(name="B", hs_code="7403", origin_country="IR")); s.commit()
        counts, report = PI.apply(s, PI.parse("name,hs_code,origin\nC,7403,IR\n")[0], actor=None); s.commit()
        assert counts["ambiguous"] == 1 and counts["created"] == 0
        assert s.exec(select(WorkItem).where(WorkItem.type == "ambiguous_import_match")).first() is not None


def test_supplier_linked_to_company(db):
    with Session(db) as s:
        PI.apply(s, PI.parse(CSV)[0], actor=None); s.commit()
        # each supplier name resolved to a canonical supplier Company + a ProductSupplier link
        assert s.exec(select(Company).where(Company.primary_role == "supplier")).first() is not None
        assert len(s.exec(select(ProductSupplier)).all()) == 2
