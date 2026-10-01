"""Phase 12 — worker ops: the automatic campaign warm-up ramp (one step per UTC day, before the first send, only after
a full clean sending day with reply reading working; holds raise ONE task; never down, never above the mailbox), the
daily Telegram ops summary (counts only, once per day across restarts), and backup hardening (owner-only single-file
snapshots, BACKUP_KEEP, no extra copy on restart). Nothing leaves the process: SMTP + Telegram are stubbed."""
import importlib
import os
import pathlib
import sqlite3
import stat
import sys
import types
from datetime import datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine as sa_create_engine, inspect, text
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine, func, select

import app.main as main
import app.worker as worker
from app import campaign_render as CR
from app import campaign_service as CAMP
from app import config
from app import ops_summary as OPS
from app import permissions as P
from app.auth import hash_password
from app.models import (AuditLog, BounceRecord, Campaign, CampaignRecipient, CampaignSend, IngestionRun, Lead,
                        MailAccount, Outreach, User, UserProfile, WorkItem)
from scripts import backup_db

ROOT = pathlib.Path(__file__).resolve().parents[1]
THU = datetime(2026, 10, 8, 8, 0)               # a Thursday, the window opens (Mon–Fri 08–18 UTC)
FRI, SAT, MON = THU + timedelta(days=1), THU + timedelta(days=2), THU + timedelta(days=4)
HELD = "campaign_warmup_held"


def _mk(s, email, role, account_class, role_key):
    u = User(email=email, name=email.split("@")[0], role=role, active=True, password_hash=hash_password("pw"))
    s.add(u); s.commit(); s.refresh(u)
    s.add(UserProfile(user_id=u.id, account_class=account_class, role_key=role_key,
                      scope=P.ROLE_TEMPLATES[role_key]["scope"], account_status="active"))
    s.commit()
    return u


@pytest.fixture
def ctx(monkeypatch, tmp_path):
    e = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    SQLModel.metadata.create_all(e)
    with e.connect() as c:
        for ddl in ("CREATE UNIQUE INDEX IF NOT EXISTS uq_workitem_idem_open ON workitem(idempotency_key) "
                    "WHERE idempotency_key != '' AND status IN ('open','in_progress','waiting')",
                    "CREATE UNIQUE INDEX IF NOT EXISTS uq_outreach_campaign_send ON outreach(campaign_id,"
                    "campaign_recipient_id,campaign_version,campaign_step) WHERE campaign_id IS NOT NULL",
                    "CREATE UNIQUE INDEX IF NOT EXISTS uq_campaignsend_crvs ON "
                    "campaignsend(campaign_id,recipient_id,sequence_version,step_index)",
                    "CREATE UNIQUE INDEX IF NOT EXISTS uq_userprofile_user ON userprofile(user_id) "
                    "WHERE user_id IS NOT NULL"):
            c.execute(text(ddl))
        c.commit()
    for mod in (main, worker):
        monkeypatch.setattr(mod, "engine", e)
    monkeypatch.setattr(worker, "CAMPAIGN_SEND_MAX_PER_RUN", 200)
    monkeypatch.setattr(worker, "CAMPAIGN_SEND_DEADLINE_SEC", 10_000)
    monkeypatch.setattr(worker, "CAMPAIGN_SEND_SCAN_LIMIT", 1000)
    for name, value in (("BOUNCE_BREAKER_MIN_SENT", 20), ("BOUNCE_BREAKER_RATE", 0.08),
                        ("WARMUP_MAX_BOUNCE_RATE", 0.05), ("WARMUP_IMAP_FRESH_SEC", 7200)):
        monkeypatch.setattr(CAMP, name, value)                                # the documented defaults
    monkeypatch.setattr(CAMP, "_default_sender",
                        lambda mb, to, subject, text, message_id="", **kw: (True, "", message_id))
    monkeypatch.setattr(config, "AI_CONTROL_DIR", str(tmp_path / "control"))      # the summary marker lives here
    monkeypatch.setattr(config, "IMAP_ENABLED", True)
    monkeypatch.setattr(config, "IMAP_INTERVAL", 120)
    monkeypatch.setattr(config, "IMAP_USER", "info@qmat.example")
    sent = []
    monkeypatch.setattr(worker, "send_message", lambda text, *a, **k: sent.append(text) or True)
    with Session(e) as s:
        founder = _mk(s, "founder@t", "admin", "internal", "founder")
        seller = _mk(s, "sharks@t", "agent", "seller", "seller")
        mb = MailAccount(user_id=founder.id, email="info@qmat.example", from_name="Qmat Trading", admin_owned=True,
                         active=True, daily_limit=100, sender_company="Qmat Trading LLC", postal_address="Dubai, UAE")
        s.add(mb); s.commit(); s.refresh(mb)
        c = Campaign(name="TRSHARKS anchors — wave 1", tenant_id=seller.id, owner_id=founder.id, mailbox_id=mb.id,
                     status="draft", daily_limit=10, warmup_plan="10,20,35,50", send_days="0,1,2,3,4",
                     send_window_start=8, send_window_end=18)
        s.add(c); s.commit(); s.refresh(c)
        CAMP.set_sequence(s, c, [{"subject": "Anchors for {company}", "body": "Hello {company}."}])
        c.status = "running"; s.add(c); s.commit()
        for i in range(150):
            ld = Lead(product="Anchors", managed=True, seller_id=seller.id, buyer_company=f"Buyer Co {i}",
                      contact_name=f"Contact Person {i}", dest_country="PL", email=f"buyer{i}@b{i}.example")
            s.add(ld); s.commit(); s.refresh(ld)
            s.add(CampaignRecipient(campaign_id=c.id, tenant_id=seller.id, lead_id=ld.id, to_email=ld.email,
                                    sequence_version=1, current_step=0, status="pending"))
        s.commit()
        ids = {"campaign": c.id, "mailbox": mb.id, "seller": seller.id, "founder": founder.id}
    return e, ids, sent


