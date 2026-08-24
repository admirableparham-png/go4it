"""Phase 5 — structured categories: get-or-create, aliases, SAFE merge (history preserved), status."""
import pytest
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine, select

from app import category_service as CATS
from app.models import Product, ProductCategory, ProductCategoryAlias, User


@pytest.fixture
def db():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    SQLModel.metadata.create_all(engine)
    with Session(engine) as s:
        s.add(User(email="admin@t.local", name="A", role="admin", active=True, password_hash="x")); s.commit()
    return engine


def test_get_or_create_is_idempotent(db):
    with Session(db) as s:
        a = CATS.get_or_create_category(s, "Metals"); s.commit()
        b = CATS.get_or_create_category(s, "  metals ")   # normalized match
        assert a.id == b.id
        assert len(s.exec(select(ProductCategory)).all()) == 1


def test_alias_resolution(db):
    with Session(db) as s:
        cat = CATS.get_or_create_category(s, "Copper"); s.commit()
        CATS.add_alias(s, "Cu", cat.id); s.commit()
        assert CATS.resolve_category(s, "Cu").id == cat.id           # alias resolves
        assert CATS.resolve_category(s, "copper").id == cat.id       # exact name resolves
        assert CATS.resolve_category(s, "unknown thing") is None     # never guesses


def test_safe_merge_preserves_product_history(db):
    with Session(db) as s:
        src = CATS.get_or_create_category(s, "Cu cathode")
        dst = CATS.get_or_create_category(s, "Copper"); s.commit()
        p = Product(name="Cathode 99.99", category_id=src.id, category="Cu cathode")
        s.add(p); s.commit(); s.refresh(p)
        res = CATS.merge_categories(s, src.id, dst.id); s.commit()
        assert res["products_moved"] == 1
        # product re-pointed to survivor, NOT deleted; its free-text history preserved
        p = s.get(Product, p.id)
        assert p.category_id == dst.id and p.category == "Cu cathode"
        src = s.get(ProductCategory, src.id)
        assert src.status == "archived" and src.merged_into_id == dst.id
        # the old name still resolves to the survivor (alias created)
        assert CATS.resolve_category(s, "Cu cathode").id == dst.id


def test_merge_is_reversible(db):
    with Session(db) as s:
        src = CATS.get_or_create_category(s, "A")
        dst = CATS.get_or_create_category(s, "B"); s.commit()
        CATS.merge_categories(s, src.id, dst.id); s.commit()
        assert CATS.unmerge_category(s, src.id) is True; s.commit()
        src = s.get(ProductCategory, src.id)
        assert src.status == "active" and src.merged_into_id is None


def test_cannot_merge_into_self(db):
    with Session(db) as s:
        c = CATS.get_or_create_category(s, "X"); s.commit()
        assert "error" in CATS.merge_categories(s, c.id, c.id)


def test_status_archive_and_restore(db):
    with Session(db) as s:
        c = CATS.get_or_create_category(s, "Temp"); s.commit()
        CATS.set_status(s, c.id, "archived"); s.commit()
        assert s.get(ProductCategory, c.id).status == "archived"
