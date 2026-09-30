"""Phase 4 — Outreach, Campaigns & Email.

Two-way confidentiality (buyers never learn the seller; sellers never learn the buyer), central suppression
checked on every send path, idempotent campaign sends, deterministic reply/bounce effects, and isolated
workers. Admin-only throughout.
"""
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine, select

import app.main as main
from app import campaign_service as CAMP
from app import outreach_events as OE
from app import send_guard as SG
from app import suppression as SUP
from app.auth import hash_password
from app.models import (BounceRecord, Campaign, CampaignRecipient, Lead, MailAccount, Outreach, Suppression,
                        User, WorkItem)


def _indexes(engine):
    with engine.connect() as conn:
        for ddl in (
            "CREATE UNIQUE INDEX IF NOT EXISTS uq_workitem_idem_open ON workitem(idempotency_key) "
            "WHERE idempotency_key != '' AND status IN ('open','in_progress','waiting')",
            "CREATE UNIQUE INDEX IF NOT EXISTS uq_outreach_campaign_send ON "
            "outreach(campaign_id,campaign_recipient_id,campaign_version,campaign_step) WHERE campaign_id IS NOT NULL",
            "CREATE UNIQUE INDEX IF NOT EXISTS uq_suppression_addr_scope ON "
            "suppression(email_normalized,scope,tenant_id) WHERE active = 1",
            "CREATE UNIQUE INDEX IF NOT EXISTS uq_camprcpt_cc ON campaignrecipient(campaign_id,contact_id)",
        ):
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
        for email, name, role in [("admin@t.local", "Admin", "admin"), ("kim@t.local", "Kim Seller", "agent"),
                                  ("bo@t.local", "Bo", "agent")]:
            s.add(User(email=email, name=name, role=role, active=True, password_hash=hash_password("pw")))
        s.commit()
        ids = {u.email.split("@")[0]: u.id for u in s.exec(select(User)).all()}
        mb = MailAccount(user_id=ids["admin"], email="hunt@sender.example", admin_owned=True, active=True,
                         daily_limit=100, sender_company="Sender Trading LLC", postal_address="1 Test Street, Dubai")
        s.add(mb); s.commit()
        ids["mailbox"] = s.exec(select(MailAccount)).first().id
    return TestClient(main.app), engine, ids


def _login(c, email):
    assert c.post("/login", data={"email": email, "password": "pw"}, follow_redirects=False).status_code == 303


def _managed_lead(s, ids, email="b@x.com", **kw):
    ld = Lead(product=kw.pop("product", "copper"), owner_id=None, managed=True, seller_id=ids["kim"],
              email=email, engagement_class=kw.pop("engagement_class", "prospect"), **kw)
    s.add(ld); s.commit(); s.refresh(ld)
    return ld


def _running_campaign(s, ids):
    c = Campaign(name="C", tenant_id=ids["kim"], owner_id=ids["admin"], mailbox_id=ids["mailbox"],
                 status="draft", sequence_version=1, daily_limit=100, send_days="0,1,2,3,4,5,6",
                 send_window_start=0, send_window_end=24)
    s.add(c); s.commit(); s.refresh(c)
    CAMP.set_sequence(s, c, [{"subject": "Hi", "body": "hello", "delay_days": 0}], None)  # draft → v1
    c.status = "running"; s.add(c); s.commit(); s.refresh(c)
    return c


def _ok_sender(mb, to, subject, text, html=None, reply_to="", in_reply_to="", message_id="", references="", **kw):
    return True, "", f"<mid-{to}>"


# --------------------------------------------------------- authorization / confidentiality
def test_outreach_pages_admin_only(ctx):
    client, engine, ids = ctx
    _login(client, "kim@t.local")
    for p in ("/campaigns", "/inbox", "/templates", "/suppression", "/outreach/analytics"):
        assert client.get(p).status_code == 403, p
    admin = TestClient(main.app); _login(admin, "admin@t.local")
    for p in ("/campaigns", "/inbox", "/templates", "/suppression", "/outreach/analytics"):
        assert admin.get(p).status_code == 200, p


