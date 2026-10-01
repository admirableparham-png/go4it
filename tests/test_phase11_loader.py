"""Phase 11 — scripts/load_managed_buyers.py: buyers of a seller's request land as CONFIDENTIAL managed buyers
(admin pool, anon_ref, StageEvent), excluded countries/regex are skipped, messy contact fields are parsed, one
transaction, idempotent, never touches the request or notifies, and the seller sees no buyer identity."""
import io
import json

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine, select

import app.main as main
from app import permissions as P
from app.auth import hash_password
from app.models import AuditLog, Lead, ServiceRequest, StageEvent, User, UserProfile
from scripts import load_managed_buyers as LMB

BUYERS = {"buyers": [
    {"company": "Richelieu Hardware Ltd", "dest_iso": "CA", "city": "Montreal", "email": "sales@richelieu.example",
     "website": "richelieu.example", "phones": ["+1 514 000"], "buys": ["anchors"], "match_score": 83},
    {"company": "Big Box USA", "dest_iso": "US", "email": "buy@bigbox.example"},
    {"company": "Ferreteria MX", "dest_iso": "MX", "email": "compras@ferre.example"},
    {"company": "Polska Hurt", "dest_iso": "PL", "email": "Tel +48 22 555 0101 / biuro@polska.example; www.x.pl"},
    {"company": "Polska Hurt Two", "dest_iso": "PL", "email": "biuro@polska.example"},        # same address
    {"company": "NoMail Srl", "dest_iso": "IT", "email": "https://nomail.example/contact"},
    {"company": "DIY Corner Shop", "dest_iso": "IE", "email": "hi@diy.example", "buys": ["retail DIY"]},
    {"company": "Richelieu Hardware Ltd", "dest_iso": "CA", "email": "other@richelieu.example"},  # duplicate buyer
    {"company": "", "dest_iso": "GB", "email": "x@y.example"},
]}


@pytest.fixture
def ctx(monkeypatch):
    e = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    SQLModel.metadata.create_all(e)
    with e.connect() as c:
        for ddl in ("CREATE UNIQUE INDEX IF NOT EXISTS uq_lead_req_anonref ON lead(request_id, anon_ref) "
                    "WHERE anon_ref != ''",
                    "CREATE UNIQUE INDEX IF NOT EXISTS uq_userprofile_user ON userprofile(user_id) WHERE user_id IS NOT NULL",
                    "CREATE UNIQUE INDEX IF NOT EXISTS uq_workitem_idem_open ON workitem(idempotency_key) "
                    "WHERE idempotency_key != '' AND status IN ('open','in_progress','waiting')"):
            c.execute(text(ddl))
        c.commit()
    monkeypatch.setattr(LMB, "engine", e)
    monkeypatch.setattr(LMB, "init_db", lambda: None)
    monkeypatch.setattr(main, "engine", e)
    with Session(e) as s:
        seller = User(email="sharks@t", name="sharks", role="agent", active=True, password_hash=hash_password("pw"))
        s.add(seller); s.commit(); s.refresh(seller)
        s.add(UserProfile(user_id=seller.id, account_class="seller", role_key="seller",
                          scope=P.ROLE_TEMPLATES["seller"]["scope"], account_status="active"))
        sr = ServiceRequest(tracking_code="SR-202608-0001", request_type="buyer_hunt", product="Anchors",
                            status="done", owner_id=seller.id, requester_id=seller.id)
        s.add(sr); s.commit(); s.refresh(sr)
        return e, sr.id, seller.id


def _run(monkeypatch, *args, data=BUYERS):
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(data)))
    return LMB.main(["--request", "SR-202608-0001", "--stdin", *args])


