"""Phase 2 — Trade Network & Data Quality.

Additive canonical Company/Contact layer over existing records. ADMIN-ONLY: sellers must never reach any
Trade Network page, export, or company PII, and dedup/merge must never cross tenants. Classifications are
evidence-only (no reply => prospect; human reply => engaged incl. negative; auto-reply != engaged). Sources
map to readable labels with an honest 'unknown' fallback. Merges preserve everything and are reversible.
"""
import datetime

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine, select

import app.main as main
from app import company_service as CS
from app.auth import hash_password
from app.models import (Company, CompanyRole, Contact, Deal, DuplicateCandidate, Lead, Outreach,
                        Provenance, ServiceRequest, User)

STRONG_PII = ["buyer@acme.com", "+9715551234", "Zoltan", "acme.com"]


def _indexes(engine):
    with engine.connect() as conn:
        for ddl in ("CREATE UNIQUE INDEX IF NOT EXISTS uq_companyrole_company_role ON companyrole(company_id, role)",
                    "CREATE UNIQUE INDEX IF NOT EXISTS uq_dupcand_pair ON duplicatecandidate(left_id, right_id)",
                    "CREATE UNIQUE INDEX IF NOT EXISTS uq_provenance_dedup ON provenance"
                    "(entity_type, entity_id, source_type, source_ref) WHERE source_ref != ''"):
            conn.execute(text(ddl))
        conn.commit()


@pytest.fixture
def ctx(monkeypatch):
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    SQLModel.metadata.create_all(engine)
    _indexes(engine)
    monkeypatch.setattr(main, "engine", engine)
    ids = {}
    with Session(engine) as s:
        for email, role in [("admin@t.local", "admin"), ("kim@t.local", "agent"), ("c@t.local", "agent")]:
            s.add(User(email=email, name=email.split("@")[0], role=role, active=True,
                       password_hash=hash_password("pw")))
        s.commit()
        uid = {u.email: u.id for u in s.exec(select(User)).all()}
        ids.update(admin=uid["admin@t.local"], kim=uid["kim@t.local"], c=uid["c@t.local"])
    return TestClient(main.app), engine, ids


def _login(client, email):
    assert client.post("/login", data={"email": email, "password": "pw"},
                       follow_redirects=False).status_code == 303


def _lead(s, **kw):
    from app.lead_service import create_lead
    l = create_lead(s, Lead(product=kw.pop("product", "anchor"), **kw), run=False)
    s.commit()
    return l


# ------------------------------------------------------------- confidentiality / isolation
def test_new_routes_are_admin_only(ctx):
    client, engine, ids = ctx
    with Session(engine) as s:
        l = _lead(s, buyer_company="Acme", email="buyer@acme.com", phone="+9715551234",
                  contact_name="Zoltan", website="acme.com", source="manual", owner_id=ids["admin"])
        cid = l.company_id
    _login(client, "kim@t.local")
    for path in ["/sellers", "/data-quality", "/duplicates", f"/companies/{cid}",
                 "/export/buyers.csv", "/export/sellers.csv", "/export/suppliers.csv"]:
        assert client.get(path, follow_redirects=False).status_code == 403, path
    for path in [("/sellers", {"name": "X"}), (f"/companies/{cid}/verify", {}),
                 (f"/companies/{cid}/unmerge", {}), ("/duplicates/rescan", {})]:
        assert client.post(path[0], data=path[1], follow_redirects=False).status_code == 403, path[0]


def test_admin_sees_company_pii(ctx):
    client, engine, ids = ctx
    with Session(engine) as s:
        l = _lead(s, buyer_company="Acme", email="buyer@acme.com", phone="+9715551234",
                  contact_name="Zoltan", website="acme.com", source="manual", owner_id=ids["admin"])
        cid = l.company_id
    _login(client, "admin@t.local")
    body = client.get(f"/companies/{cid}").text
    assert "buyer@acme.com" in body and "Acme" in body


