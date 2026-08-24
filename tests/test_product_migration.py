"""Phase 5 — explicit gate migration + conservative backfill on a disposable DB.

Gate: dry-run changes nothing; apply creates the 8 tables + constraints/indexes; re-run is a no-op;
operational counts invariant. Backfill: categorizes from free text (inferred), links suppliers, is idempotent,
rollback restores while PRESERVING the free-text category + all products.
"""
import importlib

import pytest
from sqlalchemy import create_engine, inspect, text
from sqlmodel import Session, SQLModel, select

import app.models  # noqa: F401
from app.models import Company, Product, ProductCategory, ProductSupplier, Supplier, User


@pytest.fixture
def diskdb(tmp_path, monkeypatch):
    db = tmp_path / "p5.db"
    engine = create_engine(f"sqlite:///{db}")
    SQLModel.metadata.create_all(engine)
    # simulate a pre-Phase-5 DB: drop the new tables so the gate must create them
    with engine.begin() as c:
        for t in ("productcategory", "productcategoryalias", "productvariant", "productsupplier", "costrate",
                  "productpriceversion", "productdocument", "cataloggenerationjob"):
            c.execute(text(f"DROP TABLE IF EXISTS {t}"))
    with Session(engine) as s:
        s.add(User(email="a@t.local", name="A", role="admin", active=True, password_hash="x")); s.commit()
        co = Company(name="Nat Copper", primary_role="supplier", country="IR"); s.add(co); s.commit()
        s.refresh(co)
        sup = Supplier(name="Nat Copper", company_id=co.id); s.add(sup); s.commit(); s.refresh(sup)
        s.add(Product(name="Copper", category="Metals", supplier_id=sup.id))
        s.add(Product(name="Zinc", category="Metals"))
        s.add(Product(name="Rug", category="Textiles")); s.commit()
    import scripts.migrate_gate_p5 as G
    import scripts.backfill_products as B
    importlib.reload(G); importlib.reload(B)
    monkeypatch.setattr(G, "engine", engine); monkeypatch.setattr(G, "_is_sqlite", True)
    monkeypatch.setattr(B, "engine", engine); monkeypatch.setattr(B, "init_db", lambda: None)
    return G, B, engine


def _has(engine, t):
    return t in inspect(engine).get_table_names()


def test_gate_dry_run_changes_nothing(diskdb):
    G, B, engine = diskdb
    G.main(dry=True)
    assert not _has(engine, "productsupplier")


def test_gate_apply_creates_tables_and_is_idempotent(diskdb):
    G, B, engine = diskdb
    G.main(dry=False)
    for t in ("productcategory", "productsupplier", "costrate", "productpriceversion", "cataloggenerationjob"):
        assert _has(engine, t)
    assert G._plan() == [] and G._verify() == []          # nothing pending, verification clean
    G.main(dry=False)                                     # re-run must not raise


def test_backfill_categorizes_and_links_idempotently(diskdb):
    G, B, engine = diskdb
    G.main(dry=False)
    B.migrate(dry=False)
    with Session(engine) as s:
        cats = s.exec(select(ProductCategory)).all()
        assert len(cats) == 2 and all(c.inferred for c in cats)      # Metals + Textiles, inferred
        assert len(s.exec(select(ProductSupplier)).all()) == 1        # the one product with a company-linked supplier
        assert all(p.category_id is not None for p in s.exec(select(Product)).all())
    B.migrate(dry=False)                                  # idempotent
    with Session(engine) as s:
        assert len(s.exec(select(ProductCategory)).all()) == 2


def test_backfill_does_not_invent(diskdb):
    G, B, engine = diskdb
    G.main(dry=False); B.migrate(dry=False)
    with Session(engine) as s:
        for p in s.exec(select(Product)).all():
            assert p.verification_status == "unverified"   # never invents verification
            assert p.hs_code == ""                         # never invents HS


def test_rollback_preserves_products_and_free_text(diskdb):
    G, B, engine = diskdb
    G.main(dry=False); B.migrate(dry=False)
    with Session(engine) as s:
        n_products = len(s.exec(select(Product)).all())
    B.rollback()
    with Session(engine) as s:
        assert len(s.exec(select(Product)).all()) == n_products                # products preserved
        assert s.exec(select(ProductCategory).where(ProductCategory.inferred == True)).all() == []  # noqa: E712
        # free-text category survives the rollback
        assert s.exec(select(Product).where(Product.name == "Copper")).one().category == "Metals"
        assert all(p.category_id is None for p in s.exec(select(Product)).all())
