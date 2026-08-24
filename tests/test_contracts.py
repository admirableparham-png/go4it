"""Phase 6 (B) — contracts: explicit party selection, workflow, immutable versions, allowlisted templates,
buyer/seller separation, signed-copy quarantine, honest e-sign, admin-only."""
import json

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine, select

import app.main as main
from app import contract_service as CS, esign as ESIGN
from app.auth import hash_password
from app.models import (Company, Contract, ContractDocument, ContractTemplate, ContractVersion, SignatureEvent,
                        User)


@pytest.fixture
def ctx(monkeypatch, tmp_path):
    e = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    SQLModel.metadata.create_all(e)
    with e.connect() as c:
        c.execute(text("CREATE UNIQUE INDEX IF NOT EXISTS uq_workitem_idem_open ON workitem(idempotency_key) WHERE idempotency_key != '' AND status IN ('open','in_progress','waiting')"))
        c.commit()
    monkeypatch.setattr(main, "engine", e)
    monkeypatch.setattr(main, "CONTRACT_FILES_DIR", tmp_path / "contract_files")
    with Session(e) as s:
        s.add(User(email="admin@t.local", name="A", role="admin", active=True, password_hash=hash_password("pw")))
        s.add(User(email="kim@t.local", name="K", role="agent", active=True, password_hash=hash_password("pw")))
        s.commit()
    return e


def _login(c, email):
    assert c.post("/login", data={"email": email, "password": "pw"}, follow_redirects=False).status_code == 303


# --- template allowlist + escape ------------------------------------------------------------------
def test_template_rejects_unknown_and_unresolved_vars():
    body = "Dear {{ buyer_name }}, total {{ total }}."
    txt, err = CS.render_template(body, ["buyer_name", "total"], {"buyer_name": "ACME", "total": "1000"})
    assert err == "" and "ACME" in txt and "{{" not in txt
    _, err2 = CS.render_template(body, ["buyer_name"], {"buyer_name": "x"})   # 'total' not allowlisted
    assert "unknown" in err2
    _, err3 = CS.render_template(body, ["buyer_name", "total"], {"buyer_name": "x"})  # missing value
    assert "unresolved" in err3


def test_template_escapes_values():
    txt, err = CS.render_template("Hi {{ n }}", ["n"], {"n": "<script>x</script>"})
    assert err == "" and "<script>" not in txt and "&lt;script&gt;" in txt


# --- explicit party + workflow + immutability -----------------------------------------------------
def test_create_requires_explicit_type_side(ctx):
    with Session(ctx) as s:
        co = Company(name="ACME", primary_role="buyer"); s.add(co); s.commit(); s.refresh(co)
        c, v = CS.create_contract(s, contract_type="buyer_sales", side="buyer", company_id=co.id); s.commit()
        assert c.contract_type == "buyer_sales" and c.side == "buyer" and c.current_version_id == v.id
        assert v.version == 1 and v.status == "draft"


def test_workflow_transitions_and_immutable_signed(ctx):
    with Session(ctx) as s:
        c, v = CS.create_contract(s, contract_type="nda", side="buyer"); s.commit()
        assert CS.transition(s, c, "signed")[0] is False        # draft → signed rejected
        assert CS.transition(s, c, "approved")[0] is True
        assert CS.transition(s, c, "signed")[0] is True
        s.commit()
        # amend = a NEW linked version, never editing the signed one
        v2 = CS.revise_contract(s, c, is_amendment=True); s.commit()
        assert v2.version == 2 and v2.is_amendment and v2.supersedes_id == v.id
        assert s.get(ContractVersion, v.id).status == "signed"  # original signed version untouched


def test_buyer_and_supplier_contracts_are_separate(ctx):
    with Session(ctx) as s:
        buyer_co = Company(name="Buyer Co", primary_role="buyer"); s.add(buyer_co)
        sup_co = Company(name="Supplier Co", primary_role="supplier"); s.add(sup_co); s.commit()
        s.refresh(buyer_co); s.refresh(sup_co)
        bc, _ = CS.create_contract(s, contract_type="buyer_sales", side="buyer", company_id=buyer_co.id)
        sc, _ = CS.create_contract(s, contract_type="supplier_purchase", side="supplier", company_id=sup_co.id)
        s.commit()
        # they are DISTINCT documents with different sides + counterparties (no "both parties" doc)
        assert bc.id != sc.id and bc.side != sc.side and bc.company_id != sc.company_id


# --- e-signature honesty --------------------------------------------------------------------------
def test_esign_not_configured_and_manual_not_verified(ctx):
    assert ESIGN.provider_status()["state"] == "not_configured"
    with Session(ctx) as s:
        c, v = CS.create_contract(s, contract_type="service", side="internal"); s.commit()
        ev = ESIGN.record_manual_signature(s, c, v, party_role="buyer", signer_name="Jane Doe",
                                           signer_email="j@x.com"); s.commit()
        assert ev.method == "manual" and ev.verified is False    # a typed name is NOT a verified signature


# --- signed-copy quarantine + admin-only ----------------------------------------------------------
def test_signed_upload_quarantined_admin_only(ctx):
    client = TestClient(main.app); _login(client, "admin@t.local")
    with Session(ctx) as s:
        c, _ = CS.create_contract(s, contract_type="buyer_sales", side="buyer"); s.commit()
        cid = c.id
    client.post(f"/contracts/{cid}/documents", files={"file": ("signed.pdf", b"%PDF-1.4 signed", "application/pdf")},
                follow_redirects=False)
    with Session(ctx) as s:
        doc = s.exec(select(ContractDocument)).one()
        assert doc.kind == "signed_upload" and doc.quarantine == "quarantined" and doc.sha256
    seller = TestClient(main.app); _login(seller, "kim@t.local")
    assert seller.get(f"/contracts/{cid}").status_code == 403                    # admin-only workspace
    with Session(ctx) as s:
        did = s.exec(select(ContractDocument.id)).one()
    assert seller.get(f"/contracts/{cid}/documents/{did}/download").status_code == 404  # admin-only download


def test_contracts_admin_only(ctx):
    seller = TestClient(main.app); _login(seller, "kim@t.local")
    for p in ("/contracts", "/contract-templates", "/commercial/analytics"):
        assert seller.get(p).status_code == 403
