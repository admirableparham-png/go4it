"""Phase 4 hardening — crash-safe campaign send lifecycle.

Every send is a durable CampaignSend row claimed with a time-limited lease BEFORE SMTP; the Outreach event and
'sent' state are written only AFTER the provider accepts. These tests exercise the crash window, concurrency,
stale-lease recovery, the retry policy, and the honest at-least-once boundary (an ambiguous crash-during-send
is flagged for review, never auto-resent). Honest limitation: SMTP gives no true exactly-once — we guarantee
no AUTOMATIC duplicate, not a mathematical one.
"""
from datetime import datetime, timedelta

import pytest
from sqlalchemy import text
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine, select

from app import campaign_service as CAMP
from app.models import (Campaign, CampaignRecipient, CampaignSend, Lead, MailAccount, Outreach, User, WorkItem)


def _indexes(engine):
    with engine.connect() as conn:
        for ddl in (
            "CREATE UNIQUE INDEX IF NOT EXISTS uq_workitem_idem_open ON workitem(idempotency_key) "
            "WHERE idempotency_key != '' AND status IN ('open','in_progress','waiting')",
            "CREATE UNIQUE INDEX IF NOT EXISTS uq_outreach_campaign_send ON "
            "outreach(campaign_id,campaign_recipient_id,campaign_version,campaign_step) WHERE campaign_id IS NOT NULL",
            "CREATE UNIQUE INDEX IF NOT EXISTS uq_campaignsend_crvs ON "
            "campaignsend(campaign_id,recipient_id,sequence_version,step_index)",
        ):
            conn.execute(text(ddl))
        conn.commit()


@pytest.fixture
def ctx():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    SQLModel.metadata.create_all(engine)
    _indexes(engine)
    with Session(engine) as s:
        s.add(User(email="admin@t.local", name="Admin", role="admin", active=True, password_hash="x"))
        s.add(User(email="kim@t.local", name="Kim", role="agent", active=True, password_hash="x"))
        s.commit()
        ids = {u.email.split("@")[0]: u.id for u in s.exec(select(User)).all()}
        mb = MailAccount(user_id=ids["admin"], email="hunt@go4it.vip", admin_owned=True, active=True,
                         daily_limit=100)
        s.add(mb); s.commit(); s.refresh(mb)
        ids["mailbox"] = mb.id
    return engine, ids


def _campaign(s, ids, steps=1):
    c = Campaign(name="C", tenant_id=ids["kim"], owner_id=ids["admin"], mailbox_id=ids["mailbox"],
                 status="draft", sequence_version=1, daily_limit=100, send_days="0,1,2,3,4,5,6",
                 send_window_start=0, send_window_end=24)
    s.add(c); s.commit(); s.refresh(c)
    seq = [{"subject": f"S{i}", "body": f"hello {i}", "delay_days": 0} for i in range(steps)]
    CAMP.set_sequence(s, c, seq, None)
    c.status = "running"; s.add(c); s.commit(); s.refresh(c)
    return c


def _recipient(s, c, ids, email="b@x.com"):
    ld = Lead(product="copper", managed=True, seller_id=ids["kim"], email=email)
    s.add(ld); s.commit(); s.refresh(ld)
    r = CampaignRecipient(campaign_id=c.id, tenant_id=ids["kim"], lead_id=ld.id, to_email=email,
                          sequence_version=1, current_step=0, status="pending")
    s.add(r); s.commit(); s.refresh(r)
    return r


def _mb(s, ids):
    return s.get(MailAccount, ids["mailbox"])


# --- senders ---------------------------------------------------------------------------------------
def ok(mb, to, subject, text, html=None, reply_to="", in_reply_to="", message_id="", references=""):
    return True, "", f"<mid-{to}>"


def timeout(mb, to, subject, text, html=None, reply_to="", in_reply_to="", message_id="", references=""):
    return False, "connection timed out", ""


def retryable(mb, to, subject, text, html=None, reply_to="", in_reply_to="", message_id="", references=""):
    return False, "451 4.7.1 greylisted, try again later", ""


