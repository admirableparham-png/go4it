"""Phase 11 — security fixes: Founder accounts can't be taken over or neutralised by an Admin/Manager, only internal
staff with outreach.email.send can EMAIL a buyer from the lead page, and bulk email never reaches confidential
managed buyers (they go through Campaigns) or the same address twice."""
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine, select

import app.main as main
from app import access_service as ACCESS
from app import permissions as P
from app.auth import hash_password, verify_password
from app.models import Lead, MailAccount, Outreach, User, UserProfile

_IDX = (
    "CREATE UNIQUE INDEX IF NOT EXISTS uq_workitem_idem_open ON workitem(idempotency_key) "
    "WHERE idempotency_key != '' AND status IN ('open','in_progress','waiting')",
    "CREATE UNIQUE INDEX IF NOT EXISTS uq_userprofile_user ON userprofile(user_id) WHERE user_id IS NOT NULL",
    "CREATE UNIQUE INDEX IF NOT EXISTS uq_roletemplate_key ON roletemplate(key) WHERE key != ''",
)


def _mk(s, email, role, account_class, role_key):
    u = User(email=email, name=email.split("@")[0], role=role, active=True, password_hash=hash_password("pw"))
    s.add(u); s.commit(); s.refresh(u)
    s.add(UserProfile(user_id=u.id, account_class=account_class, role_key=role_key,
                      scope=P.ROLE_TEMPLATES[role_key]["scope"], account_status="active"))
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
        _mk(s, "founder@t", "admin", "internal", "founder")
        _mk(s, "founder2@t", "admin", "internal", "founder")
        _mk(s, "mgr@t", "manager", "internal", "admin_manager")      # prod shape: legacy manager -> Admin/Manager
        _mk(s, "outreach@t", "admin", "internal", "outreach_manager")
        _mk(s, "analyst@t", "admin", "internal", "analyst")
        seller = _mk(s, "seller@t", "agent", "seller", "seller")
        s.add(Lead(product="Anchors", owner_id=seller.id, email="own-buyer@x.com", buyer_company="Own Buyer"))
        s.commit()
    return e


def _u(s, email):
    return s.exec(select(User).where(User.email == email)).one()


def _login(c, email):
    assert c.post("/login", data={"email": email, "password": "pw"}, follow_redirects=False).status_code == 303


# ---- Founder protection ---------------------------------------------------------------------------------------
def test_manager_cannot_reset_founder_password_via_route(ctx):
    with Session(ctx) as s:
        fid, before = _u(s, "founder@t").id, _u(s, "founder@t").password_hash
    c = TestClient(main.app); _login(c, "mgr@t")
    r = c.post(f"/admin/users/{fid}/password", data={"password": "takeover1"}, follow_redirects=False)
    assert r.status_code == 303 and "error=" in r.headers["location"]
    with Session(ctx) as s:
        f = _u(s, "founder@t")
        assert f.password_hash == before
        assert not verify_password("takeover1", f.password_hash)
    _login(TestClient(main.app), "founder@t")                     # the founder still logs in with the old password


def test_founder_resets_founder_and_manager_resets_seller(ctx):
    with Session(ctx) as s:
        founder, f2, mgr, seller = (_u(s, e) for e in ("founder@t", "founder2@t", "mgr@t", "seller@t"))
        assert ACCESS.reset_password(s, founder, f2, "newpass1") == (True, "password reset")
        assert ACCESS.reset_password(s, mgr, seller, "newpass2") == (True, "password reset")
        s.commit()
        assert verify_password("newpass1", _u(s, "founder2@t").password_hash)
        assert verify_password("newpass2", _u(s, "seller@t").password_hash)


def test_manager_cannot_disable_demote_or_deny_a_founder(ctx):
    with Session(ctx) as s:
        mgr, f2 = _u(s, "mgr@t"), _u(s, "founder2@t")      # f2 is NOT the last founder: only the new guard stops it
        ok, msg = ACCESS.set_status(s, mgr, f2, "disabled")
        assert not ok and "founder" in msg
        ok, msg = ACCESS.set_role(s, mgr, f2, "admin_manager")
        assert not ok and "founder" in msg
        ok, msg = ACCESS.set_override(s, mgr, f2, "users.manage", "deny")
        assert not ok and "founder" in msg
        s.commit()
        assert _u(s, "founder2@t").active
        p = s.exec(select(UserProfile).where(UserProfile.user_id == f2.id)).one()
        assert p.role_key == "founder" and p.account_status == "active"


