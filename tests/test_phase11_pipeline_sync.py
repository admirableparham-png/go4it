"""Phase 11 — the seller's anonymized funnel follows real events: a campaign email moves a managed buyer to
'contacted', a real reply to 'responded'. Forward-only, managed buyers only, never undoes a recorded send."""
from datetime import datetime

import pytest
from sqlalchemy import text
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine, select

from app import campaign_service as CAMP
from app import outreach_events as OE
from app import pipeline
from app.models import (AuditLog, Campaign, CampaignRecipient, Lead, MailAccount, Outreach, StageEvent, User)

NOW = datetime(2026, 10, 5, 10, 0)


@pytest.fixture
def ctx():
    e = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    SQLModel.metadata.create_all(e)
    with e.connect() as c:
        for ddl in ("CREATE UNIQUE INDEX IF NOT EXISTS uq_workitem_idem_open ON workitem(idempotency_key) "
                    "WHERE idempotency_key != '' AND status IN ('open','in_progress','waiting')",
                    "CREATE UNIQUE INDEX IF NOT EXISTS uq_campaignsend_crvs ON "
                    "campaignsend(campaign_id,recipient_id,sequence_version,step_index)"):
            c.execute(text(ddl))
        c.commit()
    with Session(e) as s:
        s.add(User(email="admin@t.local", name="Admin", role="admin", active=True, password_hash="x"))
        s.add(User(email="seller@t.local", name="Seller One", role="agent", active=True, password_hash="x"))
        s.commit()
        ids = {u.email.split("@")[0]: u.id for u in s.exec(select(User)).all()}
        mb = MailAccount(user_id=ids["admin"], email="info@qmat.example", admin_owned=True, active=True,
                         sender_company="Qmat Trading LLC", postal_address="Dubai, UAE")
        s.add(mb); s.commit(); s.refresh(mb)
        c = Campaign(name="C", tenant_id=ids["seller"], owner_id=ids["admin"], mailbox_id=mb.id, status="draft")
        s.add(c); s.commit(); s.refresh(c)
        CAMP.set_sequence(s, c, [{"subject": "Offer", "body": "Hello {company}."},
                                 {"subject": "Following up", "body": "Any interest, {company}?", "delay_days": 0}])
        c.status = "running"; s.add(c); s.commit()
        ids.update(mailbox=mb.id, campaign=c.id)
    return e, ids


def _buyer(s, ids, stage="identified", status="new", managed=True):
    ld = Lead(product="Anchors", managed=managed, seller_id=ids["seller"], buyer_company="Acme", dest_country="PL",
              email=f"{stage}-{status}-{managed}@acme.example", pipeline_stage=stage, status=status)
    s.add(ld); s.commit(); s.refresh(ld)
    r = CampaignRecipient(campaign_id=ids["campaign"], tenant_id=ids["seller"], lead_id=ld.id, to_email=ld.email,
                          sequence_version=1, current_step=0, status="pending")
    s.add(r); s.commit(); s.refresh(r)
    return ld, r


def _ok(mb, to, subject, text, message_id="", **kw):
    return True, "", message_id


def _send(s, ids, r):
    return CAMP.send_step(s, s.get(Campaign, ids["campaign"]), s.get(CampaignRecipient, r.id),
                          s.get(MailAccount, ids["mailbox"]), NOW, _ok)


@pytest.mark.parametrize("start", ["identified", "verified"])
def test_campaign_send_moves_buyer_to_contacted_with_history(ctx, start):
    e, ids = ctx
    with Session(e) as s:
        ld, r = _buyer(s, ids, stage=start)
        assert _send(s, ids, r)["status"] == "sent"
        assert s.get(Lead, ld.id).pipeline_stage == "contacted"
        ev = s.exec(select(StageEvent).where(StageEvent.lead_id == ld.id)).one()
        assert (ev.from_stage, ev.to_stage) == (start, "contacted")
        assert s.exec(select(AuditLog).where(AuditLog.entity_id == ld.id, AuditLog.action == "stage_change")).first()


def test_follow_up_and_later_stages_are_never_regressed(ctx):
    e, ids = ctx
    with Session(e) as s:
        ld, r = _buyer(s, ids)
        _send(s, ids, r)                                     # → contacted
        _send(s, ids, r)                                     # follow-up: no second event
        assert len(s.exec(select(StageEvent).where(StageEvent.lead_id == ld.id)).all()) == 1
        for stage, status in (("negotiating", "negotiating"), ("lost", "lost"), ("responded", "new")):
            other, r2 = _buyer(s, ids, stage=stage, status=status)
            _send(s, ids, r2)
            assert s.get(Lead, other.id).pipeline_stage == stage
        plain, r3 = _buyer(s, ids, managed=False)
        _send(s, ids, r3)
        assert s.get(Lead, plain.id).pipeline_stage == "identified"   # unmanaged leads are not in a funnel


def test_a_failing_sync_never_undoes_the_send(ctx, monkeypatch):
    e, ids = ctx

    def boom(*a, **k):
        raise RuntimeError("sync broke")
    monkeypatch.setattr(pipeline, "advance_stage", boom)
    with Session(e) as s:
        ld, r = _buyer(s, ids)
        assert _send(s, ids, r)["status"] == "sent"
        assert len(s.exec(select(Outreach)).all()) == 1
        assert s.get(CampaignRecipient, r.id).status == "sent"


def test_reply_kinds(ctx):
    e, ids = ctx
    with Session(e) as s:
        human, r1 = _buyer(s, ids)
        auto, r2 = _buyer(s, ids, stage="verified")
        unsub, r3 = _buyer(s, ids, stage="identified", status="new", managed=True)
        for r in (r1, r2, r3):
            _send(s, ids, r)
        OE.on_reply(s, s.get(Lead, human.id), "Re: Offer", "Interested, send prices.")
        OE.on_reply(s, s.get(Lead, auto.id), "Automatic reply: Out of office", "I am out of the office.")
        OE.on_reply(s, s.get(Lead, unsub.id), "Re: Offer", "Please unsubscribe me.")
        assert s.get(Lead, human.id).pipeline_stage == "responded"
        assert s.get(Lead, auto.id).pipeline_stage == "contacted"
        assert s.get(Lead, unsub.id).pipeline_stage == "contacted"


def test_reply_without_any_email_from_us_does_not_advance(ctx):
    e, ids = ctx
    with Session(e) as s:
        ld, _r = _buyer(s, ids)
        OE.on_reply(s, s.get(Lead, ld.id), "Hello", "We saw your listing.")
        assert s.get(Lead, ld.id).pipeline_stage == "identified"