def _imap(e, at, status="ok"):
    """One reply/bounce-reading poll (what inbound_email.poll_inbox records)."""
    with Session(e) as s:
        s.add(IngestionRun(source="email-inbound", status=status, started_at=at, finished_at=at)); s.commit()


def _day(e, when):
    """The first cycle of a sending day (MAX_PER_RUN is high, so it sends the whole day's limit at once)."""
    _imap(e, when - timedelta(minutes=2))
    out = worker.run_campaign_send(now=when)
    assert "error" not in out and out.get("errors", 0) == 0, out
    return out


def _camp(e, ids):
    with Session(e) as s:
        return s.get(Campaign, ids["campaign"])


def _sent_on(e, day):
    start = day.replace(hour=0, minute=0)
    with Session(e) as s:
        return s.exec(select(func.count()).where(CampaignSend.status == "sent", CampaignSend.sent_at >= start,
                                                 CampaignSend.sent_at < start + timedelta(days=1))).one()


def _held(e, open_only=True):
    with Session(e) as s:
        q = select(WorkItem).where(WorkItem.type == HELD)
        if open_only:
            q = q.where(WorkItem.status.in_(("open", "in_progress", "waiting")))
        return s.exec(q).all()


def _set(e, ids, **fields):
    with Session(e) as s:
        c = s.get(Campaign, ids["campaign"])
        for k, v in fields.items():
            setattr(c, k, v)
        s.add(c); s.commit()


def _hard_bounce(e, ids, n):
    with Session(e) as s:
        rs = s.exec(select(CampaignRecipient).where(CampaignRecipient.campaign_id == ids["campaign"],
                                                    CampaignRecipient.status == "completed")
                    .order_by(CampaignRecipient.id).limit(n)).all()
        for r in rs:
            r.status = "hard_bounced"; s.add(r)
        s.commit()


# ---------------------------------------------------------------------------------------------- warm-up ramp
def test_parse_plan_normalizes():
    assert CAMP.parse_warmup_plan("10, 20,35;50,x,0,900") == [10, 20, 35, 50, 500]
    assert CAMP.parse_warmup_plan("20 10 10") == [10, 20]
    assert CAMP.parse_warmup_plan("") == [] and CAMP.parse_warmup_plan(None) == [] and CAMP.parse_warmup_plan("-5") == []


def test_no_plan_never_changes_the_limit(ctx):
    e, ids, _ = ctx
    _set(e, ids, warmup_plan="")
    _day(e, THU)
    _day(e, FRI)
    c = _camp(e, ids)
    assert c.daily_limit == 10 and c.warmup_checked_on == "" and _sent_on(e, FRI) == 10


def test_advances_one_step_after_a_full_clean_sending_day(ctx):
    e, ids, _ = ctx
    _day(e, THU)
    assert _camp(e, ids).daily_limit == 10 and _sent_on(e, THU) == 10        # day 1: no sending day yet → wait
    _day(e, FRI)
    c = _camp(e, ids)
    assert c.daily_limit == 20 and c.warmup_checked_on == "2026-10-09"
    assert _sent_on(e, FRI) == 20                                             # the new limit applies the same day
    with Session(e) as s:
        a = s.exec(select(AuditLog).where(AuditLog.action == "warmup_advance")).one()
        assert '"from": 10' in a.meta and '"to": 20' in a.meta and a.entity_id == ids["campaign"]
        assert s.get(MailAccount, ids["mailbox"]).daily_limit == 100           # the mailbox limit is never touched
    assert _held(e) == []


