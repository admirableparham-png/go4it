"""Phase 11 — mailbox-level failures are the MAILBOX's, not the buyer's: an auth / quota / config failure refunds the
daily slot, puts the send back untouched, pauses the mailbox and raises an urgent task; the worker stops that mailbox
at once. A buyer-level failure still ends that buyer only. Stuck recipients settle so campaigns finish, and a bad
list trips the bounce breaker."""
from datetime import datetime

import pytest
from sqlalchemy import text
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine, select

import app.worker as worker
from app import campaign_service as CAMP
from app import suppression as SUP
from app.models import (Campaign, CampaignRecipient, CampaignSend, Lead, MailAccount, Outreach, User, WorkItem)

NOW = datetime(2026, 10, 5, 10, 0)          # a Monday, inside the 8-18 window


@pytest.fixture
def ctx(monkeypatch):
    e = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    SQLModel.metadata.create_all(e)
    with e.connect() as c:
        for ddl in ("CREATE UNIQUE INDEX IF NOT EXISTS uq_workitem_idem_open ON workitem(idempotency_key) "
                    "WHERE idempotency_key != '' AND status IN ('open','in_progress','waiting')",
                    "CREATE UNIQUE INDEX IF NOT EXISTS uq_outreach_campaign_send ON outreach(campaign_id,"
                    "campaign_recipient_id,campaign_version,campaign_step) WHERE campaign_id IS NOT NULL",
                    "CREATE UNIQUE INDEX IF NOT EXISTS uq_campaignsend_crvs ON "
                    "campaignsend(campaign_id,recipient_id,sequence_version,step_index)"):
            c.execute(text(ddl))
        c.commit()
    monkeypatch.setattr(worker, "engine", e)
    with Session(e) as s:
        s.add(User(email="admin@t.local", name="Admin", role="admin", active=True, password_hash="x"))
        s.add(User(email="seller@t.local", name="Seller One", role="agent", active=True, password_hash="x"))
        s.commit()
        ids = {u.email.split("@")[0]: u.id for u in s.exec(select(User)).all()}
        mb = MailAccount(user_id=ids["admin"], email="info@qmat.example", admin_owned=True, active=True,
                         daily_limit=100, sender_company="Qmat Trading LLC", postal_address="Dubai, UAE")
        s.add(mb); s.commit(); s.refresh(mb)
        ids["mailbox"] = mb.id
        c = Campaign(name="C", tenant_id=ids["seller"], owner_id=ids["admin"], mailbox_id=mb.id, status="draft",
                     daily_limit=100)
        s.add(c); s.commit(); s.refresh(c)
        CAMP.set_sequence(s, c, [{"subject": "Offer for {company}", "body": "Hello {company}."}], None)
        c.status = "running"; s.add(c); s.commit()
        ids["campaign"] = c.id
    return e, ids


def _add(s, ids, n, prefix="b"):
    out = []
    for i in range(n):
        ld = Lead(product="Anchors", managed=True, seller_id=ids["seller"], buyer_company=f"Buyer {prefix}{i}",
                  dest_country="PL", email=f"{prefix}{i}@x.example")
        s.add(ld); s.commit(); s.refresh(ld)
        r = CampaignRecipient(campaign_id=ids["campaign"], tenant_id=ids["seller"], lead_id=ld.id, to_email=ld.email,
                              sequence_version=1, current_step=0, status="pending")
        s.add(r); s.commit(); s.refresh(r)
        out.append(r)
    return out


def _failing(err):
    calls = []

    def sender(mb, to, subject, text, **kw):
        calls.append(to)
        return False, err, ""
    return sender, calls


GMAIL_535 = "(535, b'5.7.8 Username and Password not accepted. For more information, go to 5.7.8 BadCredentials')"


@pytest.mark.parametrize("err,kind", [
    (GMAIL_535, "auth"),
    ("(534, b'5.7.9 Application-specific password required')", "auth"),
    ("(550, b'5.4.5 Daily user sending quota exceeded.')", "quota"),
])
def test_mailbox_failure_pauses_mailbox_refunds_slot_and_keeps_the_send(ctx, err, kind):
    e, ids = ctx
    with Session(e) as s:
        (r,) = _add(s, ids, 1)
        sender, _calls = _failing(err)
        out = CAMP.send_step(s, s.get(Campaign, ids["campaign"]), r, s.get(MailAccount, ids["mailbox"]), NOW, sender)
        assert out["status"] == "mailbox_failed" and out["kind"] == kind
        mb = s.get(MailAccount, ids["mailbox"])
        assert mb.paused and mb.last_send_error.startswith(kind)
        assert mb.sent_today == 0                                        # slot refunded
        cs = s.exec(select(CampaignSend)).one()
        assert cs.status == "retryable" and cs.attempt_count == 0        # not the buyer's fault
        assert s.get(CampaignRecipient, r.id).status == "pending"
        assert s.exec(select(Outreach)).all() == []
        key = f"mailbox_auth_failure:{mb.id}" if kind == "auth" else f"mailbox_paused:{mb.id}"
        assert s.exec(select(WorkItem).where(WorkItem.idempotency_key == key)).first()


