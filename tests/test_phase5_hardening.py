"""Phase 5 pre-Phase-6 hardening.

Seller-safe access is tenant/owner-scoped (seller_safe alone never grants access; Seller A can't read Seller
B's files); product/catalog downloads are admin-only; uploads default private + quarantined; publication is
limited to Go4it PDFs + validated raster images; the PDF generator is sandboxed (JS off, network blocked, all
fields escaped); and the Products/Suppliers view is only a projection over the canonical Trade Network company.
"""
import inspect as _inspect

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine, select

import app.main as main
from app import catalog_studio as STUDIO
from app.auth import hash_password
from app.models import (Company, Product, ProductDocument, RequestDeliverable, ServiceRequest, User)


@pytest.fixture
def ctx(monkeypatch, tmp_path):
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    SQLModel.metadata.create_all(engine)
    monkeypatch.setattr(main, "engine", engine)
    monkeypatch.setattr(main, "PRODUCT_FILES_DIR", tmp_path / "product_files")
    monkeypatch.setattr(main, "REQUEST_FILES_DIR", tmp_path / "request_files")
    with Session(engine) as s:
        for email, role in [("admin@t.local", "admin"), ("a@t.local", "agent"), ("b@t.local", "agent")]:
            s.add(User(email=email, name=email[0], role=role, active=True, password_hash=hash_password("pw")))
        s.commit()
        ids = {u.email.split("@")[0]: u.id for u in s.exec(select(User)).all()}
        p = Product(name="Copper"); s.add(p); s.commit(); s.refresh(p)
        ids["product"] = p.id
        # a request OWNED by seller A
        sr = ServiceRequest(request_type="buyer_hunt", status="submitted", owner_id=ids["a"],
                            requester_id=ids["a"])
        s.add(sr); s.commit(); s.refresh(sr)
        ids["req_a"] = sr.id
    return TestClient(main.app), engine, ids


def _login(c, email):
    assert c.post("/login", data={"email": email, "password": "pw"}, follow_redirects=False).status_code == 303


def _upload(client, pid, name="photo.png", ct="image/png", body=b"\x89PNG\r\n", doc_type="product_image"):
    return client.post(f"/catalog/products/{pid}/documents", data={"doc_type": doc_type, "title": "x"},
                       files={"file": (name, body, ct)}, follow_redirects=False)


# --- default private + quarantined -----------------------------------------------------------------
def test_upload_defaults_private_and_quarantined(ctx):
    client, engine, ids = ctx
    _login(client, "admin@t.local")
    _upload(client, ids["product"])
    with Session(engine) as s:
        d = s.exec(select(ProductDocument)).one()
        assert d.seller_safe is False and d.quarantine == "quarantined"


# --- downloads are admin-only (seller_safe alone never grants access) ------------------------------
def test_product_download_admin_only_even_if_seller_safe(ctx):
    client, engine, ids = ctx
    _login(client, "admin@t.local")
    _upload(client, ids["product"])
    with Session(engine) as s:
        d = s.exec(select(ProductDocument)).one(); d.seller_safe = True; s.add(d); s.commit()  # force the flag
        did = d.id
    seller = TestClient(main.app); _login(seller, "a@t.local")
    # seller_safe=True but the DIRECT product route is admin-only → 404 for any seller
    assert seller.get(f"/catalog/products/{ids['product']}/documents/{did}/download").status_code == 404


def test_publish_routes_admin_only(ctx):
    client, engine, ids = ctx
    _login(client, "a@t.local")
    assert client.post(f"/catalog/products/{ids['product']}/documents/1/publish",
                       data={"req_id": ids["req_a"]}).status_code == 403