def test_decides_once_per_day(ctx):
    e, ids, _ = ctx
    _day(e, THU)
    _day(e, FRI)
    with Session(e) as s:
        c = s.get(Campaign, ids["campaign"])
        assert CAMP.apply_warmup(s, c, s.get(MailAccount, ids["mailbox"]), FRI + timedelta(hours=3)) == {"action": "skip"}
    _set(e, ids, daily_limit=10)                                              # an admin lowers it again today
    worker.run_campaign_send(now=FRI + timedelta(hours=4))
    assert _camp(e, ids).daily_limit == 10                                    # no second decision the same day


def test_waits_when_last_sending_day_not_full(ctx, monkeypatch):
    e, ids, _ = ctx
    monkeypatch.setattr(worker, "CAMPAIGN_SEND_MAX_PER_RUN", 6)
    _day(e, THU)                                                              # only 6 of 10 went out
    monkeypatch.setattr(worker, "CAMPAIGN_SEND_MAX_PER_RUN", 200)
    with Session(e) as s:
        d = CAMP.warmup_decision(s, s.get(Campaign, ids["campaign"]), s.get(MailAccount, ids["mailbox"]),
                                 FRI.replace(hour=0), FRI)
        assert d["action"] == "wait" and "6 of 10" in d["why"] and not d["alert"]
    _day(e, FRI)
    assert _camp(e, ids).daily_limit == 10 and _held(e, open_only=False) == []


def test_holds_on_bounce_rate_and_raises_one_task(ctx):
    e, ids, _ = ctx
    _day(e, THU)
    _hard_bounce(e, ids, 1)                                                   # 1 in 10 = 10% ≥ 5%
    _day(e, FRI)
    c = _camp(e, ids)
    assert c.status == "running" and c.daily_limit == 10 and _sent_on(e, FRI) == 10   # held, still sending at 10
    (wi,) = _held(e)
    assert wi.priority == "high" and wi.condition_version == "10:bounce_rate" and "1 hard bounce(s) in 10" in wi.description
    assert wi.tenant_id == ids["seller"] and "Buyer Co" not in wi.description + wi.title
    _day(e, MON)                                                              # 1 in 20 = 5%: still held, the breaker (8%) not
    c = _camp(e, ids)
    assert c.status == "running" and c.daily_limit == 10
    assert len(_held(e)) == 1 and len(_held(e, open_only=False)) == 1        # still exactly one task


def test_holds_when_reply_reading_is_stale_or_failed(ctx):
    e, ids, _ = ctx
    _day(e, THU)
    _imap(e, FRI - timedelta(hours=20))                                       # last success long ago …
    _imap(e, FRI - timedelta(minutes=1), status="error")                      # … and the latest poll failed
    worker.run_campaign_send(now=FRI)
    assert _camp(e, ids).daily_limit == 10
    (wi,) = _held(e)
    assert wi.condition_version == "10:imap_stale" and "IMAP" in wi.description


def test_advance_resolves_the_held_task(ctx):
    e, ids, _ = ctx
    _day(e, THU)
    _imap(e, FRI - timedelta(minutes=1), status="error")                      # the last success is Thursday's
    worker.run_campaign_send(now=FRI)
    assert len(_held(e)) == 1 and _sent_on(e, FRI) == 10
    _day(e, MON)                                                              # reading works again, Fri was full
    assert _camp(e, ids).daily_limit == 20
    assert _held(e) == [] and _held(e, open_only=False)[0].status == "completed"


def test_capped_by_mailbox_daily_limit(ctx):
    e, ids, _ = ctx
    with Session(e) as s:
        mb = s.get(MailAccount, ids["mailbox"]); mb.daily_limit = 15; s.add(mb); s.commit()
    _day(e, THU)
    _day(e, FRI)
    assert _camp(e, ids).daily_limit == 15 and _sent_on(e, FRI) == 15       # min(next step 20, mailbox 15)
    _day(e, MON)
    assert _camp(e, ids).daily_limit == 15
    (wi,) = _held(e)
    assert wi.condition_version == "15:mailbox_cap" and "/mail" in wi.description
    with Session(e) as s:
        assert s.get(MailAccount, ids["mailbox"]).daily_limit == 15           # never raised automatically


def test_weekend_and_outside_window_never_ramp(ctx):
    e, ids, _ = ctx
    _day(e, THU)
    _day(e, FRI.replace(hour=18, minute=30))                                  # Friday, after the window closed
    _day(e, SAT.replace(hour=10))                                             # Saturday
    c = _camp(e, ids)
    assert c.daily_limit == 10 and c.warmup_checked_on == "2026-10-08" and _sent_on(e, FRI) == 0
    _day(e, MON)                                                              # Thursday was the last (full) day
    assert _camp(e, ids).daily_limit == 20