def permanent(mb, to, subject, text, html=None, reply_to="", in_reply_to="", message_id="", references=""):
    return False, "550 5.1.1 user unknown", ""


# --- 1. two workers, same send ---------------------------------------------------------------------
def test_two_workers_same_send_only_one_claims(ctx):
    engine, ids = ctx
    with Session(engine) as s:
        c = _campaign(s, ids); r = _recipient(s, c, ids)
        a = CAMP.claim_send(s, c, r, 0)                 # worker A
        b = CAMP.claim_send(s, c, r, 0)                 # worker B, same step, live lease
        assert a is not None and b is None
        assert s.exec(select(CampaignSend)).one().status == "claimed"


# --- 2 & 4. crash immediately after claim → stale lease reclaimed (never sent) ----------------------
def test_crash_after_claim_is_reclaimed_and_sends_once(ctx):
    engine, ids = ctx
    with Session(engine) as s:
        c = _campaign(s, ids); r = _recipient(s, c, ids)
        cs = CAMP.claim_send(s, c, r, 0)                # claimed, then the worker "crashes" (never sends)
        assert CAMP.claim_send(s, c, r, 0) is None      # live lease → nobody else can take it
        cs.lease_expires_at = datetime.utcnow() - timedelta(seconds=1); s.add(cs); s.commit()
        rec = CAMP.recover_stale_sends(s)
        assert rec == {"reclaimed": 1, "needs_review": 0}
        out = CAMP.send_step(s, c, s.get(CampaignRecipient, r.id), _mb(s, ids), sender=ok)
        assert out["status"] == "sent"
        assert len(s.exec(select(Outreach)).all()) == 1  # exactly one message ever


# --- 3. crash BEFORE provider acceptance → ambiguous, flagged, never auto-resent --------------------
def test_crash_mid_send_flagged_not_resent(ctx):
    engine, ids = ctx
    with Session(engine) as s:
        c = _campaign(s, ids); r = _recipient(s, c, ids)
        cs = CampaignSend(campaign_id=c.id, recipient_id=r.id, sequence_version=1, step_index=0,
                          status="sending", claim_token="t", claimed_at=datetime.utcnow(),
                          lease_expires_at=datetime.utcnow() - timedelta(seconds=1))
        s.add(cs); s.commit(); s.refresh(cs)
        rec = CAMP.recover_stale_sends(s)
        assert rec == {"reclaimed": 0, "needs_review": 1}
        assert s.get(CampaignSend, cs.id).status == "unknown_needs_review"
        assert s.exec(select(WorkItem).where(WorkItem.type == "failed_system_job")).first() is not None
        assert CAMP.claim_send(s, c, r, 0) is None       # NOT auto-resent
        assert s.exec(select(Outreach)).first() is None   # no phantom 'sent' event


# --- 5. provider timeout → retryable, no Outreach, backoff scheduled --------------------------------
def test_provider_timeout_is_retryable(ctx):
    engine, ids = ctx
    with Session(engine) as s:
        c = _campaign(s, ids); r = _recipient(s, c, ids)
        out = CAMP.send_step(s, c, r, _mb(s, ids), sender=timeout)
        assert out["status"] == "retryable"
        cs = s.exec(select(CampaignSend)).one()
        assert cs.attempt_count == 1 and cs.next_attempt_at is not None
        assert s.exec(select(Outreach)).first() is None
        assert s.get(CampaignRecipient, r.id).current_step == 0   # not advanced on failure


# --- 6. retryable SMTP error ------------------------------------------------------------------------
def test_retryable_smtp_error(ctx):
    engine, ids = ctx
    with Session(engine) as s:
        c = _campaign(s, ids); r = _recipient(s, c, ids)
        out = CAMP.send_step(s, c, r, _mb(s, ids), sender=retryable)
        assert out["status"] == "retryable"
        assert CAMP.classify_send_error("451 4.7.1 greylisted") == "retryable"