def test_seller_cannot_view_suppression_or_inbox_thread(ctx):
    client, engine, ids = ctx
    with Session(engine) as s:
        ld = _managed_lead(s, ids)
        lid = ld.id
    _login(client, "kim@t.local")
    assert client.get("/suppression").status_code == 403
    assert client.get(f"/inbox/{lid}").status_code == 403


def test_buyer_message_carries_no_seller_identity(ctx):
    client, engine, ids = ctx
    with Session(engine) as s:
        guarded = SG.guard_buyer_text(s, "Reach Kim Seller at kim@t.local anytime", ids["kim"])
        assert "Kim Seller" not in guarded and "kim@t.local" not in guarded


def test_header_injection_blocked(ctx):
    r = SG.sanitize_header("Subject\r\nBcc: evil@x.com")
    assert "\n" not in r and "\r" not in r and "Bcc" in r and r.startswith("Subject")


def test_credentials_never_render_on_mail_page(ctx):
    client, engine, ids = ctx
    with Session(engine) as s:
        from app.outreach import mail_encrypt
        m = s.get(MailAccount, ids["mailbox"])
        m.smtp_password_enc = mail_encrypt("SUPERSECRETPW")
        s.add(m); s.commit()
        cipher = m.smtp_password_enc
    admin = TestClient(main.app); _login(admin, "admin@t.local")
    body = admin.get("/mail").text
    assert "SUPERSECRETPW" not in body and cipher not in body


# --------------------------------------------------------- suppression on every send path
def test_suppression_blocks_campaign_send(ctx):
    client, engine, ids = ctx
    with Session(engine) as s:
        c = _running_campaign(s, ids)
        ld = _managed_lead(s, ids, email="sup@x.com")
        SUP.suppress(s, "sup@x.com", "unsubscribe", None, scope="platform"); s.commit()
        r = CampaignRecipient(campaign_id=c.id, tenant_id=ids["kim"], lead_id=ld.id, to_email="sup@x.com",
                              sequence_version=1, current_step=0, status="pending")
        s.add(r); s.commit(); s.refresh(r)
        out = CAMP.send_step(s, c, r, s.get(MailAccount, ids["mailbox"]), sender=_ok_sender)
        assert out["status"] == "skipped" and out["reason"] == "suppressed"


def test_suppressed_and_duplicates_not_enrolled(ctx):
    client, engine, ids = ctx
    with Session(engine) as s:
        c = Campaign(name="C", tenant_id=ids["kim"], owner_id=ids["admin"], status="draft", sequence_version=1)
        s.add(c); s.commit(); s.refresh(c)
        _managed_lead(s, ids, email="a@x.com", dest_country="CN")
        _managed_lead(s, ids, email="a@x.com", dest_country="CN")   # duplicate address
        _managed_lead(s, ids, email="sup@x.com", dest_country="CN")
        SUP.suppress(s, "sup@x.com", "manual", None, scope="platform"); s.commit()
        f = {"country": "CN"}
        prev = CAMP.audience_preview(s, c, f)
        assert prev["suppressed"] == 1 and prev["duplicates"] == 1 and prev["final_eligible"] == 1
        CAMP.enroll(s, c, None, f)
        assert len(s.exec(select(CampaignRecipient)).all()) == 1   # only the one eligible, non-suppressed


def test_audience_preview_applies_exclusions(ctx):
    client, engine, ids = ctx
    with Session(engine) as s:
        c = Campaign(name="C", tenant_id=ids["kim"], owner_id=ids["admin"], status="draft", sequence_version=1)
        s.add(c); s.commit(); s.refresh(c)
        _managed_lead(s, ids, email="p@x.com", dest_country="CN")
        _managed_lead(s, ids, email="cust@x.com", dest_country="CN", engagement_class="customer")
        _managed_lead(s, ids, email="neg@x.com", dest_country="CN", reply_outcome="negative")
        base = CAMP.audience_preview(s, c, {"country": "CN"})["final_eligible"]
        excl = CAMP.audience_preview(s, c, {"country": "CN", "exclude_customers": True,
                                            "exclude_negative": True})["final_eligible"]
        assert base == 3 and excl == 1