# --- Seller A can access a published file; Seller B cannot (tenant/owner-scoped) -------------------
def test_seller_a_can_access_seller_b_cannot(ctx):
    client, engine, ids = ctx
    _login(client, "admin@t.local")
    _upload(client, ids["product"])                        # a validated raster image → publishable
    with Session(engine) as s:
        did = s.exec(select(ProductDocument.id)).one()
    # publish to Seller A's request → a seller-safe RequestDeliverable
    client.post(f"/catalog/products/{ids['product']}/documents/{did}/publish",
                data={"req_id": ids["req_a"]}, follow_redirects=False)
    with Session(engine) as s:
        dv = s.exec(select(RequestDeliverable)).one()
        assert dv.seller_safe is True and dv.request_id == ids["req_a"]
        dv_id = dv.id
    a = TestClient(main.app); _login(a, "a@t.local")
    b = TestClient(main.app); _login(b, "b@t.local")
    # Seller A owns the request → can download; Seller B does NOT → 404
    assert a.get(f"/requests/{ids['req_a']}/deliverable/{dv_id}").status_code == 200
    assert b.get(f"/requests/{ids['req_a']}/deliverable/{dv_id}").status_code == 404


# --- quarantine: only Go4it PDFs + validated raster images are publishable -------------------------
def test_non_image_document_not_publishable(ctx):
    client, engine, ids = ctx
    _login(client, "admin@t.local")
    _upload(client, ids["product"], name="ds.pdf", ct="application/pdf", body=b"%PDF-1.4",
            doc_type="datasheet")                          # a supplier PDF → quarantined, NOT publishable
    with Session(engine) as s:
        did = s.exec(select(ProductDocument.id)).one()
    client.post(f"/catalog/products/{ids['product']}/documents/{did}/publish",
                data={"req_id": ids["req_a"]}, follow_redirects=False)
    with Session(engine) as s:
        assert s.exec(select(RequestDeliverable)).all() == []   # refused — nothing published
        assert s.get(ProductDocument, did).seller_safe is False


def test_scan_clear_allows_publish(ctx):
    client, engine, ids = ctx
    _login(client, "admin@t.local")
    _upload(client, ids["product"], name="ds.pdf", ct="application/pdf", body=b"%PDF-1.4",
            doc_type="datasheet")
    with Session(engine) as s:
        did = s.exec(select(ProductDocument.id)).one()
    client.post(f"/catalog/products/{ids['product']}/documents/{did}/scan-clear", follow_redirects=False)
    client.post(f"/catalog/products/{ids['product']}/documents/{did}/publish",
                data={"req_id": ids["req_a"]}, follow_redirects=False)
    with Session(engine) as s:
        assert s.exec(select(RequestDeliverable)).first() is not None   # scanned → now publishable


# --- PDF sandbox ------------------------------------------------------------------------------------
def test_pdf_fields_escaped():
    html = STUDIO.render_html({"name": "<script>alert(1)</script>X", "brand": "<img onerror=y>",
                               "description": "<iframe src=file:///etc/passwd>", "specifications": [],
                               "origin": "IR", "contact": "Go4it"})
    assert "<script>" not in html and "<iframe" not in html and "<img onerror" not in html
    assert "&lt;script&gt;" in html                        # escaped to inert text


def test_pdf_generator_is_sandboxed():
    src = _inspect.getsource(STUDIO.BuiltinProvider.generate)
    assert "java_script_enabled=False" in src              # JS disabled
    assert "route.abort()" in src and 'context.route("**/*"' in src   # network blocked
    assert "offline=True" in src


# --- supplier nav: projection, canonical Trade Network intact --------------------------------------
def test_suppliers_is_projection_canonical_intact(ctx):
    client, engine, ids = ctx
    _login(client, "admin@t.local")
    with Session(engine) as s:
        co = Company(name="Nat Copper", primary_role="supplier", country="IR"); s.add(co); s.commit()
        s.refresh(co)
        from app.models import Supplier
        sup = Supplier(name="Nat Copper", company_id=co.id); s.add(sup); s.commit()
        cid = co.id
    body = client.get("/suppliers").text
    assert "projection" in body.lower() and "/companies" in body     # clearly a projection + links canonical
    # canonical company management remains reachable in the Trade Network
    assert client.get(f"/companies/{cid}").status_code == 200
