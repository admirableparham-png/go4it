"""Phase 11 — mailbox + campaign controls (Founder / outreach manager only), the start gate, and enrolment safety:
always scoped to the campaign's request, the count the admin saw must still match, one enrolment per buyer/address.
Plus the sequence validation and the buyer-exact preview page."""
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine, select

import app.main as main
from app import campaign_service as CAMP
from app import config
from app import permissions as P
from app.auth import hash_password
from app.models import (AuditLog, Campaign, CampaignRecipient, Lead, MailAccount, ServiceRequest, User, UserProfile,
                        WorkItem)


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
        for ddl in ("CREATE UNIQUE INDEX IF NOT EXISTS uq_workitem_idem_open ON workitem(idempotency_key) "
                    "WHERE idempotency_key != '' AND status IN ('open','in_progress','waiting')",
                    "CREATE UNIQUE INDEX IF NOT EXISTS uq_userprofile_user ON userprofile(user_id) WHERE user_id IS NOT NULL",
                    "CREATE UNIQUE INDEX IF NOT EXISTS uq_camprcpt_campaign_lead ON campaignrecipient(campaign_id, lead_id) "
                    "WHERE lead_id IS NOT NULL",
                    "CREATE UNIQUE INDEX IF NOT EXISTS uq_camprcpt_campaign_email ON campaignrecipient(campaign_id, to_email) "
                    "WHERE to_email != ''"):
            c.execute(text(ddl))
        c.commit()
    monkeypatch.setattr(main, "engine", e)
    monkeypatch.setattr(config, "IMAP_ENABLED", True)
    monkeypatch.setattr(config, "IMAP_INTERVAL", 120)
    monkeypatch.setattr(config, "IMAP_USER", "info@qmat.example")
    monkeypatch.setattr(config, "IMAP_PASSWORD", "imap-app-password")   # hermetic: never depend on the local .env
    ids = {}
    with Session(e) as s:
        f = _mk(s, "founder@t", "admin", "internal", "founder")
        _mk(s, "analyst@t", "admin", "internal", "analyst")
        seller = _mk(s, "seller@t", "agent", "seller", "seller")
        other = _mk(s, "other@t", "agent", "seller", "seller")
        mb = MailAccount(user_id=f.id, email="info@qmat.example", from_name="Qmat Trading", active=True,
                         admin_owned=True, sender_company="Qmat Trading LLC", postal_address="Dubai, UAE")
        s.add(mb)
        r1 = ServiceRequest(request_type="buyer_hunt", product="Anchors", status="done", owner_id=seller.id,
                            requester_id=seller.id)
        r2 = ServiceRequest(request_type="buyer_hunt", product="Anchors", status="done", owner_id=seller.id,
                            requester_id=seller.id)
        s.add(r1); s.add(r2); s.commit(); s.refresh(mb); s.refresh(r1); s.refresh(r2)
        for i in range(3):
            s.add(Lead(product="Anchors", managed=True, seller_id=seller.id, request_id=r1.id, email=f"a{i}@x.example",
                       buyer_company=f"A{i}", dest_country="PL"))
        s.add(Lead(product="Anchors", managed=True, seller_id=seller.id, request_id=r1.id, email="+48 22 555 0101",
                   buyer_company="PhoneOnly", dest_country="PL"))
        s.add(Lead(product="Anchors", managed=True, seller_id=seller.id, request_id=r2.id, email="b@x.example",
                   buyer_company="OtherRequest", dest_country="PL"))
        c = Campaign(name="TR", tenant_id=seller.id, request_id=r1.id, owner_id=f.id, mailbox_id=mb.id, status="draft")
        s.add(c); s.commit(); s.refresh(c)
        CAMP.set_sequence(s, c, [{"subject": "Anchors for {company}", "body": "Hello {company}."}])
        ids.update(founder=f.id, seller=seller.id, other=other.id, mailbox=mb.id, campaign=c.id, r1=r1.id)
    return e, ids