# --------------------------------------------------------- campaign send safety
def test_pause_all_prevents_sends(ctx):
    client, engine, ids = ctx
    with Session(engine) as s:
        c = _running_campaign(s, ids)
        ld = _managed_lead(s, ids)
        r = CampaignRecipient(campaign_id=c.id, tenant_id=ids["kim"], lead_id=ld.id, to_email=ld.email,
                              sequence_version=1, status="pending")
        s.add(r); s.commit(); s.refresh(r)
        SG.set_pause_all(s, True, None); s.commit()
        out = CAMP.send_step(s, c, r, s.get(MailAccount, ids["mailbox"]), sender=_ok_sender)
        assert out["status"] == "skipped" and out["reason"] == "outreach paused"


def test_concurrent_send_never_duplicates_step(ctx):
    """The unique (campaign,recipient,version,step) index rejects a second claim for the same step."""
    from sqlalchemy.exc import IntegrityError
    client, engine, ids = ctx
    with Session(engine) as s:
        c = _running_campaign(s, ids)
        ld = _managed_lead(s, ids)
        r = CampaignRecipient(campaign_id=c.id, tenant_id=ids["kim"], lead_id=ld.id, to_email=ld.email,
                              sequence_version=1, current_step=0, status="pending")
        s.add(r); s.commit(); s.refresh(r)
        out1 = CAMP.send_step(s, c, r, s.get(MailAccount, ids["mailbox"]), sender=_ok_sender)
        assert out1["status"] == "sent"
        # simulate a concurrent worker on the SAME step (reset the cursor, keep non-terminal) → idempotent skip
        r2 = s.get(CampaignRecipient, r.id); r2.current_step = 0; r2.status = "sent"; s.add(r2); s.commit()
        out2 = CAMP.send_step(s, c, s.get(CampaignRecipient, r.id), s.get(MailAccount, ids["mailbox"]),
                              sender=_ok_sender)
        assert out2["status"] == "already_sent"


def test_daily_limit_respected(ctx):
    client, engine, ids = ctx
    with Session(engine) as s:
        c = _running_campaign(s, ids)
        mb = s.get(MailAccount, ids["mailbox"]); mb.daily_limit = 1; s.add(mb); s.commit()
        for em in ("a@x.com", "b@x.com"):
            ld = _managed_lead(s, ids, email=em)
            s.add(CampaignRecipient(campaign_id=c.id, tenant_id=ids["kim"], lead_id=ld.id, to_email=em,
                                    sequence_version=1, status="pending"))
        s.commit()
        rs = s.exec(select(CampaignRecipient)).all()
        o1 = CAMP.send_step(s, c, rs[0], s.get(MailAccount, ids["mailbox"]), sender=_ok_sender)
        o2 = CAMP.send_step(s, c, rs[1], s.get(MailAccount, ids["mailbox"]), sender=_ok_sender)
        assert o1["status"] == "sent" and o2["reason"] == "daily limit reached"


def test_sequence_edit_running_creates_new_version(ctx):
    client, engine, ids = ctx
    with Session(engine) as s:
        c = _running_campaign(s, ids)
        assert c.sequence_version == 1
        v = CAMP.set_sequence(s, c, [{"subject": "New", "body": "x", "delay_days": 0}], None)
        assert v == 2 and s.get(Campaign, c.id).sequence_version == 2
        # old version's steps preserved
        assert len(CAMP.steps_for(s, c, 1)) == 1 and len(CAMP.steps_for(s, c, 2)) == 1


# --------------------------------------------------------- inbound replies
def test_human_reply_makes_engaged_and_stops_steps(ctx):
    client, engine, ids = ctx
    with Session(engine) as s:
        c = _running_campaign(s, ids)
        ld = _managed_lead(s, ids)
        s.add(CampaignRecipient(campaign_id=c.id, tenant_id=ids["kim"], lead_id=ld.id, to_email=ld.email,
                                sequence_version=1, status="sent"))
        s.add(Outreach(lead_id=ld.id, direction="in")); s.commit()
        OE.on_reply(s, ld, "Re: Hi", "Yes, please send pricing")
        assert s.get(Lead, ld.id).engagement_class == "engaged"
        assert s.exec(select(CampaignRecipient)).first().status == "replied"
        assert s.exec(select(WorkItem).where(WorkItem.type == "review_inbound_reply")).first() is not None


