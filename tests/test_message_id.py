"""Phase 4 production-gate — durable RFC Message-ID lifecycle + reply correlation.

A globally-unique RFC Message-ID is generated and PERSISTED before SMTP submission, reused verbatim on a safe
retry (never regenerated for the same step), survives a worker restart, and is the key inbound replies match
on via In-Reply-To / References. The provider's own id is stored separately. An uncertain handoff is never
regenerated or resent.
"""
from datetime import datetime, timedelta

import pytest
from sqlalchemy import text
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine, select

from app import campaign_service as CAMP
from app.inbound_email import handle_inbound
from app.models import (Campaign, CampaignRecipient, CampaignSend, Lead, MailAccount, Outreach, User)


def _indexes(engine):
    with engine.connect() as conn:
        for ddl in (
            "CREATE UNIQUE INDEX IF NOT EXISTS uq_outreach_campaign_send ON "
            "outreach(campaign_id,campaign_recipient_id,campaign_version,campaign_step) WHERE campaign_id IS NOT NULL",
            "CREATE UNIQUE INDEX IF NOT EXISTS uq_campaignsend_crvs ON "
            "campaignsend(campaign_id,recipient_id,sequence_version,step_index)",
            "CREATE UNIQUE INDEX IF NOT EXISTS uq_workitem_idem_open ON workitem(idempotency_key) "
            "WHERE idempotency_key != '' AND status IN ('open','in_progress','waiting')",
        ):
            conn.execute(text(ddl))
        conn.commit()


@pytest.fixture
def ctx():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    SQLModel.metadata.create_all(engine)
    _indexes(engine)
    with Session(engine) as s:
        s.add(User(email="admin@t.local", name="A", role="admin", active=True, password_hash="x"))
        s.add(User(email="kim@t.local", name="K", role="agent", active=True, password_hash="x"))
        s.commit()
        ids = {u.email.split("@")[0]: u.id for u in s.exec(select(User)).all()}
        mb = MailAccount(user_id=ids["admin"], email="hunt@sender.example", admin_owned=True, active=True,
                         daily_limit=100, sender_company="Sender Trading LLC", postal_address="1 Test Street, Dubai")
        s.add(mb); s.commit(); s.refresh(mb)
        ids["mailbox"] = mb.id
    return engine, ids


def _campaign(s, ids):
    c = Campaign(name="C", tenant_id=ids["kim"], owner_id=ids["admin"], mailbox_id=ids["mailbox"],
                 status="draft", sequence_version=1, daily_limit=100, send_days="0,1,2,3,4,5,6",
                 send_window_start=0, send_window_end=24)
    s.add(c); s.commit(); s.refresh(c)
    CAMP.set_sequence(s, c, [{"subject": "Hi", "body": "hello", "delay_days": 0}], None)
    c.status = "running"; s.add(c); s.commit(); s.refresh(c)
    return c


def _recipient(s, c, ids, email="b@x.com"):
    ld = Lead(product="copper", managed=True, seller_id=ids["kim"], email=email)
    s.add(ld); s.commit(); s.refresh(ld)
    r = CampaignRecipient(campaign_id=c.id, tenant_id=ids["kim"], lead_id=ld.id, to_email=email,
                          sequence_version=1, current_step=0, status="pending")
    s.add(r); s.commit(); s.refresh(r)
    return r


def _ok(mb, to, subject, text, html=None, reply_to="", in_reply_to="", message_id="", references="", **kw):
    return True, "", ""                       # SMTP: no distinct provider id


def _prov(mb, to, subject, text, html=None, reply_to="", in_reply_to="", message_id="", references="", **kw):
    return True, "", "<provider-xyz@relay>"   # provider returns its OWN id


def _fail(mb, to, subject, text, html=None, reply_to="", in_reply_to="", message_id="", references="", **kw):
    return False, "451 try again later", ""


# --- 1. Message-ID persisted BEFORE the provider is invoked ----------------------------------------
def test_message_id_persisted_before_provider_invocation(ctx):
    engine, ids = ctx
    captured = {}

    def spy(mb, to, subject, text, html=None, reply_to="", in_reply_to="", message_id="", references="", **kw):
        # at the moment SMTP would be invoked, the id is ALREADY persisted + the row is 'sending'
        with Session(engine) as s2:
            cs = s2.exec(select(CampaignSend)).one()
            captured["persisted"] = cs.rfc_message_id
            captured["status"] = cs.status
        captured["passed"] = message_id
        return True, "", ""

    with Session(engine) as s:
        c = _campaign(s, ids); r = _recipient(s, c, ids)
        CAMP.send_step(s, c, r, s.get(MailAccount, ids["mailbox"]), sender=spy)
    assert captured["passed"] and captured["passed"].startswith("<") and captured["passed"].endswith(">")
    assert captured["persisted"] == captured["passed"]     # persisted before the send call
    assert captured["status"] == "sending"                 # committed 'sending' before SMTP


# --- 2. retry reuses the SAME Message-ID (never regenerated) ----------------------------------------
def test_retry_reuses_same_message_id(ctx):
    engine, ids = ctx
    with Session(engine) as s:
        c = _campaign(s, ids); r = _recipient(s, c, ids)
        CAMP.send_step(s, c, r, s.get(MailAccount, ids["mailbox"]), sender=_fail)   # attempt 1 fails
        cs = s.exec(select(CampaignSend)).one()
        first_id = cs.rfc_message_id
        assert first_id
        cs.next_attempt_at = datetime.utcnow() - timedelta(seconds=1); s.add(cs); s.commit()
        seen = {}

        def spy(mb, to, subject, text, html=None, reply_to="", in_reply_to="", message_id="", references="", **kw):
            seen["id"] = message_id
            return True, "", ""

        CAMP.send_step(s, c, s.get(CampaignRecipient, r.id), s.get(MailAccount, ids["mailbox"]), sender=spy)
        cs = s.exec(select(CampaignSend)).one()
        assert seen["id"] == first_id and cs.rfc_message_id == first_id   # same id on retry


