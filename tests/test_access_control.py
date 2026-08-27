"""Phase 10 — access-control proofs: role matrix, seller confidentiality (incl. downloads), cross-tenant,
analyst-no-PII, researcher-no-send, outreach-no-payments, operations-no-user-mgmt, finance-no-export,
live-Claude-founder-only, overrides-can't-bypass-seller-rule, disabled-loses-access, final-founder, audit,
exports=pages, no leak via Command tools, and additive/idempotent/count-invariant migration."""
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine, func, select

import app.main as main
from app import access_service as ACCESS
from app import authz
from app import permissions as P
from app.auth import hash_password
from app.models import (AccessAuditLog, Company, Contact, Lead, PaymentMilestone, PermissionOverride,
                        ServiceRequest, User, UserProfile)

_IDX = (
    "CREATE UNIQUE INDEX IF NOT EXISTS uq_workitem_idem_open ON workitem(idempotency_key) "
    "WHERE idempotency_key != '' AND status IN ('open','in_progress','waiting')",
    "CREATE UNIQUE INDEX IF NOT EXISTS uq_userprofile_user ON userprofile(user_id) WHERE user_id IS NOT NULL",
    "CREATE UNIQUE INDEX IF NOT EXISTS uq_roletemplate_key ON roletemplate(key) WHERE key != ''",
)


def _mk(s, email, role, account_class, role_key, active=True):
    u = User(email=email, name=email.split("@")[0], role=role, active=active, password_hash=hash_password("pw"))
    s.add(u); s.commit(); s.refresh(u)
    s.add(UserProfile(user_id=u.id, account_class=account_class, role_key=role_key,
                      scope=P.ROLE_TEMPLATES[role_key]["scope"],
                      account_status="active" if active else "disabled"))
    s.commit()
    return u


@pytest.fixture
def ctx(monkeypatch):
    e = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    SQLModel.metadata.create_all(e)
    with e.connect() as c:
        for ddl in _IDX:
            c.execute(text(ddl))
        c.commit()
    monkeypatch.setattr(main, "engine", e)
    with Session(e) as s:
        # internal staff carry legacy role 'admin' (reach internal workspaces); sellers carry 'agent'
        _mk(s, "founder@t", "admin", "internal", "founder")
        _mk(s, "analyst@t", "admin", "internal", "analyst")
        _mk(s, "researcher@t", "admin", "internal", "researcher")
        _mk(s, "outreach@t", "admin", "internal", "outreach_manager")
        _mk(s, "finance@t", "admin", "internal", "finance_compliance")
        _mk(s, "ops@t", "admin", "internal", "operations_manager")
        sa = _mk(s, "sellera@t", "agent", "seller", "seller")
        sb = _mk(s, "sellerb@t", "agent", "seller", "seller")
        # buyer PII + a payment + a sellerA-owned request
        co = Company(name="ACME BUYER LLC", tenant_id=None, country="IQ", primary_role="buyer")
        s.add(co); s.commit(); s.refresh(co)
        s.add(Contact(company_id=co.id, name="Jane Secret", email="jane@acme.iq", phone="+964 1 000"))
        # a managed (admin-pool) buyer lead — never owned by a seller (matches the confidential model)
        s.add(Lead(product="Zinc", tracking_code="G4-A", owner_id=None, managed=True, seller_id=sa.id,
                   buyer_company="ACME BUYER LLC", contact_name="Jane Secret"))
        s.add(ServiceRequest(request_type="remittance", product="Zinc", market="Iraq", status="approved",
                             owner_id=sa.id, requester_id=sa.id))
        s.add(PaymentMilestone(tenant_id=None, milestone_type="buyer_deposit", expected_amount="100",
                               currency="USD", status="planned"))
        s.commit()
        cid = co.id
    return e, cid


def _login(c, email):
    assert c.post("/login", data={"email": email, "password": "pw"}, follow_redirects=False).status_code == 303


def _u(s, email):
    return s.exec(select(User).where(User.email == email)).one()