def test_breaker_pauses_before_ramp(ctx):
    e, ids, _ = ctx
    _set(e, ids, daily_limit=20, warmup_plan="20,35,50")
    _day(e, THU)
    _hard_bounce(e, ids, 2)                                                   # 2 in 20 = 10% ≥ the breaker's 8%
    _day(e, FRI)
    c = _camp(e, ids)
    assert c.status == "paused" and c.daily_limit == 20 and c.warmup_checked_on == "2026-10-08"
    assert _held(e) == [] and _sent_on(e, FRI) == 0


def test_resume_needs_a_full_day_since_restart(ctx):
    e, ids, _ = ctx
    _day(e, THU)
    with Session(e) as s:
        c = s.get(Campaign, ids["campaign"])
        CAMP.transition(s, c, "paused"); CAMP.transition(s, c, "running"); s.commit()
        assert c.bounce_baseline == "0:10"
    _day(e, FRI)
    assert _camp(e, ids).daily_limit == 10 and _sent_on(e, FRI) == 10        # nothing sent since the restart yet
    _day(e, MON)
    assert _camp(e, ids).daily_limit == 20


def test_never_lowers_a_manual_limit_above_plan_or_raises_a_zero(ctx):
    e, ids, _ = ctx
    _set(e, ids, daily_limit=40, warmup_plan="10,20,35")
    _day(e, THU)
    _day(e, FRI)
    assert _camp(e, ids).daily_limit == 40 and _sent_on(e, FRI) == 40
    _set(e, ids, daily_limit=0)
    with Session(e) as s:
        d = CAMP.warmup_decision(s, s.get(Campaign, ids["campaign"]), s.get(MailAccount, ids["mailbox"]),
                                 MON.replace(hour=0), MON)
        assert d["action"] == "none" and d["to"] == 0                         # 0 = a deliberate stop


def test_breaker_refactor_uses_the_same_numbers(ctx):
    e, ids, _ = ctx
    _set(e, ids, daily_limit=20)
    _day(e, THU)
    _hard_bounce(e, ids, 1)
    with Session(e) as s:
        c = s.get(Campaign, ids["campaign"])
        assert CAMP.bounce_stats(s, c) == (1, 20)
        c.bounce_baseline = "1:5"
        assert CAMP.bounce_stats(s, c) == (0, 15)
        c.bounce_baseline = "garbage"
        assert CAMP.bounce_stats(s, c) == (1, 20) and not CAMP.bounce_breaker(s, c)   # 5% < 8%


# ---------------------------------------------------------------------------------------------- controls
def _client(email):
    c = TestClient(main.app)
    assert c.post("/login", data={"email": email, "password": "pw"}, follow_redirects=False).status_code == 303
    return c


def test_controls_save_a_plan_and_a_stale_page_never_reverts_a_ramp_increase(ctx):
    e, ids, _ = ctx
    cl = _client("founder@t")
    url = f"/campaigns/{ids['campaign']}/controls"
    base = {"send_window_start": "8", "send_window_end": "18", "send_days": ["0", "1", "2", "3", "4"]}
    page = cl.get(f"/campaigns/{ids['campaign']}")
    assert page.status_code == 200 and 'name="warmup_plan"' in page.text and 'name="daily_limit_was"' in page.text
    assert "Automatic warm-up 10,20,35,50/day" in page.text
    _set(e, ids, warmup_plan="")
    cl.post(url, data={**base, "daily_limit": "10", "daily_limit_was": "10", "warmup_plan": " 10, 20;35 50,x"},
            follow_redirects=False)
    c = _camp(e, ids)
    assert c.warmup_plan == "10,20,35,50" and c.daily_limit == 10
    assert c.warmup_checked_on == datetime.utcnow().strftime("%Y-%m-%d")    # a changed plan starts the next UTC day
    _set(e, ids, daily_limit=20)                                              # the ramp raised it meanwhile
    cl.post(url, data={**base, "daily_limit": "10", "daily_limit_was": "10", "warmup_plan": "10,20,35,50"},
            follow_redirects=False)                                           # the page still showed 10
    assert _camp(e, ids).daily_limit == 20
    cl.post(url, data={**base, "daily_limit": "15", "daily_limit_was": "20", "warmup_plan": "10,20,35,50"},
            follow_redirects=False)                                           # a deliberate change applies
    assert _camp(e, ids).daily_limit == 15
    cl.post(url, data={**base, "daily_limit": "12"}, follow_redirects=False)  # an older form: no guard, no plan field
    c = _camp(e, ids)
    assert c.daily_limit == 12 and c.warmup_plan == "10,20,35,50"
    with Session(e) as s:
        s.add(WorkItem(type=HELD, title="held", idempotency_key=f"{HELD}:{ids['campaign']}")); s.commit()
        assert '"warmup_plan": "10,20,35,50"' in s.exec(select(AuditLog).where(
            AuditLog.action == "controls").order_by(AuditLog.id.desc())).first().meta
    cl.post(url, data={**base, "daily_limit": "12", "daily_limit_was": "12", "warmup_plan": ""},
            follow_redirects=False)                                           # clearing the plan closes its task
    assert _camp(e, ids).warmup_plan == "" and _held(e) == []