# --- 3. worker restart retains the Message-ID -------------------------------------------------------
def test_worker_restart_retains_message_id(ctx):
    engine, ids = ctx
    with Session(engine) as s:
        c = _campaign(s, ids); r = _recipient(s, c, ids)
        CAMP.send_step(s, c, r, s.get(MailAccount, ids["mailbox"]), sender=_fail)
        first_id = s.exec(select(CampaignSend)).one().rfc_message_id
    # "restart": brand-new Session on the same durable DB
    with Session(engine) as s2:
        assert s2.exec(select(CampaignSend)).one().rfc_message_id == first_id


# --- 4. provider id stored SEPARATELY from our RFC id ----------------------------------------------
def test_provider_id_stored_separately(ctx):
    engine, ids = ctx
    with Session(engine) as s:
        c = _campaign(s, ids); r = _recipient(s, c, ids)
        out = CAMP.send_step(s, c, r, s.get(MailAccount, ids["mailbox"]), sender=_prov)
        cs = s.exec(select(CampaignSend)).one()
        assert cs.rfc_message_id and cs.rfc_message_id != "<provider-xyz@relay>"
        assert cs.provider_message_id == "<provider-xyz@relay>"
        assert out["rfc_message_id"] == cs.rfc_message_id
        # the Outreach event carries OUR durable id (the header we actually sent) for reply correlation
        o = s.exec(select(Outreach).where(Outreach.direction == "out")).one()
        assert o.message_id == cs.rfc_message_id


# --- 5. reply matching through In-Reply-To ----------------------------------------------------------
def test_reply_matches_via_in_reply_to(ctx):
    engine, ids = ctx
    with Session(engine) as s:
        c = _campaign(s, ids); r = _recipient(s, c, ids)
        CAMP.send_step(s, c, r, s.get(MailAccount, ids["mailbox"]), sender=_ok)
        sent_id = s.exec(select(CampaignSend)).one().rfc_message_id
        res = handle_inbound(s, "someone-else@buyer.com", "Re: Hi", "yes please",
                             message_id="<reply1@buyer.com>", in_reply_to=sent_id)
        assert res == "threaded"
        assert s.exec(select(Outreach).where(Outreach.direction == "in")).one().lead_id == r.lead_id


# --- 6. reply matching through References (In-Reply-To absent) --------------------------------------
def test_reply_matches_via_references(ctx):
    engine, ids = ctx
    with Session(engine) as s:
        c = _campaign(s, ids); r = _recipient(s, c, ids)
        CAMP.send_step(s, c, r, s.get(MailAccount, ids["mailbox"]), sender=_ok)
        sent_id = s.exec(select(CampaignSend)).one().rfc_message_id
        res = handle_inbound(s, "x@buyer.com", "Re: Hi", "ok", message_id="<reply2@buyer.com>",
                             in_reply_to="", references=f"<root@buyer.com> {sent_id}")
        assert res == "threaded"


# --- 7. malformed / unknown Message-ID → no match, no crash ----------------------------------------
def test_malformed_or_unknown_message_id_no_match(ctx):
    engine, ids = ctx
    with Session(engine) as s:
        _campaign(s, ids)
        assert handle_inbound(s, "ghost@nowhere.com", "hi", "b",
                              in_reply_to="not-a-msgid", references="garbage") == "unmatched"
        assert handle_inbound(s, "ghost@nowhere.com", "hi", "b",
                              in_reply_to="<never-sent@x>") == "unmatched"


# --- 8. no cross-thread match (unique id → only its own thread) ------------------------------------
def test_no_cross_thread_match(ctx):
    engine, ids = ctx
    with Session(engine) as s:
        c = _campaign(s, ids)
        r1 = _recipient(s, c, ids, email="one@x.com")
        r2 = _recipient(s, c, ids, email="two@x.com")
        CAMP.send_step(s, c, r1, s.get(MailAccount, ids["mailbox"]), sender=_ok)
        CAMP.send_step(s, c, r2, s.get(MailAccount, ids["mailbox"]), sender=_ok)
        id2 = s.exec(select(CampaignSend).where(CampaignSend.recipient_id == r2.id)).one().rfc_message_id
        handle_inbound(s, "reply@x.com", "Re", "hi", message_id="<rc@x>", in_reply_to=id2)
        inbound = s.exec(select(Outreach).where(Outreach.direction == "in")).one()
        assert inbound.lead_id == r2.lead_id and inbound.lead_id != r1.lead_id   # only r2's thread


# --- 9. uncertain handoff does not regenerate or resend --------------------------------------------
def test_uncertain_handoff_not_regenerated_or_resent(ctx):
    engine, ids = ctx
    with Session(engine) as s:
        c = _campaign(s, ids); r = _recipient(s, c, ids)
        cs = CampaignSend(campaign_id=c.id, recipient_id=r.id, sequence_version=1, step_index=0,
                          status="sending", claim_token="t", rfc_message_id="<keep-me@go4it.vip>",
                          claimed_at=datetime.utcnow(), lease_expires_at=datetime.utcnow() - timedelta(seconds=1))
        s.add(cs); s.commit(); s.refresh(cs)
        CAMP.recover_stale_sends(s)
        cs = s.get(CampaignSend, cs.id)
        assert cs.status == "unknown_needs_review"
        assert cs.rfc_message_id == "<keep-me@go4it.vip>"      # NOT regenerated
        assert CAMP.claim_send(s, c, r, 0) is None             # NOT resent
        assert s.exec(select(Outreach)).first() is None        # no message recorded
