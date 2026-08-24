"""Phase 5 (B) — Catalog Studio: provider abstraction (mocked, no live external calls), generation lifecycle,
failure → work item (non-blocking), approved-fields-only (no contacts/margins), Higgs-not-configured,
generated-PDF authorization."""
import pytest
from sqlalchemy import text
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine, select

import app.main as main
from app import catalog_studio as STUDIO
from app.auth import hash_password
from app.models import CatalogGenerationJob, Product, ProductPriceVersion, User, WorkItem


class FakeOK:
    name = "builtin"

    def status(self):
        return "ready"

    def generate(self, html, out_path, timeout_ms=0):
        out_path.write_bytes(b"%PDF-1.4 fake catalog")
        return True, "", "fake-123"


class FakeFail:
    name = "builtin"

    def status(self):
        return "ready"

    def generate(self, html, out_path, timeout_ms=0):
        return False, "render boom", ""


@pytest.fixture
def ctx(monkeypatch, tmp_path):
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    SQLModel.metadata.create_all(engine)
    with engine.connect() as c:
        c.execute(text("CREATE UNIQUE INDEX IF NOT EXISTS uq_workitem_idem_open ON workitem(idempotency_key) "
                       "WHERE idempotency_key != '' AND status IN ('open','in_progress','waiting')"))
        c.commit()
    monkeypatch.setattr(main, "engine", engine)
    monkeypatch.setattr(main, "PRODUCT_FILES_DIR", tmp_path / "product_files")
    with Session(engine) as s:
        s.add(User(email="admin@t.local", name="A", role="admin", active=True, password_hash=hash_password("pw")))
        s.add(User(email="kim@t.local", name="K", role="agent", active=True, password_hash=hash_password("pw")))
        s.commit()
        ids = {u.email.split("@")[0]: u.id for u in s.exec(select(User)).all()}
        p = Product(name="Copper cathode", short_description="High-purity", origin_country="IR",
                    incoterms="FOB,CIF", certifications="ISO9001", min_order_qty=25, unit="tonne",
                    exw_price=8500, internal_notes="SECRET buy cost note")
        s.add(p); s.commit(); s.refresh(p)
        ids["product"] = p.id
    return TestClient_(main), engine, ids


def TestClient_(m):
    from fastapi.testclient import TestClient
    return TestClient(m.app)


def _login(c, email):
    assert c.post("/login", data={"email": email, "password": "pw"}, follow_redirects=False).status_code == 303


# --- approved-fields snapshot: no contacts / margins / internal notes ------------------------------
def test_approved_fields_excludes_internal_data(ctx):
    client, engine, ids = ctx
    with Session(engine) as s:
        p = s.get(Product, ids["product"])
        fields = STUDIO.approved_fields(p)
    blob = str(fields).lower()
    assert "go4it" in fields["contact"].lower()
    assert "secret" not in blob and "buy cost" not in blob      # internal notes never included
    assert "exw" not in blob and "margin" not in blob and "8500" not in blob  # no buy-cost/margin
    assert fields["name"] == "Copper cathode" and "ISO9001" in fields["certifications"]


def test_studio_admin_only(ctx):
    client, engine, ids = ctx
    _login(client, "kim@t.local")
    assert client.get("/catalog/studio").status_code == 403
    admin = TestClient_(main); _login(admin, "admin@t.local")
    assert admin.get("/catalog/studio").status_code == 200


def test_higgs_not_configured(ctx):
    st = STUDIO.provider_status()
    assert st["higgs"] == "not_configured"                      # honest — never claims it works
    ok, err, jid = STUDIO.HiggsProvider().generate("<html></html>", None)
    assert ok is False and "not configured" in err.lower()


def test_generation_lifecycle_and_authorized_download(ctx, monkeypatch):
    client, engine, ids = ctx
    monkeypatch.setattr(STUDIO, "get_provider", lambda name=None: FakeOK())
    _login(client, "admin@t.local")
    client.post("/catalog/studio/generate", data={"product_id": ids["product"], "provider": "builtin"},
                follow_redirects=False)
    with Session(engine) as s:
        j = s.exec(select(CatalogGenerationJob)).one()
        assert j.status == "needs_review" and j.file_path and j.provider_job_id == "fake-123"
        jid = j.id
    # admin can download; approve → approved
    assert client.get(f"/catalog/studio/{jid}/download").status_code == 200
    client.post(f"/catalog/studio/{jid}/action", data={"action": "approve"}, follow_redirects=False)
    with Session(engine) as s:
        assert s.get(CatalogGenerationJob, jid).status == "approved"
    # seller blocked until published seller-safe
    seller = TestClient_(main); _login(seller, "kim@t.local")
    assert seller.get(f"/catalog/studio/{jid}/download").status_code == 404
    client.post(f"/catalog/studio/{jid}/action", data={"action": "publish_seller_safe"}, follow_redirects=False)
    assert seller.get(f"/catalog/studio/{jid}/download").status_code == 200


def test_generation_failure_opens_workitem_nonblocking(ctx, monkeypatch):
    client, engine, ids = ctx
    monkeypatch.setattr(STUDIO, "get_provider", lambda name=None: FakeFail())
    _login(client, "admin@t.local")
    r = client.post("/catalog/studio/generate", data={"product_id": ids["product"], "provider": "builtin"},
                    follow_redirects=False)
    assert r.status_code == 303                                 # never blocks / raises
    with Session(engine) as s:
        assert s.exec(select(CatalogGenerationJob)).one().status == "failed"
        assert s.exec(select(WorkItem).where(WorkItem.type == "catalog_generation_failed")).first() is not None


def test_never_auto_emailed(ctx):
    """There is no email trigger anywhere in the studio routes (generation stores privately only)."""
    import inspect as _inspect
    src = _inspect.getsource(main.catalog_studio_generate) + _inspect.getsource(main.catalog_studio_action)
    assert "send_email" not in src and "send_via_account" not in src
