"""Phase 5 (B) — private product documents: admin-only upload, validated, admin-download-gated, seller access
only when published seller-safe, path-traversal-safe, archive-not-delete."""
import pytest
from fastapi.testclient import TestClient
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine, select

import app.main as main
from app.auth import hash_password
from app.models import Product, ProductDocument, User


@pytest.fixture
def ctx(monkeypatch, tmp_path):
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    SQLModel.metadata.create_all(engine)
    monkeypatch.setattr(main, "engine", engine)
    monkeypatch.setattr(main, "PRODUCT_FILES_DIR", tmp_path / "product_files")
    with Session(engine) as s:
        s.add(User(email="admin@t.local", name="A", role="admin", active=True, password_hash=hash_password("pw")))
        s.add(User(email="kim@t.local", name="K", role="agent", active=True, password_hash=hash_password("pw")))
        s.commit()
        ids = {u.email.split("@")[0]: u.id for u in s.exec(select(User)).all()}
        p = Product(name="Copper"); s.add(p); s.commit(); s.refresh(p)
        ids["product"] = p.id
    return TestClient(main.app), engine, ids


def _login(c, email):
    assert c.post("/login", data={"email": email, "password": "pw"}, follow_redirects=False).status_code == 303


def _upload(client, pid, name="datasheet.pdf", ct="application/pdf", body=b"%PDF-1.4 test"):
    return client.post(f"/catalog/products/{pid}/documents",
                       data={"doc_type": "datasheet", "title": "DS"},
                       files={"file": (name, body, ct)}, follow_redirects=False)


def test_upload_admin_only(ctx):
    client, engine, ids = ctx
    _login(client, "kim@t.local")
    assert _upload(client, ids["product"]).status_code == 403


def test_valid_upload_and_admin_download(ctx):
    client, engine, ids = ctx
    _login(client, "admin@t.local")
    assert _upload(client, ids["product"]).status_code == 303
    with Session(engine) as s:
        d = s.exec(select(ProductDocument)).one()
        assert d.status == "active" and d.original_filename == "datasheet.pdf" and d.file_path
        did = d.id
    r = client.get(f"/catalog/products/{ids['product']}/documents/{did}/download")
    assert r.status_code == 200 and r.content.startswith(b"%PDF")


def test_dangerous_and_mismatch_rejected(ctx):
    client, engine, ids = ctx
    _login(client, "admin@t.local")
    _upload(client, ids["product"], name="payload.exe", ct="application/octet-stream", body=b"MZ")
    _upload(client, ids["product"], name="notreally.png", ct="application/pdf", body=b"x")  # ext/MIME mismatch
    with Session(engine) as s:
        assert s.exec(select(ProductDocument)).all() == []       # neither stored


def test_seller_cannot_download_unless_seller_safe(ctx):
    client, engine, ids = ctx
    _login(client, "admin@t.local")
    _upload(client, ids["product"])
    with Session(engine) as s:
        did = s.exec(select(ProductDocument.id)).one()
    seller = TestClient(main.app); _login(seller, "kim@t.local")
    assert seller.get(f"/catalog/products/{ids['product']}/documents/{did}/download").status_code == 404
    # publish seller-safe → now allowed
    with Session(engine) as s:
        d = s.get(ProductDocument, did); d.seller_safe = True; s.add(d); s.commit()
    assert seller.get(f"/catalog/products/{ids['product']}/documents/{did}/download").status_code == 200


def test_archive_not_delete(ctx):
    client, engine, ids = ctx
    _login(client, "admin@t.local")
    _upload(client, ids["product"])
    with Session(engine) as s:
        did = s.exec(select(ProductDocument.id)).one()
    client.post(f"/catalog/products/{ids['product']}/documents/{did}/archive", follow_redirects=False)
    with Session(engine) as s:
        d = s.get(ProductDocument, did)
        assert d is not None and d.status == "archived"          # archived, row preserved
    # an archived doc is no longer downloadable
    assert client.get(f"/catalog/products/{ids['product']}/documents/{did}/download").status_code == 404