def _client(email):
    c = TestClient(main.app)
    assert c.post("/login", data={"email": email, "password": "pw"}, follow_redirects=False).status_code == 303
    return c


def test_mailbox_controls_are_founder_only_and_audited(ctx):
    e, ids = ctx
    url = f"/mail/{ids['mailbox']}/controls"
    data = {"admin_owned": "1", "daily_limit": "10", "from_name": "Qmat Trading", "sender_company": "Qmat Trading LLC",
            "postal_address": "Office 1\nDubai, UAE", "paused": "1"}
    assert _client("seller@t").post(url, data=data, follow_redirects=False).status_code == 403
    assert _client("analyst@t").post(url, data=data, follow_redirects=False).status_code == 403
    assert _client("founder@t").post(url, data=data, follow_redirects=False).status_code == 303
    with Session(e) as s:
        mb = s.get(MailAccount, ids["mailbox"])
        assert (mb.daily_limit, mb.paused, mb.postal_address) == (10, True, "Office 1\nDubai, UAE")
        assert s.exec(select(AuditLog).where(AuditLog.entity_type == "mailbox", AuditLog.action == "controls")).first()


def test_credentials_reconnect_verifies_and_resumes(ctx, monkeypatch):
    e, ids = ctx
    monkeypatch.setattr(main, "verify_smtp", lambda h, p, u, pw: (pw == "good-app-pass", "bad credentials"))
    monkeypatch.setattr(main, "mail_encrypt", lambda pw: "enc:" + pw)
    with Session(e) as s:
        mb = s.get(MailAccount, ids["mailbox"]); mb.paused = True; mb.last_send_error = "auth: 535"; s.add(mb)
        s.add(WorkItem(type="mailbox_auth_failure", title="x", idempotency_key=f"mailbox_auth_failure:{mb.id}"))
        s.commit()
    c = _client("founder@t")
    c.post(f"/mail/{ids['mailbox']}/credentials", data={"app_password": "wrong"}, follow_redirects=False)
    with Session(e) as s:
        assert s.get(MailAccount, ids["mailbox"]).paused
    c.post(f"/mail/{ids['mailbox']}/credentials", data={"app_password": "good-app-pass"}, follow_redirects=False)
    with Session(e) as s:
        mb = s.get(MailAccount, ids["mailbox"])
        assert not mb.paused and mb.smtp_password_enc == "enc:good-app-pass" and mb.last_send_error == ""
        assert s.exec(select(WorkItem)).one().status not in ("open", "in_progress", "waiting")


def test_a_mailbox_in_use_cannot_be_deleted(ctx):
    e, ids = ctx
    _client("founder@t").post(f"/mail/{ids['mailbox']}/delete", follow_redirects=False)
    with Session(e) as s:
        assert s.get(MailAccount, ids["mailbox"]) is not None


def test_start_gate(ctx):
    e, ids = ctx
    c = _client("founder@t")
    url = f"/campaigns/{ids['campaign']}/status"
    with Session(e) as s:
        mb = s.get(MailAccount, ids["mailbox"]); mb.postal_address = ""; s.add(mb); s.commit()
    c.post(url, data={"to": "running"}, follow_redirects=False)
    with Session(e) as s:
        assert s.get(Campaign, ids["campaign"]).status == "draft"          # no footer → refused
        mb = s.get(MailAccount, ids["mailbox"]); mb.postal_address = "Dubai, UAE"; mb.paused = True; s.add(mb)
        s.commit()
    c.post(url, data={"to": "running"}, follow_redirects=False)
    with Session(e) as s:
        assert s.get(Campaign, ids["campaign"]).status == "draft"          # paused mailbox → refused
        mb = s.get(MailAccount, ids["mailbox"]); mb.paused = False; s.add(mb); s.commit()
        assert CAMP.start_problems(s, s.get(Campaign, ids["campaign"])) == []
    assert _client("analyst@t").post(url, data={"to": "running"}, follow_redirects=False).status_code == 403
    c.post(url, data={"to": "running"}, follow_redirects=False)
    with Session(e) as s:
        assert s.get(Campaign, ids["campaign"]).status == "running"


