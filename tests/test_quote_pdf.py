"""Phase 6 (A) — quote PDF: buyer-safe (no cost/margin/seller), immutable + hashed, no-unresolved-vars,
admin-only download, buyer-via-token, and the deferred attachment path stays isolated (global lock intact)."""
import pytest
from sqlalchemy import text
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine, select

import app.main as main
from app import pdf_render as PDF, quote_pdf as QPDF, quote_service as QS, quote_workflow as QW
from app import quote_portal as QP
from app.auth import hash_password
from app.models import Lead, Product, Quote, QuoteDocument, QuoteVersion, User


def _fake_render(html, out_path, timeout_ms=20000):
    out_path.write_bytes(b"%PDF-1.4 fake quote pdf")
    return True, ""


@pytest.fixture
def ctx(monkeypatch, tmp_path):
    e = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    SQLModel.metadata.create_all(e)
    with e.connect() as c:
        c.execute(text("CREATE UNIQUE INDEX IF NOT EXISTS uq_quoteaccesstoken_hash ON quoteaccesstoken(token_hash) WHERE token_hash != ''"))
        c.commit()
    monkeypatch.setattr(main, "engine", e)
    monkeypatch.setattr(main, "QUOTE_FILES_DIR", tmp_path / "quote_files")
    monkeypatch.setattr(PDF, "render_pdf", _fake_render)         # no real chromium in the suite
    with Session(e) as s:
        s.add(User(email="admin@t.local", name="A", role="admin", active=True, password_hash=hash_password("pw")))
        s.add(User(email="kim@t.local", name="K", role="agent", active=True, password_hash=hash_password("pw")))
        s.commit()
        ld = Lead(product="x", tracking_code="G4-1", dest_country="GE", buyer_company="ACME LLC")
        s.add(ld); s.commit(); s.refresh(ld)
        p = Product(name="Steel rebar", exw_price=590, weight_kg_per_unit=1000, min_order_qty=100,
                    currency="USD", unit="tonne", origin_country="IR", short_description="grade B500")
        s.add(p); s.commit(); s.refresh(p)
        q = QS.create_quote(s, ld, p); s.commit()
        ver = QS.ensure_version(s, q); s.commit()
        QW.transition(s, q, "approved"); QW.transition(s, q, "sent"); s.commit()
        raw, _ = QP.mint_token(s, q, ver, valid_days=14); s.commit()
        ids = {"quote": q.id, "version": ver.id, "token": raw}
    return e, ids


def _login(c, email):
    assert c.post("/login", data={"email": email, "password": "pw"}, follow_redirects=False).status_code == 303


def test_rendered_html_has_no_cost_margin_or_seller():
    fields = {"ref": "G4-1-Q1", "version": 1, "issued": "2026-01-01", "expires": "", "buyer": "ACME",
              "product": "Steel", "spec": "B500", "hs_code": "7213", "quantity": 100, "unit": "tonne",
              "unit_price": 150.0, "total": 15000.0, "currency": "USD", "incoterm": "DAP", "origin": "IR",
              "destination": "GE", "packaging": "bundled", "lead_time": 30, "payment_terms": "30% advance",
              "commercial_text": "", "options": [], "link": "https://x/q/tok"}
    import re
    raw = QPDF.render_quote_html(fields)
    html = re.sub(r"<style>.*?</style>", "", raw, flags=re.S).lower()   # CSS legitimately uses 'margin:'
    for banned in ("exw", "margin", "cost component", "factory gate", "supplier", "markup"):
        assert banned not in html, banned
    assert "15000.00" in html and "dap" in html               # delivered price + incoterm shown


def test_no_unresolved_vars_guard():
    assert PDF.has_unresolved_vars("hello {{ name }}") is True
    assert PDF.has_unresolved_vars("hello world") is False


def test_generate_stores_immutable_hashed_pdf(ctx):
    engine, ids = ctx
    client = main and __import__("fastapi.testclient", fromlist=["TestClient"]).TestClient(main.app)
    _login(client, "admin@t.local")
    client.post(f"/quotes/{ids['quote']}/generate-pdf", follow_redirects=False)
    with Session(engine) as s:
        doc = s.exec(select(QuoteDocument)).one()
        assert doc.sha256 and len(doc.sha256) == 64 and doc.size_bytes > 0
        ver = s.get(QuoteVersion, ids["version"])
        assert ver.pdf_document_id == doc.id                  # version references its immutable PDF


def test_admin_download_and_buyer_via_token(ctx):
    engine, ids = ctx
    client = __import__("fastapi.testclient", fromlist=["TestClient"]).TestClient(main.app)
    _login(client, "admin@t.local")
    client.post(f"/quotes/{ids['quote']}/generate-pdf", follow_redirects=False)
    assert client.get(f"/quotes/{ids['quote']}/pdf").status_code == 200          # admin
    # seller (non-owner) cannot admin-download
    seller = __import__("fastapi.testclient", fromlist=["TestClient"]).TestClient(main.app)
    _login(seller, "kim@t.local")
    assert seller.get(f"/quotes/{ids['quote']}/pdf").status_code == 404
    # buyer downloads through the token (public)
    buyer = __import__("fastapi.testclient", fromlist=["TestClient"]).TestClient(main.app)
    r = buyer.get(f"/q/{ids['token']}/pdf")
    assert r.status_code == 200 and r.content.startswith(b"%PDF")


def test_verify_pdf_seam(ctx, tmp_path):
    p = tmp_path / "x.pdf"; p.write_bytes(b"%PDF-1.4 hello")
    good = PDF.sha256_file(p)
    assert PDF.verify_pdf(str(p), good) == (True, "")
    assert PDF.verify_pdf(str(p), "deadbeef")[0] is False      # sha mismatch
    bad = tmp_path / "y.txt"; bad.write_bytes(b"not a pdf")
    assert PDF.verify_pdf(str(bad), PDF.sha256_file(bad))[0] is False   # bad signature


def test_global_attachment_lock_still_effective():
    from app import attachments as ATT
    assert ATT.attachments_enabled() is False                 # Phase-4 lock untouched by the quote PDF path