# ---------------------------------------------------------------------------------------------- migrations
@pytest.fixture
def gate(tmp_path, monkeypatch):
    eng = sa_create_engine(f"sqlite:///{tmp_path / 'g12.db'}")
    SQLModel.metadata.create_all(eng)
    with eng.begin() as c:
        for col in ("warmup_plan", "warmup_checked_on"):
            c.execute(text(f"ALTER TABLE campaign DROP COLUMN {col}"))
    import scripts.migrate_gate_p11 as G
    importlib.reload(G)
    monkeypatch.setattr(G, "engine", eng)
    monkeypatch.setattr(G, "_is_sqlite", True)
    return G, eng


def test_migrate_and_gate_add_the_warmup_columns(gate, capsys):
    from scripts.migrate import MIGRATIONS
    for col in ("warmup_plan", "warmup_checked_on"):
        assert ("campaign", col, "VARCHAR DEFAULT ''") in MIGRATIONS
    G, eng = gate
    G.main(dry=True)
    assert "ADD COLUMN campaign.warmup_plan" in capsys.readouterr().out
    assert "warmup_plan" not in {c["name"] for c in inspect(eng).get_columns("campaign")}
    G.main(dry=False)
    assert {"warmup_plan", "warmup_checked_on"} <= {c["name"] for c in inspect(eng).get_columns("campaign")}
    capsys.readouterr()
    G.main(dry=False)
    assert "none (already up to date)" in capsys.readouterr().out


def test_the_new_task_type_is_registered():
    from app import work_queue as WQ
    assert HELD in WQ.TYPES and WQ.TYPE_LABELS[HELD] == "Campaign warm-up held" and WQ.PARTY_OF_TYPE[HELD] == "system"


# ---------------------------------------------------------------------------------------------- daily summary
SUMMARY_AT = "18:05"


def _marker(tmp_path):
    return tmp_path / "control" / "daily_summary_last"


def test_summary_is_off_by_default_and_when_invalid(ctx, tmp_path, monkeypatch):
    e, ids, sent = ctx
    monkeypatch.setattr(worker, "DAILY_SUMMARY_AT", "")
    assert worker.run_daily_summary(now=THU.replace(hour=19)) == {"skipped": "off"}
    assert worker.run_daily_summary(now=THU.replace(hour=19), at="6pm") == {"skipped": "off"}
    assert worker.run_daily_summary(now=THU.replace(hour=19), at="25:00") == {"skipped": "off"}
    assert sent == [] and not _marker(tmp_path).exists()
    assert "\nDAILY_SUMMARY_AT=\n" in (ROOT / ".env.example").read_text()   # off unless the server sets it


def test_summary_not_before_the_configured_time(ctx, tmp_path):
    e, ids, sent = ctx
    assert worker.run_daily_summary(now=THU.replace(hour=18, minute=4), at=SUMMARY_AT) == {"skipped": "not due"}
    assert sent == [] and not _marker(tmp_path).exists()


def test_summary_once_per_day_and_survives_restart(ctx, tmp_path):
    e, ids, sent = ctx
    _day(e, THU)
    assert worker.run_daily_summary(now=THU.replace(hour=18, minute=6), at=SUMMARY_AT) == {"sent": True}
    assert len(sent) == 1 and _marker(tmp_path).read_text() == "2026-10-08T18:06:00"
    # the worker keeps no memory of it: every later pass (or a restarted worker) reads the marker on disk
    for minute in (7, 40):
        assert worker.run_daily_summary(now=THU.replace(hour=18, minute=minute), at=SUMMARY_AT) == {
            "skipped": "done today"}
    assert len(sent) == 1
    assert worker.run_daily_summary(now=FRI.replace(hour=18, minute=6), at=SUMMARY_AT) == {"sent": True}
    assert len(sent) == 2 and "since Thu 08 Oct 18:06 UTC" in sent[1]


def test_marker_unwritable_never_sends(ctx, monkeypatch):
    e, ids, sent = ctx
    monkeypatch.setattr(worker, "_write_marker", lambda name, value: False)
    for minute in (6, 8, 10):
        assert worker.run_daily_summary(now=THU.replace(hour=18, minute=minute), at=SUMMARY_AT) == {
            "error": "marker not writable"}
    assert sent == []                                                         # never once per pass