# --- 7. permanent SMTP error → permanently_failed, no more attempts ---------------------------------
def test_permanent_smtp_error_stops(ctx):
    engine, ids = ctx
    with Session(engine) as s:
        c = _campaign(s, ids); r = _recipient(s, c, ids)
        out = CAMP.send_step(s, c, r, _mb(s, ids), sender=permanent)
        assert out["status"] == "permanently_failed"
        assert CAMP.classify_send_error("550 5.1.1 user unknown") == "permanent"
        assert CAMP.claim_send(s, c, r, 0) is None        # terminal, never retried
        assert s.exec(select(Outreach)).first() is None


# --- 8. successful retry after a transient failure --------------------------------------------------
def test_successful_retry(ctx):
    engine, ids = ctx
    with Session(engine) as s:
        c = _campaign(s, ids); r = _recipient(s, c, ids)
        out1 = CAMP.send_step(s, c, r, _mb(s, ids), sender=timeout)
        assert out1["status"] == "retryable"
        cs = s.exec(select(CampaignSend)).one()
        cs.next_attempt_at = datetime.utcnow() - timedelta(seconds=1); s.add(cs); s.commit()  # backoff elapsed
        out2 = CAMP.send_step(s, c, s.get(CampaignRecipient, r.id), _mb(s, ids), sender=ok)
        assert out2["status"] == "sent"
        cs = s.exec(select(CampaignSend)).one()
        assert cs.status == "sent" and cs.attempt_count == 2 and cs.provider_message_id
        assert len(s.exec(select(Outreach)).all()) == 1   # only the successful attempt is recorded


# --- 9. no duplicate send after a confirmed provider acceptance -------------------------------------
def test_no_duplicate_after_acceptance(ctx):
    engine, ids = ctx
    with Session(engine) as s:
        c = _campaign(s, ids); r = _recipient(s, c, ids)
        assert CAMP.send_step(s, c, r, _mb(s, ids), sender=ok)["status"] == "sent"
        # a buggy re-claim of the SAME step must NOT create a second message — the Outreach unique index is
        # the last-line guard even if the CampaignSend row were forced back to retryable.
        cs = s.exec(select(CampaignSend)).one()
        cs.status = "retryable"; cs.next_attempt_at = datetime.utcnow() - timedelta(seconds=1)
        cs.claim_token = ""; s.add(cs)
        rr = s.get(CampaignRecipient, r.id); rr.current_step = 0; rr.status = "sent"; s.add(rr); s.commit()
        out = CAMP.send_step(s, c, s.get(CampaignRecipient, r.id), _mb(s, ids), sender=ok)
        assert out["status"] == "sent"
        assert len(s.exec(select(Outreach)).all()) == 1   # still exactly one


# --- 10. worker keeps processing other recipients after one failure ---------------------------------
def test_worker_continues_after_one_failure(ctx, monkeypatch):
    engine, ids = ctx
    import app.worker as worker
    monkeypatch.setattr(worker, "engine", engine)
    with Session(engine) as s:
        c = _campaign(s, ids)
        for em in ("good1@x.com", "bad@x.com", "good2@x.com"):
            _recipient(s, c, ids, email=em)

    def picky(mb, to, subject, text, html=None, reply_to="", in_reply_to="", message_id="", references=""):
        if to == "bad@x.com":
            return False, "550 5.1.1 user unknown", ""
        return True, "", f"<mid-{to}>"

    monkeypatch.setattr(CAMP, "_default_sender", picky)
    out = worker.run_campaign_send()
    assert "error" not in out
    with Session(engine) as s:
        sent = {o.recipient for o in s.exec(select(Outreach)).all()}
        assert sent == {"good1@x.com", "good2@x.com"}                  # the two good ones delivered
        bad = s.exec(select(CampaignSend).join(CampaignRecipient).where(
            CampaignRecipient.to_email == "bad@x.com")).one()
        assert bad.status == "permanently_failed"                      # the bad one failed, isolated