def test_seller_leads_surface_has_no_company_pii(ctx):
    client, engine, ids = ctx
    with Session(engine) as s:                 # a managed buyer FOR kim
        l = Lead(product="anchor", buyer_company="Acme", email="buyer@acme.com", phone="+9715551234",
                 contact_name="Zoltan", website="acme.com", source="req-1", owner_id=None, managed=True,
                 seller_id=ids["kim"], request_id=1, anon_ref="Buyer-XX-001", dest_country="IQ")
        s.add(l); s.commit(); s.refresh(l)
        CS.link_lead_company(s, l); s.commit()
    _login(client, "kim@t.local")
    for path in ["/leads", "/"]:
        body = client.get(path).text
        for leak in STRONG_PII:
            assert leak not in body, f"{leak} leaked at {path}"


def test_managed_buyer_company_scoped_to_seller_not_admin_pool(ctx):
    client, engine, ids = ctx
    with Session(engine) as s:
        l = Lead(product="anchor", buyer_company="Acme", email="buyer@acme.com", source="req-1",
                 owner_id=None, managed=True, seller_id=ids["kim"], request_id=1, anon_ref="B1")
        s.add(l); s.commit(); s.refresh(l)
        co = CS.link_lead_company(s, l); s.commit()
        assert co.tenant_id == ids["kim"]      # scoped to the seller it is FOR, NOT the NULL owner_id


def test_dedup_and_merge_never_cross_tenant(ctx):
    _, engine, ids = ctx
    with Session(engine) as s:
        a = Lead(product="x", buyer_company="Acme Ltd", email="buyer@acme.com", source="req-1",
                 owner_id=None, managed=True, seller_id=ids["kim"], request_id=1, anon_ref="A")
        b = Lead(product="x", buyer_company="Acme Ltd", email="buyer@acme.com", source="req-2",
                 owner_id=None, managed=True, seller_id=ids["c"], request_id=2, anon_ref="B")
        s.add(a); s.add(b); s.commit(); s.refresh(a); s.refresh(b)
        ca = CS.link_lead_company(s, a); cb = CS.link_lead_company(s, b); s.commit()
        assert ca.id != cb.id and ca.tenant_id != cb.tenant_id      # same email, different seller -> separate
        CS.scan_duplicates(s); s.commit()
        for dc in s.exec(select(DuplicateCandidate)).all():
            la, lb = s.get(Company, dc.left_id), s.get(Company, dc.right_id)
            assert la.tenant_id == lb.tenant_id                     # never a cross-tenant candidate
        admin = s.get(User, ids["admin"])
        ok, err = CS.merge_companies(s, ca.id, cb.id, admin)
        assert not ok and "cross-tenant" in err                     # merge refuses cross-tenant


# ------------------------------------------------------------- classification (evidence-only)
def test_engagement_classification(ctx):
    _, engine, ids = ctx
    with Session(engine) as s:
        now = datetime.datetime.utcnow()
        prospect = _lead(s, buyer_company="P Co", source="manual", owner_id=ids["admin"])
        assert prospect.engagement_class == "prospect"
        contacted = _lead(s, buyer_company="Ct Co", source="manual", owner_id=ids["admin"],
                          first_response_at=now)
        assert contacted.engagement_class == "contacted"
        engaged = _lead(s, buyer_company="E Co", source="manual", owner_id=ids["admin"],
                        buyer_replied_at=now, status="lost", lost_reason="price too high")
        assert engaged.engagement_class == "engaged" and engaged.reply_outcome == "negative"  # negative still engaged
        won = _lead(s, buyer_company="W Co", source="manual", owner_id=ids["admin"], status="won")
        s.add(Deal(lead_id=won.id, tracking_code="D1", owner_id=ids["admin"])); s.commit()
        cls, out = CS.classify_engagement(s, won)
        assert cls == "customer"
        # auto-reply must NOT become engaged
        auto = _lead(s, buyer_company="Auto Co", source="manual", owner_id=ids["admin"])
        s.add(Outreach(lead_id=auto.id, direction="out", status="sent"))
        s.add(Outreach(lead_id=auto.id, direction="in", subject="Automatic reply: Out of office"))
        s.commit()
        cls2, out2 = CS.classify_engagement(s, auto)
        assert cls2 == "contacted" and out2 == "auto_reply"