def test_build_failure_is_retried_without_marker(ctx, tmp_path, monkeypatch):
    e, ids, sent = ctx
    real, calls = OPS.build, []

    def flaky(*a, **k):                                                       # fails once (e.g. a locked DB)
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError("db busy")
        return real(*a, **k)
    monkeypatch.setattr(OPS, "build", flaky)
    assert "error" in worker.run_daily_summary(now=THU.replace(hour=18, minute=6), at=SUMMARY_AT)
    assert sent == [] and not _marker(tmp_path).exists()
    assert worker.run_daily_summary(now=THU.replace(hour=18, minute=8), at=SUMMARY_AT) == {"sent": True}
    assert len(sent) == 1 and _marker(tmp_path).exists()


def test_nothing_to_report_marks_the_day_without_sending(ctx, tmp_path):
    e, ids, sent = ctx
    _set(e, ids, status="completed", completed_at=THU - timedelta(days=3))
    _imap(e, THU.replace(hour=18))
    assert worker.run_daily_summary(now=THU.replace(hour=18, minute=6), at=SUMMARY_AT) == {
        "skipped": "nothing to report"}
    assert sent == [] and _marker(tmp_path).exists()


def _inbound(s, lead_id, subject, body, at, channel="email"):
    s.add(Outreach(lead_id=lead_id, direction="in", channel=channel, status="received", from_addr="x@y.example",
                   subject=subject, body=body, created_at=at))


def _seed_activity(e, ids, at):
    """Replies of every kind + bounces at `at`, plus look-alikes that must NOT be counted."""
    with Session(e) as s:
        rs = s.exec(select(CampaignRecipient).where(CampaignRecipient.campaign_id == ids["campaign"])
                    .order_by(CampaignRecipient.id)).all()
        lid = [r.lead_id for r in rs]
        quoted = ("Please send the price list.\n\nOn Thu, 8 Oct 2026 at 10:00, Qmat Trading <info@qmat.example> wrote:\n"
                  "> Hello Buyer Co 3.\n> " + CR.OPT_OUT_LINE)
        _inbound(s, lid[0], "Re: Anchors for Buyer Co 0", "Interested — what is your MOQ?", at)
        _inbound(s, lid[1], "Automatic reply: Anchors", "I am out of the office until Monday.", at)
        _inbound(s, lid[2], "Re: Anchors", "Please remove me from your list.", at)
        _inbound(s, lid[3], "Re: Anchors for Buyer Co 3", quoted, at)        # quotes our footer: still a human reply
        _inbound(s, lid[4], "portal message", "hello", at, channel="portal")  # not an email reply
        other = Lead(product="Bolts", managed=True, seller_id=ids["seller"], buyer_company="Elsewhere Ltd",
                     email="other@elsewhere.example")
        s.add(other); s.commit(); s.refresh(other)
        _inbound(s, other.id, "Re: Bolts", "yes please", at)                  # another campaign's / no campaign's buyer
        s.add(BounceRecord(email_normalized=rs[5].to_email, bounce_type="hard", last_bounce_at=at, first_bounce_at=at))
        s.add(BounceRecord(email_normalized=rs[6].to_email, bounce_type="soft", last_bounce_at=at, first_bounce_at=at))
        s.add(BounceRecord(email_normalized="other@elsewhere.example", bounce_type="hard", last_bounce_at=at,
                           first_bounce_at=at))
        s.commit()


def test_counts_and_reply_kinds(ctx):
    e, ids, _ = ctx
    _day(e, THU)
    _seed_activity(e, ids, THU.replace(hour=12))
    _imap(e, THU.replace(hour=18))
    now = THU.replace(hour=18, minute=5)
    with Session(e) as s:
        d = OPS.collect(s, now, now - timedelta(hours=24))
    (c,) = d["campaigns"]
    assert (c["sent_today"], c["sent_total"], c["limit"], c["enrolled"], c["remaining"], c["day_n"]) == \
        (10, 10, 10, 150, 140, 1)
    assert c["replies"] == {"human": 2, "auto": 1, "unsubscribe": 1}
    assert c["hard_new"] == 1 and (c["hard"], c["since_start"]) == (0, 10)
    assert c["mailbox"]["ok"] and c["mailbox"]["sent_today"] == 10 and c["mailbox"]["limit"] == 100
    assert c["next_day"].isoformat() == "2026-10-09" and c["next_limit"] == 20 and c["warmup"]["action"] == "advance"
    assert not d["inbox"]["problem"] and not d["paused_all"]


def test_period_is_since_previous_summary(ctx):
    e, ids, sent = ctx
    _day(e, THU)
    _imap(e, THU.replace(hour=18))
    worker.run_daily_summary(now=THU.replace(hour=18, minute=6), at=SUMMARY_AT)
    _seed_activity(e, ids, THU.replace(hour=20))                             # after Thursday's summary
    _day(e, FRI)
    _imap(e, FRI.replace(hour=18))
    worker.run_daily_summary(now=FRI.replace(hour=18, minute=6), at=SUMMARY_AT)
    assert "Hard bounces: 0 new" in sent[0] and "Replies: 0 human" in sent[0]
    assert "Hard bounces: 1 new" in sent[1] and "Replies: 2 human · 1 auto-reply · 1 unsubscribe" in sent[1]