def test_founder_can_still_manage_another_founder(ctx):
    with Session(ctx) as s:
        founder, f2 = _u(s, "founder@t"), _u(s, "founder2@t")
        assert ACCESS.set_status(s, founder, f2, "disabled")[0]
        s.commit()
        assert not _u(s, "founder2@t").active


# ---- single-lead send is internal-only -------------------------------------------------------------------------
@pytest.fixture
def spy_send(monkeypatch):
    calls = []

    def fake_send_email(to, subject, text_body, html=None, **kw):
        calls.append(to)
        return True, "", "<mid@test>"
    monkeypatch.setattr(main, "send_email", fake_send_email)
    return calls


def test_seller_cannot_email_a_buyer_but_can_still_log(ctx, spy_send):
    with Session(ctx) as s:
        lid = s.exec(select(Lead)).first().id
    c = TestClient(main.app); _login(c, "seller@t")
    r = c.post(f"/leads/{lid}/outreach", data={"channel": "email", "subject": "hi", "body": "b", "send": "1"},
               follow_redirects=False)
    assert r.status_code == 403
    assert spy_send == []
    r = c.post(f"/leads/{lid}/outreach", data={"channel": "call", "body": "called them"}, follow_redirects=False)
    assert r.status_code == 303
    with Session(ctx) as s:
        rows = s.exec(select(Outreach).where(Outreach.lead_id == lid)).all()
        assert [o.status for o in rows] == ["logged"]


def test_internal_send_needs_the_send_permission(ctx, spy_send):
    with Session(ctx) as s:
        lid = s.exec(select(Lead)).first().id
    data = {"channel": "email", "subject": "hi", "body": "b", "send": "1"}
    c = TestClient(main.app); _login(c, "analyst@t")                     # internal, but no outreach.email.send
    assert c.post(f"/leads/{lid}/outreach", data=data, follow_redirects=False).status_code == 403
    assert spy_send == []
    c = TestClient(main.app); _login(c, "outreach@t")
    assert c.post(f"/leads/{lid}/outreach", data=data, follow_redirects=False).status_code == 303
    assert spy_send == ["own-buyer@x.com"]


# ---- bulk email never reaches managed buyers or duplicates -----------------------------------------------------
def test_bulk_email_skips_managed_buyers_and_duplicate_addresses(ctx, monkeypatch):
    sent = []

    def fake_bulk(acct, items, reply_to=None):
        sent.extend(i[0] for i in items)
        return [(i[0], True, "", f"<{n}@t>") for n, i in enumerate(items)]
    monkeypatch.setattr(main, "send_bulk_via_account", fake_bulk)
    with Session(ctx) as s:
        founder = _u(s, "founder@t")
        s.add(MailAccount(user_id=founder.id, email="me@qmat.example", active=True))
        s.add(Lead(product="Anchors", owner_id=None, managed=True, seller_id=_u(s, "seller@t").id,
                   email="managed@x.com", buyer_company="Managed Buyer"))
        s.add(Lead(product="Anchors", owner_id=founder.id, email="plain@x.com", buyer_company="Plain"))
        s.add(Lead(product="Anchors", owner_id=founder.id, email="PLAIN@x.com", buyer_company="Plain dup"))
        s.commit()
        acct_id = s.exec(select(MailAccount)).one().id
        ids = [ld.id for ld in s.exec(select(Lead).where(Lead.product == "Anchors")).all()]
    c = TestClient(main.app); _login(c, "founder@t")
    r = c.post("/leads/bulk/email", data={"account_id": acct_id, "subject": "Offer", "body": "Hello",
                                          "ids": ids}, follow_redirects=False)
    assert r.status_code == 303
    # the managed buyer is skipped and PLAIN@x.com is the same address as plain@x.com
    assert sorted(e.lower() for e in sent) == ["own-buyer@x.com", "plain@x.com"]
