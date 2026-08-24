"""Phase 5 — catalog workspace: admin/seller authorization, filters + pagination, product CRUD (audited,
no GET mutation), product↔supplier linking, duplicate prevention, bulk actions, and NO cost/margin leakage
to sellers."""
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine, select

import app.main as main
from app.auth import hash_password
from app.models import (AuditLog, Company, Product, ProductCategory, ProductSupplier, User)


def _indexes(engine):
    with engine.connect() as c:
        for ddl in (
            "CREATE UNIQUE INDEX IF NOT EXISTS uq_productsupplier_pc ON productsupplier(product_id, company_id)",
            "CREATE UNIQUE INDEX IF NOT EXISTS uq_workitem_idem_open ON workitem(idempotency_key) "
            "WHERE idempotency_key != '' AND status IN ('open','in_progress','waiting')",
        ):
            c.execute(text(ddl))
        c.commit()


@pytest.fixture
def ctx(monkeypatch):
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    SQLModel.metadata.create_all(engine)
    _indexes(engine)
    monkeypatch.setattr(main, "engine", engine)
    with Session(engine) as s:
        s.add(User(email="admin@t.local", name="A", role="admin", active=True, password_hash=hash_password("pw")))
        s.add(User(email="kim@t.local", name="K", role="agent", active=True, password_hash=hash_password("pw")))
        s.commit()
        ids = {u.email.split("@")[0]: u.id for u in s.exec(select(User)).all()}
        for i in range(60):
            s.add(Product(name=f"Prod {i}", sku=f"P-{i}", category="Metals", hs_code="7403",
                          origin_country="IR", unit="tonne", exw_price=1000 + i, currency="USD",
                          weight_kg_per_unit=1000, min_order_qty=25))
        co = Company(name="Nat Copper", primary_role="supplier", country="IR")
        s.add(co); s.commit()
        ids["company"] = s.exec(select(Company.id)).first()
        ids["product"] = s.exec(select(Product.id)).first()
    return TestClient(main.app), engine, ids


def _login(c, email):
    assert c.post("/login", data={"email": email, "password": "pw"}, follow_redirects=False).status_code == 303


def test_catalog_admin_only(ctx):
    client, engine, ids = ctx
    _login(client, "kim@t.local")
    for p in ("/catalog", "/categories", "/pricing", f"/catalog/products/{ids['product']}",
              "/catalog/export.csv"):
        assert client.get(p).status_code == 403, p
    admin = TestClient(main.app); _login(admin, "admin@t.local")
    for p in ("/catalog", "/categories", "/pricing", f"/catalog/products/{ids['product']}"):
        assert admin.get(p).status_code == 200, p


def test_pagination_does_not_load_everything(ctx):
    client, engine, ids = ctx
    _login(client, "admin@t.local")
    body = client.get("/catalog").text
    assert "Page 1 /" in body and "Prod 59" in body       # paginated (60 products, 50/page; newest first)
    assert "Prod 0" not in body                            # page 1 does not load the whole catalog
    assert client.get("/catalog?page=2").status_code == 200


def test_filters(ctx):
    client, engine, ids = ctx
    _login(client, "admin@t.local")
    assert "Prod 5" in client.get("/catalog?q=Prod 5").text
    assert client.get("/catalog?hs=no").status_code == 200
    assert client.get("/catalog?price=yes&origin=IR&sort=price").status_code == 200


def test_product_edit_is_audited_no_get_mutation(ctx):
    client, engine, ids = ctx
    _login(client, "admin@t.local")
    with Session(engine) as s:
        before = s.get(Product, ids["product"]).brand
    client.get(f"/catalog/products/{ids['product']}")     # GET must not mutate
    with Session(engine) as s:
        assert s.get(Product, ids["product"]).brand == before
    r = client.post(f"/catalog/products/{ids['product']}/edit",
                    data={"brand": "Acme", "hs_code": "7403", "name": "Renamed"}, follow_redirects=False)
    assert r.status_code == 303
    with Session(engine) as s:
        p = s.get(Product, ids["product"])
        assert p.brand == "Acme" and p.name == "Renamed"
        assert s.exec(select(AuditLog).where(AuditLog.action == "product_edit")).first() is not None


def test_supplier_link_idempotent(ctx):
    client, engine, ids = ctx
    _login(client, "admin@t.local")
    for _ in range(2):
        client.post(f"/catalog/products/{ids['product']}/suppliers",
                    data={"company_id": ids["company"]}, follow_redirects=False)
    with Session(engine) as s:
        links = s.exec(select(ProductSupplier).where(
            ProductSupplier.product_id == ids["product"])).all()
        assert len(links) == 1                              # unique (product, company) prevents duplicate


def test_bulk_category_assign(ctx):
    client, engine, ids = ctx
    _login(client, "admin@t.local")
    with Session(engine) as s:
        cat = ProductCategory(name="Refined", name_normalized="refined", status="active")
        s.add(cat); s.commit(); s.refresh(cat)
        cid = cat.id
        pids = [p.id for p in s.exec(select(Product).limit(3)).all()]
    client.post("/catalog/bulk", data={"action": "category", "category_id": cid,
                                       "product_ids": pids}, follow_redirects=False)
    with Session(engine) as s:
        assert all(s.get(Product, pid).category_id == cid for pid in pids)


def test_export_admin_only_and_audited(ctx):
    client, engine, ids = ctx
    _login(client, "admin@t.local")
    r = client.get("/catalog/export.csv")
    assert r.status_code == 200 and "id,sku,name" in r.text
    with Session(engine) as s:
        assert s.exec(select(AuditLog).where(AuditLog.action == "catalog_export")).first() is not None
    seller = TestClient(main.app); _login(seller, "kim@t.local")
    assert seller.get("/catalog/export.csv").status_code == 403


def test_no_cost_or_margin_leak_to_seller(ctx):
    """A seller must never reach any catalog/pricing surface (buy-cost + margin live there)."""
    client, engine, ids = ctx
    _login(client, "kim@t.local")
    for p in ("/catalog", "/pricing", f"/catalog/products/{ids['product']}?tab=pricing"):
        r = client.get(p)
        assert r.status_code == 403
        assert "exw" not in r.text.lower() and "margin" not in r.text.lower()