def test_admin_can_override_classification(ctx):
    client, engine, ids = ctx
    with Session(engine) as s:
        l = _lead(s, buyer_company="Ovr Co", source="manual", owner_id=ids["admin"])
        lid = l.id
    _login(client, "admin@t.local")
    assert client.post(f"/leads/{lid}/classify", data={"engagement_class": "qualified", "reply_outcome": "positive"},
                       follow_redirects=False).status_code == 303
    with Session(engine) as s:
        l = s.get(Lead, lid)
        assert l.engagement_class == "qualified" and l.reply_outcome == "positive"
        # reply history (Outreach) is untouched by a classification override
    # a seller cannot override
    _login(client, "kim@t.local")
    assert client.post(f"/leads/{lid}/classify", data={"engagement_class": "prospect"},
                       follow_redirects=False).status_code == 403


def test_engaged_saved_view_finds_negative_repliers(ctx):
    client, engine, ids = ctx
    with Session(engine) as s:
        _lead(s, buyer_company="NegReplier", source="manual", owner_id=ids["admin"],
              buyer_replied_at=datetime.datetime.utcnow(), status="lost", lost_reason="price")
    _login(client, "admin@t.local")
    body = client.get("/leads?view=engaged").text
    assert "NegReplier" in body                 # a rejected-but-replied buyer is still findable as engaged


# ------------------------------------------------------------- source + verification
def test_map_source_readable_and_unknown_fallback():
    assert CS.map_source("req-7", "")[0] == "seller_request"
    assert CS.map_source("osm-ge", "")[0] == "directory"
    assert CS.map_source("go4world_browser", "")[0] == "marketplace"
    st, name, _ = CS.map_source("uae-decor-buyer", "")   # undeterminable -> unknown, raw slug kept
    assert st == "unknown" and name == "uae-decor-buyer"


def test_provenance_idempotent(ctx):
    _, engine, ids = ctx
    with Session(engine) as s:
        l = _lead(s, buyer_company="Prov Co", email="a@provco.com", source="req-1", external_id="r1:x",
                  owner_id=ids["admin"])
        cid = l.company_id
        before = s.exec(select(Provenance).where(Provenance.entity_id == cid)).all()
        CS.add_provenance(s, "company", cid, None, "seller_request", "Concierge request 1", source_ref="r1:x")
        s.commit()
        after = s.exec(select(Provenance).where(Provenance.entity_id == cid)).all()
        assert len(after) == len(before)        # re-seen provenance is a no-op (last_seen bump), not a dup


def test_verification_writes_audit_not_a_table(ctx):
    client, engine, ids = ctx
    with Session(engine) as s:
        l = _lead(s, buyer_company="Ver Co", email="a@verco.com", source="manual", owner_id=ids["admin"])
        cid = l.company_id
    _login(client, "admin@t.local")
    client.post(f"/companies/{cid}/verify", data={"verification_status": "verified",
                "verification_method": "website", "verification_confidence": "80"}, follow_redirects=False)
    with Session(engine) as s:
        co = s.get(Company, cid)
        assert co.verification_status == "verified" and co.verification_confidence == 80
        from app.models import AuditLog
        assert s.exec(select(AuditLog).where(AuditLog.action == "verification_change")).first() is not None


def test_response_stats_labelled_denominator(ctx):
    _, engine, ids = ctx
    from app import tradenet as TN
    with Session(engine) as s:
        a = _lead(s, buyer_company="A", source="manual", owner_id=ids["admin"],
                  buyer_replied_at=datetime.datetime.utcnow(), reply_outcome="positive")
        a.reply_outcome = "positive"; s.add(a)
        s.add(Outreach(lead_id=a.id, direction="out", status="sent"))
        s.commit()
        st = TN.response_stats(s, [a])
        assert st["delivered"] == 1 and st["denominator_label"] == "delivered outreach"
        assert st["human_replies"] == 1 and st["response_rate"] == 100