def test_start_needs_reply_reading(ctx, monkeypatch):
    e, ids = ctx
    monkeypatch.setattr(config, "IMAP_INTERVAL", 0)
    with Session(e) as s:
        assert any("IMAP" in p for p in CAMP.start_problems(s, s.get(Campaign, ids["campaign"])))


def test_sequence_rejects_unknown_merge_fields(ctx):
    e, ids = ctx
    _client("founder@t").post(f"/campaigns/{ids['campaign']}/sequence",
                              data={"subjects": ["Hi {first_name}"], "bodies": ["b"], "bodies_html": [""],
                                    "delays": ["0"]}, follow_redirects=False)
    with Session(e) as s:
        assert CAMP.steps_for(s, s.get(Campaign, ids["campaign"]))[0].subject == "Anchors for {company}"


def test_enrol_is_request_scoped_count_checked_and_once_only(ctx):
    e, ids = ctx
    c = _client("founder@t")
    with Session(e) as s:
        prev = CAMP.audience_preview(s, s.get(Campaign, ids["campaign"]), main._audience_filter(
            s.get(Campaign, ids["campaign"])))
        assert prev["final_eligible"] == 3 and prev["invalid_email"] == 1      # the other request's buyer is out
    url = f"/campaigns/{ids['campaign']}/audience"
    c.post(url, data={"do": "enroll", "expected": "2"}, follow_redirects=False)           # stale count
    with Session(e) as s:
        assert s.exec(select(CampaignRecipient)).all() == []
    c.post(url, data={"do": "enroll", "expected": "3"}, follow_redirects=False)
    c.post(url, data={"do": "enroll", "expected": "0"}, follow_redirects=False)           # a double submit
    with Session(e) as s:
        rows = s.exec(select(CampaignRecipient)).all()
        assert sorted(r.to_email for r in rows) == ["a0@x.example", "a1@x.example", "a2@x.example"]


def test_campaign_controls_persist(ctx):
    e, ids = ctx
    _client("founder@t").post(f"/campaigns/{ids['campaign']}/controls",
                              data={"daily_limit": "10", "send_window_start": "7", "send_window_end": "16",
                                    "send_days": ["0", "1", "2", "3"]}, follow_redirects=False)
    with Session(e) as s:
        cp = s.get(Campaign, ids["campaign"])
        assert (cp.daily_limit, cp.send_window_start, cp.send_window_end, cp.send_days) == (10, 7, 16, "0,1,2,3")


def test_preview_shows_the_exact_buyer_email_in_a_sandbox(ctx):
    e, ids = ctx
    with Session(e) as s:
        ld = s.exec(select(Lead).where(Lead.buyer_company == "A0")).one()
        s.add(CampaignRecipient(campaign_id=ids["campaign"], tenant_id=ids["seller"], lead_id=ld.id,
                                to_email=ld.email, status="pending"))
        s.commit()
    r = _client("founder@t").get(f"/campaigns/{ids['campaign']}/preview?step=0")
    assert r.status_code == 200
    assert 'sandbox=""' in r.text and "Anchors for A0" in r.text and "List-Unsubscribe" in r.text
    assert _client("seller@t").get(f"/campaigns/{ids['campaign']}/preview?step=0").status_code == 403


def test_pages_render(ctx):
    e, ids = ctx
    c = _client("founder@t")
    r = c.get("/mail")
    assert r.status_code == 200 and "Go4it-owned" in r.text and "Sending controls" in r.text
    r = c.get(f"/campaigns/{ids['campaign']}?preview=1")
    assert r.status_code == 200 and "Before it can run" in r.text and "Final eligible" in r.text
    assert "optional HTML design" in r.text