# ---- 1. role matrix (unit) ----------------------------------------------------------------------
def test_role_matrix_allowed_and_denied(ctx):
    e, _ = ctx
    with Session(e) as s:
        cases = {
            "founder@t": {"ai.live.use": True, "users.manage": True, "buyer.pii.export": True, "payment.confirm": True},
            "analyst@t": {"intelligence.view": True, "buyer.pii.view": False, "buyer.pii.export": False,
                          "payment.confirm": False, "ai.live.use": False},
            "researcher@t": {"research.run": True, "buyer.pii.view": True, "outreach.email.send": False},
            "outreach@t": {"outreach.email.send": True, "payment.confirm": False, "users.manage": False},
            "finance@t": {"payment.confirm": True, "buyer.pii.export": False, "buyer.pii.view": False},
            "ops@t": {"freight.manage": True, "users.manage": False, "payment.confirm": False},
            "sellera@t": {k: False for k in ("buyer.pii.view", "outreach.email.send", "ai.live.use",
                                             "users.manage", "intelligence.view")},
        }
        for email, expect in cases.items():
            u = _u(s, email)
            for perm, want in expect.items():
                assert authz.has_permission(s, u, perm) is want, f"{email} {perm} expected {want}"


# ---- 2. seller confidentiality across workspaces + downloads ------------------------------------
def test_seller_denied_all_internal_pages_and_downloads(ctx):
    e, cid = ctx
    c = TestClient(main.app); _login(c, "sellera@t")
    # buyer-PII-exposing + admin-only surfaces are hard-denied (403/404) — never a leak path
    for path in [f"/companies/{cid}", "/export/buyers.csv", "/admin/users", "/admin/roles",
                 "/admin/access-log", "/command", "/operations", "/intelligence"]:
        r = c.get(path, follow_redirects=False)
        assert r.status_code in (403, 404), f"seller reached {path} ({r.status_code})"
    # owner-scoped surfaces (e.g. /leads) render for a seller but MUST NOT contain the managed buyer's identity
    body = c.get("/leads").text
    assert "ACME BUYER" not in body and "Jane Secret" not in body and "jane@acme.iq" not in body


def test_overrides_cannot_bypass_hard_seller_rule(ctx):
    e, cid = ctx
    with Session(e) as s:
        founder, seller = _u(s, "founder@t"), _u(s, "sellera@t")
        # even a manager granting buyer.pii.view to a seller must be refused, and authz strips it regardless
        ok, msg = ACCESS.set_override(s, founder, seller, "buyer.pii.view", "grant"); s.commit()
        assert not ok and "hard" in msg.lower()
        # force an override row directly and confirm authz still denies
        s.add(PermissionOverride(user_id=seller.id, permission_key="buyer.pii.view", effect="grant")); s.commit()
        assert authz.has_permission(s, seller, "buyer.pii.view") is False
    c = TestClient(main.app); _login(c, "sellera@t")
    assert c.get(f"/companies/{cid}", follow_redirects=False).status_code in (403, 404)


# ---- 3. cross-tenant (seller B cannot see seller A) ---------------------------------------------
def test_cross_tenant_hidden(ctx):
    e, _ = ctx
    with Session(e) as s:
        sa_req = s.exec(select(ServiceRequest)).first().id
    c = TestClient(main.app); _login(c, "sellerb@t")
    assert c.get(f"/requests/{sa_req}/thread", follow_redirects=False).status_code == 404


# ---- 4-8. internal least-privilege on the dangerous surface (route-level) -----------------------
def test_analyst_cannot_view_buyer_pii(ctx):
    e, cid = ctx
    c = TestClient(main.app); _login(c, "analyst@t")
    assert c.get(f"/companies/{cid}", follow_redirects=False).status_code == 403
    with Session(e) as s:                                   # and the AI contact tool refuses
        from app import ai_tools
        out = ai_tools.run_tool(s, "lookup_contact", {"company_id": cid}, _u(s, "analyst@t"))
        assert "contacts" not in out["result"]


def test_researcher_cannot_send_outreach(ctx):
    c = TestClient(main.app); _login(c, "researcher@t")
    assert c.post("/leads/bulk/email", data={"account_id": 1, "subject": "x", "body": "y"},
                  follow_redirects=False).status_code == 403


def test_outreach_cannot_confirm_payments(ctx):
    e, _ = ctx
    with Session(e) as s:
        pm = s.exec(select(PaymentMilestone)).first().id
    c = TestClient(main.app); _login(c, "outreach@t")
    assert c.post(f"/operations/payments/{pm}/confirm", data={"confirmed_amount": "100"},
                  follow_redirects=False).status_code == 403


def test_operations_cannot_manage_users(ctx):
    c = TestClient(main.app); _login(c, "ops@t")
    assert c.get("/admin/users", follow_redirects=False).status_code == 403
    assert c.post("/admin/users", data={"email": "x@t", "password": "secret1", "account_class": "seller"},
                  follow_redirects=False).status_code == 403