# ------------------------------------------------------------- deduplication
def test_dedup_signals(ctx):
    _, engine, ids = ctx
    with Session(engine) as s:
        _lead(s, buyer_company="Acme Fixings Ltd", email="sales@acmefix.com", source="manual", owner_id=ids["admin"])
        _lead(s, buyer_company="Acme Fixings", email="info@acmefix.com", source="manual", owner_id=ids["admin"])
        _lead(s, buyer_company="Gmail A", email="a@gmail.com", source="manual", owner_id=ids["admin"])
        _lead(s, buyer_company="Gmail B", email="b@gmail.com", source="manual", owner_id=ids["admin"])
        CS.scan_duplicates(s); s.commit()
        # the two Acme leads share a domain -> ONE company (auto-linked), so no candidate between them
        acme = s.exec(select(Company).where(Company.name_normalized == "acme fixings")).all()
        assert len(acme) == 1
        # gmail companies are separate and NOT flagged on domain (generic excluded)
        cands = s.exec(select(DuplicateCandidate)).all()
        for dc in cands:
            sigs = dc.signals
            assert "gmail" not in (s.get(Company, dc.left_id).domain or "")
        # a shared phone across different names -> a strong candidate (never auto-merged)
        _lead(s, buyer_company="Phone One", phone="+1 415 000 1111", source="manual", owner_id=ids["admin"])
        _lead(s, buyer_company="Phone Two", phone="+1 415 000 1111", source="manual", owner_id=ids["admin"])
        n = CS.scan_duplicates(s); s.commit()
        strong = s.exec(select(DuplicateCandidate).where(DuplicateCandidate.match_type == "strong")).all()
        assert any("phone_exact" in dc.signals for dc in strong)


def test_merge_preserves_and_is_reversible(ctx):
    _, engine, ids = ctx
    with Session(engine) as s:
        admin = s.get(User, ids["admin"])
        a = _lead(s, buyer_company="Keep Co", email="a@keep.com", source="manual", owner_id=ids["admin"])
        b = _lead(s, buyer_company="Dup Co", phone="+1 222 333 4444", source="manual", owner_id=ids["admin"])
        can, dup = a.company_id, b.company_id
        ok, _ = CS.merge_companies(s, can, dup, admin); s.commit()
        assert ok
        s.refresh(b)
        assert b.company_id == can and s.get(Company, dup).status == "archived"   # lead followed, dup archived
        ok2, _ = CS.unmerge_companies(s, dup, admin); s.commit()
        s.refresh(b)
        assert ok2 and b.company_id == dup and s.get(Company, dup).status == "active"  # fully reversible


def test_duplicates_page_and_merge_action(ctx):
    client, engine, ids = ctx
    with Session(engine) as s:
        _lead(s, buyer_company="One", phone="+1 999 888 7777", source="manual", owner_id=ids["admin"])
        _lead(s, buyer_company="Two", phone="+1 999 888 7777", source="manual", owner_id=ids["admin"])
        CS.scan_duplicates(s); s.commit()
        dc = s.exec(select(DuplicateCandidate)).first()
        did = dc.id
    _login(client, "admin@t.local")
    assert client.get("/duplicates").status_code == 200
    assert client.post(f"/duplicates/{did}/dispose", data={"action": "merge"},
                       follow_redirects=False).status_code == 303
    with Session(engine) as s:
        assert s.get(DuplicateCandidate, did).status == "merged"


# ------------------------------------------------------------- export (CSV-safe, audited)
def test_csv_safe_neutralizes_formula_injection():
    assert main._csv_safe("=1+2") == "'=1+2"
    assert main._csv_safe("+cmd") == "'+cmd" and main._csv_safe("-x") == "'-x" and main._csv_safe("@a") == "'@a"
    assert main._csv_safe("Acme Ltd") == "Acme Ltd"


def test_export_is_audited_and_respects_admin(ctx):
    client, engine, ids = ctx
    with Session(engine) as s:
        _lead(s, buyer_company="=Evil", email="e@x.com", source="manual", owner_id=ids["admin"])
    _login(client, "admin@t.local")
    r = client.get("/export/buyers.csv")
    assert r.status_code == 200 and "text/csv" in r.headers["content-type"]
    assert "'=Evil" in r.text and "exported_at" in r.text and "exported_by" in r.text
    with Session(engine) as s:
        from app.models import AuditLog
        assert s.exec(select(AuditLog).where(AuditLog.action == "pii_export")).first() is not None
    _login(client, "kim@t.local")
    assert client.get("/export/buyers.csv", follow_redirects=False).status_code == 403


# ------------------------------------------------------------- non-blocking guarantee
def test_company_link_failure_never_breaks_lead_creation(ctx, monkeypatch):
    _, engine, ids = ctx
    monkeypatch.setattr(CS, "link_lead_company", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    with Session(engine) as s:
        l = _lead(s, buyer_company="Resilient Co", source="manual", owner_id=ids["admin"])
        assert l is not None and l.id                        # lead still created despite link failure
        from app.models import AuditLog
        assert s.exec(select(AuditLog).where(AuditLog.action == "dq_link_failed")).first() is not None