def test_worker_stops_the_mailbox_after_the_first_auth_failure_and_resends_same_id(ctx, monkeypatch):
    e, ids = ctx
    with Session(e) as s:
        _add(s, ids, 5)
    sender, calls = _failing(GMAIL_535)
    monkeypatch.setattr(CAMP, "_default_sender", sender)
    worker.run_campaign_send(now=NOW)
    assert len(calls) == 1                                              # stopped at once, not 5 failures
    with Session(e) as s:
        first_id = s.exec(select(CampaignSend)).one().rfc_message_id
        mb = s.get(MailAccount, ids["mailbox"]); mb.paused = False; mb.last_send_error = ""; s.add(mb); s.commit()
    sent_ids = []

    def ok(mb, to, subject, text, message_id="", **kw):
        sent_ids.append(message_id)
        return True, "", message_id
    monkeypatch.setattr(CAMP, "_default_sender", ok)
    worker.run_campaign_send(now=NOW)
    assert sent_ids[0] == first_id and len(sent_ids) == 5                # recovered send keeps its Message-ID


def test_a_sender_that_raises_never_leaves_a_row_sending(ctx):
    e, ids = ctx
    with Session(e) as s:
        (r,) = _add(s, ids, 1)

        def boom(*a, **k):
            raise RuntimeError("sender error: key mismatch")
        out = CAMP.send_step(s, s.get(Campaign, ids["campaign"]), r, s.get(MailAccount, ids["mailbox"]), NOW, boom)
        assert out["status"] == "mailbox_failed" and out["kind"] == "config"
        assert s.exec(select(CampaignSend).where(CampaignSend.status.in_(("sending", "unknown_needs_review")))).all() == []


def test_bad_recipient_ends_that_buyer_only(ctx):
    e, ids = ctx
    with Session(e) as s:
        (r,) = _add(s, ids, 1)
        sender, _ = _failing("(550, b'5.1.1 The email account that you tried to reach does not exist')")
        out = CAMP.send_step(s, s.get(Campaign, ids["campaign"]), r, s.get(MailAccount, ids["mailbox"]), NOW, sender)
        assert out["status"] == "permanently_failed"
        assert s.get(CampaignRecipient, r.id).status == "skipped"
        assert not s.get(MailAccount, ids["mailbox"]).paused


def test_suppressed_after_enrolment_settles_so_the_campaign_completes(ctx, monkeypatch):
    e, ids = ctx
    with Session(e) as s:
        rs = _add(s, ids, 2)
        SUP.suppress(s, rs[0].to_email, "unsubscribe")
        s.commit()
        r0, r1 = rs[0].id, rs[1].id
    monkeypatch.setattr(CAMP, "_default_sender", lambda mb, to, subject, text, message_id="", **kw: (True, "", message_id))
    worker.run_campaign_send(now=NOW)
    with Session(e) as s:
        assert s.get(CampaignRecipient, r0).status == "suppressed"
        assert s.get(CampaignRecipient, r1).status == "completed"
        assert s.get(Campaign, ids["campaign"]).status == "completed"


def test_daily_limit_stops_the_campaign_loop_not_every_recipient(ctx, monkeypatch):
    e, ids = ctx
    with Session(e) as s:
        c = s.get(Campaign, ids["campaign"]); c.daily_limit = 2; s.add(c); s.commit()
        _add(s, ids, 6)
    calls = []

    def ok(mb, to, subject, text, message_id="", **kw):
        calls.append(to)
        return True, "", message_id
    monkeypatch.setattr(CAMP, "_default_sender", ok)
    out = worker.run_campaign_send(now=NOW)
    assert len(calls) == 2 and out["skipped"] == 1                      # one campaign-level skip, then break


def test_bounce_breaker_pauses_a_bad_list(ctx):
    e, ids = ctx
    with Session(e) as s:
        rs = _add(s, ids, 20)
        for r in rs[:2]:
            r.status = "hard_bounced"; s.add(r)
        for r in rs[2:]:
            r.status = "sent"; s.add(r)
        s.commit()
        c = s.get(Campaign, ids["campaign"])
        assert CAMP.bounce_breaker(s, c)                                  # 2/20 = 10% ≥ 8%
        assert s.get(Campaign, ids["campaign"]).status == "paused"
        assert s.exec(select(WorkItem).where(WorkItem.idempotency_key == f"campaign_paused:{c.id}")).first()


def test_bounce_breaker_needs_volume(ctx):
    e, ids = ctx
    with Session(e) as s:
        rs = _add(s, ids, 5)
        rs[0].status = "hard_bounced"; s.add(rs[0]); s.commit()
        assert not CAMP.bounce_breaker(s, s.get(Campaign, ids["campaign"]))