def test_finance_cannot_export_buyer_contacts(ctx):
    c = TestClient(main.app); _login(c, "finance@t")
    assert c.get("/export/buyers.csv", follow_redirects=False).status_code == 403
    # but the founder (buyer.pii.export) can
    fc = TestClient(main.app); _login(fc, "founder@t")
    assert fc.get("/export/buyers.csv", follow_redirects=False).status_code == 200


def test_exports_enforce_same_perms_as_pages(ctx):
    e, cid = ctx
    c = TestClient(main.app); _login(c, "analyst@t")
    assert c.get(f"/companies/{cid}", follow_redirects=False).status_code == 403      # page
    assert c.get("/export/buyers.csv", follow_redirects=False).status_code == 403     # download — same gate


# ---- 9. live Claude founder-only (unit) ---------------------------------------------------------
def test_live_claude_founder_only(ctx):
    e, _ = ctx
    with Session(e) as s:
        assert authz.has_permission(s, _u(s, "founder@t"), "ai.live.use") is True
        for other in ("analyst@t", "researcher@t", "outreach@t", "finance@t", "ops@t", "sellera@t"):
            assert authz.has_permission(s, _u(s, other), "ai.live.use") is False, other


# ---- 11. disabled users lose access -------------------------------------------------------------
def test_disabled_user_loses_access(ctx):
    e, _ = ctx
    c = TestClient(main.app); _login(c, "outreach@t")
    assert c.get("/", follow_redirects=False).status_code == 200
    with Session(e) as s:
        founder, target = _u(s, "founder@t"), _u(s, "outreach@t")
        ok, _m = ACCESS.set_status(s, founder, target, "disabled"); s.commit()
        assert ok
    # next request → logged out (session revoked / account not active)
    assert c.get("/", follow_redirects=False).status_code in (302, 303)


# ---- 12. final Founder cannot be removed --------------------------------------------------------
def test_last_founder_cannot_be_removed_or_demoted(ctx):
    e, _ = ctx
    with Session(e) as s:
        founder = _u(s, "founder@t")
        assert authz.is_last_founder(s, founder.id) is True
        ok, msg = ACCESS.set_status(s, founder, founder, "disabled"); assert not ok
        ok2, msg2 = ACCESS.set_role(s, founder, founder, "admin_manager"); assert not ok2 and "last active Founder" in msg2


# ---- 13. permission changes are audited (no secrets) --------------------------------------------
def test_permission_changes_audited(ctx):
    e, _ = ctx
    with Session(e) as s:
        founder, analyst = _u(s, "founder@t"), _u(s, "analyst@t")
        ok, _m = ACCESS.set_override(s, founder, analyst, "buyer.pii.view", "grant", reason="temp"); s.commit()
        assert ok
        row = s.exec(select(AccessAuditLog).where(AccessAuditLog.action == "permission_granted")).first()
        assert row and row.target_user_id == analyst.id and row.field == "buyer.pii.view"
        # a secret-looking reason is redacted
        ACCESS.set_override(s, founder, analyst, "research.view", "grant",
                            reason="api_key=sk-ant-shouldnotstore"); s.commit()
        r2 = s.exec(select(AccessAuditLog).where(AccessAuditLog.field == "research.view")).first()
        assert "sk-ant" not in (r2.reason or "") and "[redacted]" in (r2.reason or "")


# ---- 16. migration additive, count-invariant, idempotent ---------------------------------------
def test_backfill_idempotent_and_count_invariant(ctx):
    e, _ = ctx
    import scripts.backfill_access_control as BF
    import app.db as db
    # point the backfill's engine at the test engine
    import sqlmodel
    orig = db.engine
    try:
        db.engine = e
        BF.engine = e
        with Session(e) as s:
            before = {t: s.exec(select(func.count()).select_from(m)).one()
                      for t, m in (("users", User), ("leads", Lead), ("requests", ServiceRequest))}
            prof_before = s.exec(select(func.count()).select_from(UserProfile)).one()
        BF.migrate(dry=False)   # re-run over already-backfilled users
        with Session(e) as s:
            after = {t: s.exec(select(func.count()).select_from(m)).one()
                     for t, m in (("users", User), ("leads", Lead), ("requests", ServiceRequest))}
            prof_after = s.exec(select(func.count()).select_from(UserProfile)).one()
        assert after == before                       # operational counts invariant
        assert prof_after == prof_before             # idempotent: no duplicate profiles
    finally:
        db.engine = orig
        BF.engine = orig