def test_loads_confidential_managed_buyers(ctx, monkeypatch, capsys):
    e, rid, seller = ctx
    assert _run(monkeypatch, "--exclude-countries", "US,MX", "--exclude-regex", r"\bretail\b") == 0
    out = capsys.readouterr().out
    assert "@" not in out.replace("SR-202608-0001", "")                  # counts only, never an address
    with Session(e) as s:
        leads = s.exec(select(Lead).order_by(Lead.id)).all()
        assert [ld.buyer_company for ld in leads] == ["Richelieu Hardware Ltd", "Polska Hurt", "Polska Hurt Two",
                                                      "NoMail Srl"]
        for ld in leads:
            assert ld.owner_id is None and ld.managed and ld.seller_id == seller and ld.request_id == rid
            assert ld.pipeline_stage == "identified" and ld.anon_ref.startswith(f"Buyer-{ld.dest_country}-")
            assert ld.tracking_code.startswith("G4-")
            ev = s.exec(select(StageEvent).where(StageEvent.lead_id == ld.id)).one()
            assert ev.to_stage == "identified" and not ev.inferred
        emails = {ld.buyer_company: ld.email for ld in leads}
        assert emails["Polska Hurt"] == "biuro@polska.example"             # parsed out of a messy field
        assert emails["Polska Hurt Two"] == "" and emails["NoMail Srl"] == ""
        assert "contact field" in s.exec(select(Lead).where(Lead.buyer_company == "Polska Hurt")).one().notes
        sr = s.get(ServiceRequest, rid)
        assert sr.status == "done" and not sr.leads_delivered                # request untouched
        audit = s.exec(select(AuditLog).where(AuditLog.action == "managed_load")).one()
        assert "@" not in audit.meta


def test_rerun_adds_nothing_and_dry_run_writes_nothing(ctx, monkeypatch):
    e, rid, _ = ctx
    assert _run(monkeypatch, "--dry-run") == 0
    with Session(e) as s:
        assert s.exec(select(Lead)).all() == []
    _run(monkeypatch, "--exclude-countries", "US")
    with Session(e) as s:
        n = len(s.exec(select(Lead)).all())
    _run(monkeypatch, "--exclude-countries", "US")
    with Session(e) as s:
        assert len(s.exec(select(Lead)).all()) == n


def test_require_email(ctx, monkeypatch):
    e, rid, _ = ctx
    _run(monkeypatch, "--exclude-countries", "US,MX", "--require-email")
    with Session(e) as s:
        assert all(ld.email for ld in s.exec(select(Lead)).all())


def test_a_failure_mid_load_leaves_nothing(ctx, monkeypatch):
    e, rid, _ = ctx
    calls = {"n": 0}
    real = LMB.pipeline.assign_anon_ref

    def flaky(session, lead, retries=8):
        calls["n"] += 1
        if calls["n"] == 3:
            raise RuntimeError("boom")
        return real(session, lead, retries)
    monkeypatch.setattr(LMB.pipeline, "assign_anon_ref", flaky)
    assert _run(monkeypatch) == 1
    with Session(e) as s:
        assert s.exec(select(Lead)).all() == [] and s.exec(select(StageEvent)).all() == []


def test_refuses_while_seller_owned_buyers_exist(ctx, monkeypatch, capsys):
    e, rid, seller = ctx
    with Session(e) as s:
        s.add(Lead(product="Anchors", owner_id=seller, source=f"req-{rid}", buyer_company="Legacy", email="l@x.example"))
        s.commit()
    assert _run(monkeypatch) == 2
    assert "backfill_confidential" in capsys.readouterr().out


def test_the_seller_never_sees_a_loaded_buyer(ctx, monkeypatch):
    e, rid, _ = ctx
    _run(monkeypatch, "--exclude-countries", "US,MX")
    c = TestClient(main.app)
    assert c.post("/login", data={"email": "sharks@t", "password": "pw"}, follow_redirects=False).status_code == 303
    for path in ("/requests", "/leads", "/dashboard"):
        r = c.get(path)
        assert "Richelieu" not in r.text and "richelieu.example" not in r.text and "Polska" not in r.text


def test_old_deliver_script_refuses(capsys):
    from scripts import deliver_request
    deliver_request.run(1, "whatever.json")
    assert "REFUSED" in capsys.readouterr().out


def test_only_countries_loads_a_single_country_wave(ctx, monkeypatch):
    e, rid, _ = ctx
    assert _run(monkeypatch, "--only-countries", "CA") == 0
    with Session(e) as s:
        assert {ld.dest_country for ld in s.exec(select(Lead)).all()} == {"CA"}
