"""Phase 11 — scripts/campaign_dryrun.py: every remaining message is rendered with the sender's own function and
checked; a clean campaign passes, each problem is caught on its own, nothing is written, the network is locked,
and the dry-run render is byte-identical to what send_step hands the SMTP layer."""
import smtplib
import socket

import pytest
from sqlalchemy import text
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine, func, select

from app import campaign_service as CAMP
from app import config
from app import outreach as OUT
from app import suppression as SUP
from app.models import (AuditLog, Campaign, CampaignRecipient, CampaignSend, Lead, MailAccount, Outreach,
                        ServiceRequest, User, UserProfile)
from scripts import campaign_dryrun as DRY


@pytest.fixture
def ctx(monkeypatch):
    e = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    SQLModel.metadata.create_all(e)
    with e.connect() as c:
        c.execute(text("CREATE UNIQUE INDEX IF NOT EXISTS uq_workitem_idem_open ON workitem(idempotency_key) "
                       "WHERE idempotency_key != '' AND status IN ('open','in_progress','waiting')"))
        c.commit()
    monkeypatch.setattr(config, "IMAP_ENABLED", True)
    monkeypatch.setattr(config, "IMAP_INTERVAL", 120)
    monkeypatch.setattr(config, "IMAP_USER", "info@qmat.example")
    monkeypatch.setattr(OUT, "mail_decrypt", lambda enc: "app-password" if enc else "")
    with Session(e) as s:
        s.add(User(email="founder@t", name="Founder", role="admin", active=True, password_hash="x"))
        s.add(User(email="sharks@t", name="Sharkline", role="agent", active=True, password_hash="x"))
        s.commit()
        ids = {u.email.split("@")[0]: u.id for u in s.exec(select(User)).all()}
        s.add(UserProfile(user_id=ids["sharks"], account_class="seller", role_key="seller", company="TRSHARKS"))
        mb = MailAccount(user_id=ids["founder"], email="info@qmat.example", from_name="Qmat Trading",
                         admin_owned=True, active=True, smtp_password_enc="enc", daily_limit=50,
                         sender_company="Qmat Trading LLC", postal_address="Dubai, UAE")
        s.add(mb); s.commit(); s.refresh(mb)
        sr = ServiceRequest(request_type="buyer_hunt", product="Anchors", status="done", owner_id=ids["sharks"],
                            requester_id=ids["sharks"])
        s.add(sr); s.commit(); s.refresh(sr)
        c = Campaign(name="TR", tenant_id=ids["sharks"], request_id=sr.id, owner_id=ids["founder"], mailbox_id=mb.id,
                     status="draft", daily_limit=10)
        s.add(c); s.commit(); s.refresh(c)
        CAMP.set_sequence(s, c, [{"subject": "Anchors for {company}", "body": "Hello {company} in {country}."},
                                 {"subject": "Following up", "body": "Any interest?", "delay_days": 3}])
        c.status = "running"; s.add(c); s.commit()
        for i, iso in enumerate(("PL", "IT", "CA")):
            ld = Lead(product="Anchors", managed=True, seller_id=ids["sharks"], buyer_company=f"Buyer {i}",
                      dest_country=iso, email=f"b{i}@x.example", request_id=sr.id)
            s.add(ld); s.commit(); s.refresh(ld)
            s.add(CampaignRecipient(campaign_id=c.id, tenant_id=ids["sharks"], lead_id=ld.id, to_email=ld.email))
        s.commit()
        ids.update(campaign=c.id, mailbox=mb.id)
    return e, ids


def _check(e, ids, **kw):
    with Session(e, autoflush=False) as s:
        return DRY.check(s, s.get(Campaign, ids["campaign"]), **kw)


def test_clean_campaign_passes(ctx):
    e, ids = ctx
    rep = _check(e, ids, samples=2)
    assert rep["errors"] == {}
    assert rep["recipients"] == 3 and rep["messages"] == 6 and rep["per_step"] == {1: 3, 2: 3}
    assert rep["days_for_first_email"] == 1 and len(rep["samples"]) == 2