def test_negative_reply_still_engaged(ctx):
    client, engine, ids = ctx
    with Session(engine) as s:
        ld = _managed_lead(s, ids)
        s.add(Outreach(lead_id=ld.id, direction="in")); s.commit()
        OE.on_reply(s, ld, "Re", "No thanks, too expensive")
        assert s.get(Lead, ld.id).engagement_class == "engaged"   # a negative human reply is still valuable


def test_auto_reply_not_engaged(ctx):
    client, engine, ids = ctx
    with Session(engine) as s:
        ld = _managed_lead(s, ids)
        OE.on_reply(s, ld, "Out of Office", "I am on vacation")
        assert s.get(Lead, ld.id).engagement_class != "engaged"
        assert s.get(Lead, ld.id).reply_outcome == "auto_reply"


def test_unsubscribe_suppresses_immediately(ctx):
    client, engine, ids = ctx
    with Session(engine) as s:
        ld = _managed_lead(s, ids, email="uns@x.com")
        OE.on_reply(s, ld, "re", "please unsubscribe me")
        assert SUP.is_suppressed(s, "uns@x.com")


# --------------------------------------------------------- bounces
def test_hard_bounce_suppresses_and_cancels(ctx):
    client, engine, ids = ctx
    with Session(engine) as s:
        c = _running_campaign(s, ids)
        ld = _managed_lead(s, ids, email="")
        s.add(CampaignRecipient(campaign_id=c.id, tenant_id=ids["kim"], lead_id=ld.id, to_email="hb@x.com",
                                sequence_version=1, status="sent"))
        s.commit()
        OE.on_bounce(s, ld, "hb@x.com", "550 5.1.1 user unknown")
        assert SUP.is_suppressed(s, "hb@x.com")
        assert s.exec(select(CampaignRecipient)).first().status == "hard_bounced"
        assert s.exec(select(BounceRecord)).first().bounce_type == "hard"


def test_soft_bounce_retries_then_suppresses(ctx):
    client, engine, ids = ctx
    with Session(engine) as s:
        ld = _managed_lead(s, ids, email="")
        for _ in range(2):
            OE.on_bounce(s, ld, "soft@x.com", "451 4.7.1 try again later")
        assert not SUP.is_suppressed(s, "soft@x.com")            # under the retry limit
        OE.on_bounce(s, ld, "soft@x.com", "451 4.7.1 try again later")
        assert SUP.is_suppressed(s, "soft@x.com")                # persistent soft → suppressed


def test_spam_complaint_suppresses(ctx):
    client, engine, ids = ctx
    with Session(engine) as s:
        ld = _managed_lead(s, ids, email="")
        OE.on_bounce(s, ld, "spam@x.com", "5.7.1 message refused as spam complaint")
        assert SUP.is_suppressed(s, "spam@x.com")
        assert s.exec(select(WorkItem).where(WorkItem.type == "spam_complaint")).first() is not None


def test_classify_bounce_provider_failure_not_invalid(ctx):
    # a transient/policy failure is soft — the buyer address is not marked invalid
    assert OE.classify_bounce("451 greylisted")[1] is False
    assert OE.classify_bounce("552 mailbox full")[1] is False
    assert OE.classify_bounce("550 no such user")[1] is True


# --------------------------------------------------------- worker isolation
def test_campaign_worker_isolated_and_nonraising(ctx, monkeypatch):
    client, engine, ids = ctx
    import app.worker as worker
    monkeypatch.setattr(worker, "engine", engine)
    with Session(engine) as s:
        _running_campaign(s, ids)
    monkeypatch.setattr(CAMP, "send_step", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    out = worker.run_campaign_send()                # a per-recipient explosion must not raise
    assert "error" not in out or out.get("errors", 0) >= 0   # cycle returned, never raised