def test_no_buyer_pii_and_html_escaped(ctx):
    e, ids, sent = ctx
    _set(e, ids, name="A&B <wave 1>")
    _day(e, THU)
    _seed_activity(e, ids, THU.replace(hour=12))
    _hard_bounce(e, ids, 1)
    _imap(e, THU.replace(hour=18))
    assert worker.run_daily_summary(now=THU.replace(hour=18, minute=6), at=SUMMARY_AT) == {"sent": True}
    (msg,) = sent
    assert "A&amp;B &lt;wave 1&gt;" in msg and "<wave" not in msg
    assert "Sent today 10/10" in msg and "TRSHARKS" not in msg
    with Session(e) as s:
        for ld in s.exec(select(Lead)).all():
            for value in (ld.email, ld.buyer_company, ld.contact_name):
                assert not value or value not in msg, value
        for o in s.exec(select(Outreach).where(Outreach.direction == "in")).all():
            assert o.body.splitlines()[0] not in msg
    assert len(msg) <= OPS.MAX_LEN


def test_projection_matches_ramp_decision(ctx):
    e, ids, sent = ctx
    _day(e, THU)
    _imap(e, THU.replace(hour=18))
    worker.run_daily_summary(now=THU.replace(hour=18, minute=6), at=SUMMARY_AT)
    assert "Next: Fri 09 Oct — 20/day (warm-up 10→20, projected)" in sent[0]
    _day(e, FRI)
    assert _camp(e, ids).daily_limit == 20                                    # what the summary promised


def test_a_morning_summary_projects_today(ctx):
    e, ids, sent = ctx
    _day(e, THU)
    _imap(e, FRI.replace(hour=6, minute=55))
    with Session(e) as s:
        (c,) = OPS.collect(s, FRI.replace(hour=7), THU.replace(hour=7))["campaigns"]
    assert c["next_day"].isoformat() == "2026-10-09" and c["next_limit"] == 20     # the window opens later today
    _set(e, ids, warmup_checked_on="2026-10-09")                              # e.g. the plan was changed this morning
    with Session(e) as s:
        (c,) = OPS.collect(s, FRI.replace(hour=7), THU.replace(hour=7))["campaigns"]
    assert c["next_limit"] == 10 and c["warmup"] is None                      # no step today, so none projected


def test_summary_shows_a_hold_and_open_tasks(ctx):
    e, ids, sent = ctx
    _day(e, THU)
    _hard_bounce(e, ids, 1)
    _day(e, FRI)                                                              # held → one task
    _imap(e, FRI.replace(hour=18))
    worker.run_daily_summary(now=FRI.replace(hour=18, minute=6), at=SUMMARY_AT)
    assert "warm-up held" in sent[0] and "Campaign warm-up held 1" in sent[0]


def test_cli_preview_never_touches_the_marker(ctx, tmp_path):
    e, ids, sent = ctx
    _day(e, THU)
    text_ = worker.preview_daily_summary(now=THU.replace(hour=12))
    assert "Daily summary" in text_ and sent == [] and not _marker(tmp_path).exists()
    worker.preview_daily_summary(send=True, now=THU.replace(hour=12))
    assert len(sent) == 1 and not _marker(tmp_path).exists()


def test_summary_marker_lives_in_the_control_dir(ctx, tmp_path):
    assert worker._marker_path("daily_summary_last") == str(_marker(tmp_path))
    assert worker._write_marker("daily_summary_last", "2026-10-08T18:06:00")
    assert worker._read_marker("daily_summary_last") == "2026-10-08T18:06:00"
    assert not (tmp_path / "control" / "daily_summary_last.tmp").exists()     # written atomically


# ---------------------------------------------------------------------------------------------- backups
def _live_db(tmp_path):
    db = tmp_path / "live.db"
    con = sqlite3.connect(db)
    con.execute("PRAGMA journal_mode=WAL")                                    # like production
    con.execute("CREATE TABLE lead (id INTEGER PRIMARY KEY, email TEXT)")
    con.execute("INSERT INTO lead VALUES (1, 'buyer@x.example')")
    con.commit(); con.close()
    return db