@pytest.mark.parametrize("breakit,expect", [
    (lambda s, ids: SUP.suppress(s, "b0@x.example", "unsubscribe"), "do-not-contact"),
    (lambda s, ids: _set(s, Lead, "b1@x.example", email="b0@x.example", to="b0@x.example"), "duplicate"),
    (lambda s, ids: _set(s, Lead, "b2@x.example", email="not-an-email", to="not-an-email"), "invalid email"),
    (lambda s, ids: _set(s, Lead, "b2@x.example", owner_id=ids["founder"]), "confidential managed"),
    (lambda s, ids: _mb(s, ids, postal_address=""), "postal address"),
    (lambda s, ids: _mb(s, ids, from_name="go4it desk"), "internal platform"),
    (lambda s, ids: _set(s, Lead, "b0@x.example", buyer_company="TRSHARKS Poland"), "names the seller"),
])
def test_each_problem_is_caught(ctx, breakit, expect):
    e, ids = ctx
    with Session(e) as s:
        breakit(s, ids)
        s.commit()
    rep = _check(e, ids)
    assert any(expect in k for k in rep["errors"]), rep["errors"]


def test_excluded_country_and_quoted_reply_misread(ctx):
    e, ids = ctx
    assert any("excluded country CA" in k for k in _check(e, ids, exclude_countries=["CA"])["errors"])
    with Session(e) as s:
        st = CAMP.steps_for(s, s.get(Campaign, ids["campaign"]))[1]
        st.subject = "Please opt out if not relevant"; s.add(st); s.commit()
    assert any("misread" in k for k in _check(e, ids)["errors"])


def test_check_writes_nothing(ctx):
    e, ids = ctx

    def counts():
        with Session(e) as s:
            return [s.exec(select(func.count()).select_from(m)).one()
                    for m in (Outreach, CampaignSend, AuditLog, CampaignRecipient)]
    before = counts()
    with Session(e, autoflush=False) as s:
        DRY.check(s, s.get(Campaign, ids["campaign"]), samples=3)
        assert not s.new and not s.dirty and not s.deleted
    assert counts() == before


def test_network_lock(monkeypatch):
    DRY.lock_network(patch=monkeypatch.setattr)
    with pytest.raises(RuntimeError):
        smtplib.SMTP("smtp.gmail.com", 587)
    with pytest.raises(RuntimeError):
        socket.create_connection(("example.com", 443))
    assert DRY.NET_ATTEMPTS


def test_dry_run_render_is_byte_identical_to_what_is_sent(ctx):
    e, ids = ctx
    captured = {}

    def fake_sender(mb, to, subject, text, html=None, headers=None, **kw):
        captured.update(subject=subject, text=text, html=html, headers=headers)
        return True, "", kw.get("message_id", "")
    with Session(e, autoflush=False) as s:
        rep = DRY.check(s, s.get(Campaign, ids["campaign"]), samples=6)
    first = next(m for lid, n, m in rep["samples"] if n == 1 and "Buyer 0" in m["subject"])
    with Session(e) as s:
        r = s.exec(select(CampaignRecipient).where(CampaignRecipient.to_email == "b0@x.example")).one()
        c = s.get(Campaign, ids["campaign"])
        c.send_days, c.send_window_start, c.send_window_end = "0,1,2,3,4,5,6", 0, 24
        s.add(c); s.commit()
        out = CAMP.send_step(s, c, r, s.get(MailAccount, ids["mailbox"]), sender=fake_sender)
        assert out["status"] == "sent", out
    assert (captured["subject"], captured["text"], captured["html"], captured["headers"]) == \
        (first["subject"], first["text"], first["html"], first["headers"])


def _set(s, model, match, to=None, **fields):
    ld = s.exec(select(Lead).where(Lead.email == match)).one()
    for k, v in fields.items():
        setattr(ld, k, v)
    s.add(ld)
    if to is not None:
        r = s.exec(select(CampaignRecipient).where(CampaignRecipient.lead_id == ld.id)).one()
        r.to_email = to
        s.add(r)


def _mb(s, ids, **fields):
    mb = s.get(MailAccount, ids["mailbox"])
    for k, v in fields.items():
        setattr(mb, k, v)
    s.add(mb)