def test_snapshot_is_private_single_file(tmp_path, monkeypatch):
    db = _live_db(tmp_path)
    out = tmp_path / "backups"
    monkeypatch.setattr(backup_db, "DATABASE_URL", f"sqlite:///{db}")
    monkeypatch.setattr(backup_db, "OUT", str(out))
    backup_db.run()
    (snap,) = out.glob("data-*.db")
    assert stat.S_IMODE(snap.stat().st_mode) == 0o600                        # buyer data: owner-only
    assert sorted(p.name for p in out.iterdir()) == [snap.name]              # no -wal / -shm beside it
    con = sqlite3.connect(snap)
    try:
        assert con.execute("PRAGMA journal_mode").fetchone()[0] == "delete"
        assert con.execute("SELECT count(*) FROM lead").fetchone()[0] == 1
    finally:
        con.close()
    assert sorted(p.name for p in out.iterdir()) == [snap.name]              # reading it later leaves nothing either


def test_prune_honours_backup_keep_and_removes_companions(tmp_path, monkeypatch):
    db = _live_db(tmp_path)
    out = tmp_path / "backups"
    out.mkdir()
    for stamp in ("20200101-000000", "20200102-000000", "20200103-000000"):
        (out / f"data-{stamp}.db").write_bytes(b"old")
        os.chmod(out / f"data-{stamp}.db", 0o644)
    (out / "data-20200101-000000.db-wal").write_bytes(b"w")
    (out / "data-20190101-000000.db-shm").write_bytes(b"s")                  # orphan: its snapshot is long gone
    (out / "prod-data-20200101-000000.db").write_bytes(b"pulled copy")      # not ours to prune
    monkeypatch.setattr(backup_db, "DATABASE_URL", f"sqlite:///{db}")
    monkeypatch.setattr(backup_db, "OUT", str(out))
    monkeypatch.setattr(backup_db, "KEEP", 2)
    backup_db.run()
    names = {p.name for p in out.iterdir()}
    new = [n for n in names if n.startswith("data-") and n > "data-2021"]
    assert len(new) == 1 and names == {"data-20200103-000000.db", "prod-data-20200101-000000.db", new[0]}
    assert stat.S_IMODE((out / "data-20200103-000000.db").stat().st_mode) == 0o600   # kept + now owner-only


def test_backup_keep_is_read_from_the_environment(monkeypatch):
    for raw, want in (("3", 3), ("0", 1), ("x", 14)):
        monkeypatch.setenv("BACKUP_KEEP", raw)
        assert backup_db._keep() == want
    monkeypatch.delenv("BACKUP_KEEP")
    assert backup_db._keep() == 14


class _Stop(Exception):
    pass


def _one_loop_pass(monkeypatch, calls):
    """Run worker.main() for exactly one loop pass with every job stubbed out."""
    monkeypatch.setattr(sys, "argv", ["app.worker"])
    for name, value in (("init_db", lambda: None), ("_heartbeat", lambda: None),
                        ("run_inbox", lambda: {"new": 0, "seen": 0}), ("GO4WORLD_ENABLED", False),
                        ("ENRICH_INTERVAL", 0), ("IMAP_ENABLED", False), ("FOLLOWUP_ENABLED", False),
                        ("REQUEST_REMINDER_INTERVAL", 0), ("WORKITEM_SYNC_INTERVAL", 0),
                        ("CAMPAIGN_SEND_INTERVAL", 0), ("BACKUP_INTERVAL", 86400), ("DAILY_SUMMARY_AT", "18:05"),
                        ("run_backup", lambda: calls.append("backup")),
                        ("run_daily_summary", lambda: calls.append("summary") or {"skipped": "not due"})):
        monkeypatch.setattr(worker, name, value)

    def stop(_seconds):
        raise _Stop()
    monkeypatch.setattr(worker, "time", types.SimpleNamespace(time=__import__("time").time, sleep=stop))
    with pytest.raises(_Stop):
        worker.main()


def test_restart_does_not_take_an_extra_backup(tmp_path, monkeypatch):
    out = tmp_path / "backups"
    out.mkdir()
    monkeypatch.setattr(backup_db, "OUT", str(out))
    assert worker._last_backup_time() == 0.0
    snap = out / "data-20261008-030000.db"
    snap.write_bytes(b"x")
    (out / "prod-data-20261009-030000.db").write_bytes(b"x")                 # an off-server copy doesn't count
    os.utime(out / "prod-data-20261009-030000.db", (0, 0))
    assert worker._last_backup_time() == pytest.approx(snap.stat().st_mtime)
    calls = []
    _one_loop_pass(monkeypatch, calls)
    assert calls == ["summary"]                                               # a fresh snapshot → no extra copy
    os.utime(snap, (0, 0))                                                    # the newest snapshot is old
    calls.clear()
    _one_loop_pass(monkeypatch, calls)
    assert calls == ["summary", "backup"]


def test_compose_mounts_backups_into_the_worker():
    worker_block = (ROOT / "docker-compose.coexist.yml").read_text().split("\n  worker:", 1)[1]
    assert "./backups:/app/backups" in worker_block and 'DATABASE_URL: "sqlite:////app/var/go4it.db"' in worker_block
