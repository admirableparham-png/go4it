"""Phase 12 — the campaign follow-up (email 2). It goes out as a real reply in each buyer's own thread (In-Reply-To +
References = the email they got, subject "Re: <that email's subject>"), due per buyer at their own email 1 + N days,
held until the founder approves it, and never after a reply, a bounce (hard or soft) or an unsubscribe. It is appended
IN PLACE to a live campaign by scripts/campaign_followup.py (no new sequence version, so email 1 can never repeat).
The legacy KIMIEL follow-up sweep never touches a managed buyer. Nothing leaves the process: a fake SMTP server builds
and records every real MIME message."""
import types
from datetime import datetime, timedelta
from email import policy
from email.parser import BytesParser

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine, select

import app.main as main
import app.worker as worker
from app import campaign_render as CR
from app import campaign_service as CAMP
from app import config
from app import inbound_email as IE
from app import outreach as OUT
from app import outreach_events as OE
from app import permissions as P
from app import suppression as SUP
from app.auth import hash_password
from app.followups import process_followups
from app.models import (AuditLog, Campaign, CampaignRecipient, CampaignSend, CampaignStep, IngestionRun, Lead, MailAccount,
                        Outreach, ServiceRequest, User, UserProfile, WorkItem)
from scripts import campaign_dryrun as DRY
from scripts import campaign_followup as FU
from scripts import campaign_setup as SETUP

MON = datetime(2026, 10, 5, 9, 0)                     # a Monday, inside the 08–18 UTC window
TEMPLATE = "campaigns/trsharks-anchors"
FOLLOWUP = "campaigns/trsharks-anchors-followup"
SUBJECT_1 = "Metal Wall Plugs & Butterfly Anchors | Supply Enquiry"
PDF = b"%PDF-1.4\n1 0 obj<<>>endobj\ntrailer<<>>\n%%EOF\n"
IDX = (
    "CREATE UNIQUE INDEX IF NOT EXISTS uq_workitem_idem_open ON workitem(idempotency_key) "
    "WHERE idempotency_key != '' AND status IN ('open','in_progress','waiting')",
    "CREATE UNIQUE INDEX IF NOT EXISTS uq_outreach_campaign_send ON outreach(campaign_id,campaign_recipient_id,"
    "campaign_version,campaign_step) WHERE campaign_id IS NOT NULL",
    "CREATE UNIQUE INDEX IF NOT EXISTS uq_suppression_addr_scope ON suppression(email_normalized, scope, tenant_id) "
    "WHERE active = 1",
    "CREATE UNIQUE INDEX IF NOT EXISTS uq_campaignsend_crvs ON campaignsend(campaign_id,recipient_id,"
    "sequence_version,step_index)",
    "CREATE UNIQUE INDEX IF NOT EXISTS uq_lead_req_anonref ON lead(request_id, anon_ref) WHERE anon_ref != ''",
    "CREATE UNIQUE INDEX IF NOT EXISTS uq_camprcpt_campaign_lead ON campaignrecipient(campaign_id, lead_id) "
    "WHERE lead_id IS NOT NULL",
    "CREATE UNIQUE INDEX IF NOT EXISTS uq_camprcpt_campaign_email ON campaignrecipient(campaign_id, to_email) "
    "WHERE to_email != ''",
    "CREATE UNIQUE INDEX IF NOT EXISTS uq_userprofile_user ON userprofile(user_id) WHERE user_id IS NOT NULL",
    "CREATE UNIQUE INDEX IF NOT EXISTS uq_roletemplate_key ON roletemplate(key) WHERE key != ''",
)


class FakeSMTP:
    """Builds + records every real MIME message the campaign path hands over (nothing leaves the process)."""
    sent = []

    def __init__(self, host, port, timeout=None):
        pass

    def starttls(self, context=None):
        pass

    def login(self, user, pw):
        pass

    def send_message(self, msg):
        FakeSMTP.sent.append(BytesParser(policy=policy.default).parsebytes(msg.as_bytes()))

    def quit(self):
        pass


@pytest.fixture
def world(tmp_path, monkeypatch):
    import pathlib
    import shutil
    root = pathlib.Path(__file__).resolve().parents[1]
    for folder in (TEMPLATE, FOLLOWUP):                     # the REAL template text, in a throwaway repo root
        (tmp_path / folder).mkdir(parents=True)
        for f in ("subject.txt", "body.txt", "body.html", "options.txt"):
            shutil.copy(root / folder / f, tmp_path / folder / f)
    (tmp_path / TEMPLATE / "pricelist.pdf").write_bytes(PDF)
    monkeypatch.setattr(CR, "_REPO", str(tmp_path))
    monkeypatch.setattr(SETUP, "BASE", str(tmp_path))
    e = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    SQLModel.metadata.create_all(e)
    with e.connect() as c:
        for ddl in IDX:
            c.execute(text(ddl))
        c.commit()
    for mod in (worker, SETUP, FU, main):
        monkeypatch.setattr(mod, "engine", e)
    monkeypatch.setattr(SETUP, "init_db", lambda: None)
    monkeypatch.setattr(FU, "SETTLE_SECONDS", 0)
    monkeypatch.setattr(FU, "time", types.SimpleNamespace(sleep=lambda _s: None))
    monkeypatch.setattr(OUT.smtplib, "SMTP", FakeSMTP)
    monkeypatch.setattr(OUT, "mail_decrypt", lambda enc: "app-password" if enc else "")
    monkeypatch.setattr(config, "IMAP_ENABLED", True)
    monkeypatch.setattr(config, "IMAP_INTERVAL", 120)
    monkeypatch.setattr(config, "IMAP_USER", "info@qmatalsaha.com")
    for name in ("notify_buyer_reply", "notify_bounce", "send_message", "enrich_lead"):   # never a real alert / fetch
        monkeypatch.setattr(IE, name, lambda *a, **k: None)
    FakeSMTP.sent = []
    with Session(e) as s:
        founder = User(email="founder@t", name="Founder", role="admin", active=True, password_hash="x")
        seller = User(email="trsharks@t", name="trsharks", role="agent", active=True, password_hash="x")
        s.add(founder); s.add(seller); s.commit(); s.refresh(founder); s.refresh(seller)
        s.add(UserProfile(user_id=seller.id, account_class="seller", role_key="seller", company="TRSHARKS"))
        mb = MailAccount(user_id=founder.id, email="info@qmatalsaha.com", from_name="Qmat Alsaha UAE",
                         admin_owned=True, active=True, smtp_password_enc="enc", daily_limit=200)
        sr = ServiceRequest(tracking_code="SR-202608-0001", request_type="buyer_hunt", product="Anchors",
                            status="done", owner_id=seller.id, requester_id=seller.id)
        s.add(mb); s.add(sr); s.commit(); s.refresh(mb); s.refresh(sr)
        ids = {"founder": founder.id, "seller": seller.id, "mailbox": mb.id, "request": sr.id}
    return e, ids


def _buyers(s, ids, names):
    """Managed buyers of the request; addresses are numbered across calls (buyer0@b0.example, buyer1@b1.example …)."""
    out = []
    for i, name in enumerate(names, start=len(s.exec(select(Lead)).all())):
        ld = Lead(product="Anchors", managed=True, owner_id=None, seller_id=ids["seller"], request_id=ids["request"],
                  buyer_company=name, dest_country="PL", email=f"buyer{i}@b{i}.example")
        s.add(ld); s.commit(); s.refresh(ld)
        out.append(ld)
    return out


def _campaign(s, ids, steps, daily_limit=10):
    """A running campaign over the request's buyers — set up exactly like campaign #33 (preview → enrol → start)."""
    c = Campaign(name="TRSHARKS anchors — wave 1", tenant_id=ids["seller"], request_id=ids["request"],
                 owner_id=ids["founder"], mailbox_id=ids["mailbox"], status="draft", daily_limit=daily_limit,
                 send_window_start=8, send_window_end=18, send_days="0,1,2,3,4")
    s.add(c); s.commit(); s.refresh(c)
    CAMP.set_sequence(s, c, steps)
    f = {"request_id": ids["request"]}
    CAMP.enroll(s, c, None, f, expected=CAMP.audience_preview(s, c, f)["final_eligible"])
    assert CAMP.start_problems(s, c) == []
    CAMP.transition(s, c, "running"); s.commit()
    return c.id


def _templates(delay=7, hold=False):
    one, errs1 = SETUP.load_template(TEMPLATE)
    two, errs2 = SETUP.load_template(FOLLOWUP)
    assert errs1 == [] and errs2 == []
    two.update(delay_days=delay, manual_review=hold)
    return one, two


def _inbox_read(t, status="ok"):
    """The IMAP poller (reply + bounce reading) finished at `t`. A follow-up only goes while this is recent."""
    with Session(worker.engine) as s:
        s.add(IngestionRun(source="email-inbound", status=status, started_at=t, finished_at=t)); s.commit()


def _cycle(t, inbox=True):
    if inbox:
        _inbox_read(t)                     # the worker's IMAP poll runs every cycle in production
    res = worker.run_campaign_send(now=t)
    assert "error" not in res and res.get("errors", 0) == 0, res
    return res


def _rcpt(s, email):
    return s.exec(select(CampaignRecipient).where(CampaignRecipient.to_email == email)).one()


def _mid_of(s, email, step):
    r = _rcpt(s, email)
    return s.exec(select(CampaignSend).where(CampaignSend.recipient_id == r.id, CampaignSend.step_index == step,
                                             CampaignSend.status == "sent")).one().rfc_message_id


# ---------------------------------------------------------------------------------------------- threading
def test_the_followup_is_a_reply_in_the_buyers_own_thread(world):
    e, ids = world
    with Session(e) as s:
        _buyers(s, ids, ["IHL Canada (Investments Hardware Ltd.)"])
        cid = _campaign(s, ids, list(_templates()))
    _cycle(MON)
    _cycle(MON + timedelta(days=6, hours=23, minutes=59))         # next Monday 08:59: in the window, not due yet
    assert len(FakeSMTP.sent) == 1
    _cycle(MON + timedelta(days=7))                               # its own email 1 + 7 days
    m1, m2 = FakeSMTP.sent
    assert m1["In-Reply-To"] is None and m1["References"] is None
    assert m2["In-Reply-To"] == m1["Message-ID"] and m2["References"] == m1["Message-ID"]
    assert m2["Message-ID"] != m1["Message-ID"]
    assert str(m1["Subject"]) == SUBJECT_1 and str(m2["Subject"]) == "Re: " + SUBJECT_1
    assert m1["From"] == m2["From"] == "Qmat Alsaha UAE <info@qmatalsaha.com>" and m1["To"] == m2["To"]
    assert m2["List-Unsubscribe"] is None                         # options.txt: no bulk-mail header
    assert [a.get_filename() for a in m1.iter_attachments()] == ["pricelist.pdf"]
    assert list(m2.iter_attachments()) == []                      # the follow-up carries no PDF
    txt, html = m2.get_body(("plain",)).get_content(), m2.get_body(("html",)).get_content()
    assert txt.startswith("Hi IHL Canada team,\n\nJust following up on my note last week")
    assert txt.rstrip().endswith("W  qmatalsaha.com") and "gmail_signature" in html and "Hi IHL Canada team," in html
    for part in (str(m2["Subject"]), txt, html, m2["From"]):
        assert not CR.INTERNAL_BRAND.search(part) and "TRSHARKS" not in part and "{" not in part
    assert "attached" not in txt.lower()
    with Session(e) as s:
        r = s.exec(select(CampaignRecipient).where(CampaignRecipient.campaign_id == cid)).one()
        assert r.status == "completed" and r.current_step == 2 and r.sequence_version == 1
        outs = s.exec(select(Outreach).where(Outreach.campaign_recipient_id == r.id).order_by(Outreach.id)).all()
        assert [(o.campaign_step, o.message_id, o.subject) for o in outs] == [
            (0, m1["Message-ID"], SUBJECT_1), (1, m2["Message-ID"], "Re: " + SUBJECT_1)]


def test_the_followup_subject_is_the_one_email_1_really_had(world):
    e, ids = world
    with Session(e) as s:
        lid = _buyers(s, ids, ["Acme Fixings Ltd"])[0].id
        _campaign(s, ids, [{"subject": "Offer for {company}", "body": "Hello {company}."},
                           {"subject": "Re: offer", "body": "Following up, {company}.", "delay_days": 7}])
    _cycle(MON)
    with Session(e) as s:
        ld = s.get(Lead, lid); ld.buyer_company = "Zeta Tools GmbH"; s.add(ld); s.commit()   # renamed since
    _cycle(MON + timedelta(days=7))
    m1, m2 = FakeSMTP.sent
    assert str(m1["Subject"]) == "Offer for Acme Fixings"
    assert str(m2["Subject"]) == "Re: Offer for Acme Fixings" and m2["In-Reply-To"] == m1["Message-ID"]
    assert "Following up, Zeta Tools." in m2.get_body(("plain",)).get_content()


def test_a_followup_with_its_own_subject_still_threads(world):
    e, ids = world
    with Session(e) as s:
        _buyers(s, ids, ["Acme Fixings Ltd"])
        _campaign(s, ids, [{"subject": "Offer for {company}", "body": "Hello {company}."},
                           {"subject": "Prices for {company}", "body": "Prices, {company}.", "delay_days": 7}])
    _cycle(MON)
    _cycle(MON + timedelta(days=7))
    m1, m2 = FakeSMTP.sent
    assert str(m2["Subject"]) == "Prices for Acme Fixings" and m2["In-Reply-To"] == m1["Message-ID"]


def test_the_render_checks_the_final_reply_subject(world):
    e, ids = world
    with Session(e) as s:
        ld, = _buyers(s, ids, ["Acme Fixings Ltd"])
        c = Campaign(name="R", tenant_id=ids["seller"], request_id=ids["request"], mailbox_id=ids["mailbox"])
        s.add(c); s.commit(); s.refresh(c)
        st = CampaignStep(campaign_id=c.id, step_index=1, subject="Re: x", body="Hi {company}.")
        mb = s.get(MailAccount, ids["mailbox"])

        def subject(thread):
            m = CR.render_campaign_message(s, c, st, ld, mb, thread_subject=thread)
            return m["subject"] if m["ok"] else "BLOCKED: " + m["error"]
        assert subject("Re: Fwd: RE: Offer for Acme") == "Re: Offer for Acme"
        assert subject("") == "Re: x"                                       # no thread → the step's own subject
        assert subject("A" * 199).startswith("BLOCKED: the subject is over 200")
        assert "internal platform name" in subject("Go4it offer")
        assert "names the seller" in subject("TRSHARKS anchors")
        st.subject = "Following up"
        assert subject("Offer for Acme") == "Following up"                  # not written as "Re:" → kept as is


# ---------------------------------------------------------------------------------------------- timing
def test_each_buyer_gets_it_seven_days_after_their_own_email_1(world):
    e, ids = world
    with Session(e) as s:
        _buyers(s, ids, ["Alpha Ltd", "Beta Ltd"])
        _campaign(s, ids, list(_templates(delay=7)), daily_limit=1)
    _cycle(MON)                                                     # Alpha's email 1 (1/day)
    _cycle(MON + timedelta(days=1))                                 # Beta's email 1 on Tuesday
    _cycle(MON + timedelta(days=6, hours=23, minutes=59))           # Monday 08:59 — Alpha is due at 09:00
    assert len(FakeSMTP.sent) == 2
    _cycle(MON + timedelta(days=7))
    _cycle(MON + timedelta(days=7, hours=23, minutes=59))           # Tuesday 08:59 — Beta is due at 09:00
    assert len(FakeSMTP.sent) == 3
    _cycle(MON + timedelta(days=8))
    got = [(m["To"], str(m["Subject"])) for m in FakeSMTP.sent]
    assert got == [("buyer0@b0.example", SUBJECT_1), ("buyer1@b1.example", SUBJECT_1),
                   ("buyer0@b0.example", "Re: " + SUBJECT_1), ("buyer1@b1.example", "Re: " + SUBJECT_1)]


def test_never_on_a_weekend_or_outside_the_hours(world):
    e, ids = world
    with Session(e) as s:
        _buyers(s, ids, ["Alpha Ltd"])
        _campaign(s, ids, list(_templates(delay=5)))
    _cycle(MON)                                                     # email 2 due Saturday 09:00
    for t in (MON + timedelta(days=5, hours=1), MON + timedelta(days=6, hours=1),     # Sat, Sun
              MON + timedelta(days=7, hours=-1, minutes=-1)):                         # Mon 07:59
        _cycle(t)
    assert len(FakeSMTP.sent) == 1
    _cycle(MON + timedelta(days=7, hours=-1))                       # Monday 08:00 — the window opens
    assert len(FakeSMTP.sent) == 2 and str(FakeSMTP.sent[1]["Subject"]) == "Re: " + SUBJECT_1


def test_no_followup_while_reply_reading_is_down_but_first_emails_go_on(world):
    e, ids = world
    with Session(e) as s:
        _buyers(s, ids, ["Alpha Ltd", "Beta Ltd"])
        cid = _campaign(s, ids, list(_templates(delay=7)))
    _cycle(MON)                                                     # both email 1s
    with Session(e) as s:                                           # a buyer enrolled later: email 1 still due
        late, = _buyers(s, ids, ["Gamma Ltd"])
        late_email = late.email
        s.add(CampaignRecipient(campaign_id=cid, tenant_id=ids["seller"], lead_id=late.id, to_email=late_email,
                                sequence_version=1, request_id=ids["request"]))
        s.commit()
    t = MON + timedelta(days=7, hours=1)                            # both follow-ups due; IMAP last OK 3 h ago
    _inbox_read(t - timedelta(hours=3))
    _inbox_read(t - timedelta(minutes=2), status="error")           # a failed poll is not reading
    _cycle(t, inbox=False)
    assert [str(m["Subject"]) for m in FakeSMTP.sent] == [SUBJECT_1] * 3 and FakeSMTP.sent[2]["To"] == late_email
    with Session(e) as s:
        c, mb = s.get(Campaign, cid), s.get(MailAccount, ids["mailbox"])
        r = _rcpt(s, "buyer0@b0.example")
        assert CAMP.can_send(s, c, r, mb, t) == (False, "reply reading stale")
        assert not CAMP.is_campaign_level_skip("reply reading stale")
        assert (r.status, r.current_step) == ("sent", 1)            # held, not settled — it goes once reading works
        assert c.status == "running"
    _cycle(t + timedelta(minutes=5))                                # the poll succeeds again
    assert [str(m["Subject"]) for m in FakeSMTP.sent[3:]] == ["Re: " + SUBJECT_1] * 2


def test_an_interrupted_inbox_read_does_not_release_a_follow_up(world):
    e, ids = world
    with Session(e) as s:
        _buyers(s, ids, ["Alpha Ltd"])
        _campaign(s, ids, list(_templates(delay=7)))
    _cycle(MON)
    t = MON + timedelta(days=7, hours=1)
    _inbox_read(t - timedelta(minutes=1), status="partial")       # the connection dropped half-way
    _cycle(t, inbox=False)
    assert len(FakeSMTP.sent) == 1                                # held: that read may have missed the reply
    _cycle(t + timedelta(minutes=5))                              # a complete read
    assert len(FakeSMTP.sent) == 2


def test_reply_reading_is_checked_once_per_cycle_not_once_per_buyer(world, monkeypatch):
    e, ids = world
    with Session(e) as s:
        _buyers(s, ids, [f"Buyer {i} Ltd" for i in range(6)])
        _campaign(s, ids, list(_templates(delay=7)))
    _cycle(MON)                                                     # six email 1s
    calls, real = [], CAMP.last_inbox_success
    monkeypatch.setattr(CAMP, "last_inbox_success", lambda s, **kw: calls.append(1) or real(s, **kw))
    _cycle(MON + timedelta(days=7, hours=1), inbox=False)           # six follow-ups due, reading a week old
    assert len(calls) == 1 and len(FakeSMTP.sent) == 6


# ---------------------------------------------------------------------------------------------- hold → approve
def test_a_held_followup_never_sends_until_approved(world):
    e, ids = world
    with Session(e) as s:
        _buyers(s, ids, ["Alpha Ltd", "Beta Ltd"])
        cid = _campaign(s, ids, list(_templates(delay=7, hold=True)))
    _cycle(MON)
    assert len(FakeSMTP.sent) == 2
    with Session(e) as s:                                           # a buyer enrolled later still gets email 1
        late, = _buyers(s, ids, ["Gamma Ltd"])
        s.add(CampaignRecipient(campaign_id=cid, tenant_id=ids["seller"], lead_id=late.id, to_email=late.email,
                                sequence_version=1, request_id=ids["request"]))
        s.commit()
        before = {r.id: (r.status, r.next_action_at, r.current_step)
                  for r in s.exec(select(CampaignRecipient).where(CampaignRecipient.current_step == 1)).all()}
    t = MON + timedelta(days=7, hours=1)
    for minute in (0, 5, 10):                                       # due and held, cycle after cycle
        _cycle(t + timedelta(minutes=minute))
    assert [m["To"] for m in FakeSMTP.sent[2:]] == ["buyer2@b2.example"]
    assert str(FakeSMTP.sent[2]["Subject"]) == SUBJECT_1
    with Session(e) as s:
        held = s.exec(select(CampaignRecipient).where(CampaignRecipient.id.in_(list(before)))).all()
        assert {r.id: (r.status, r.next_action_at, r.current_step) for r in held} == before
        assert s.exec(select(CampaignSend).where(CampaignSend.step_index == 1)).all() == []
        assert s.get(MailAccount, ids["mailbox"]).sent_today == 1        # the held ones used no slot
        assert s.get(Campaign, cid).status == "running"                 # never 'completed' while held
        c = s.get(Campaign, cid)
        assert CAMP.approve_step(s, c, 1) == (True, "email 2 approved")
        assert CAMP.approve_step(s, c, 1) == (True, "email 2 is already approved")   # idempotent
        assert len(s.exec(select(AuditLog).where(AuditLog.action == "approve_step")).all()) == 1
        assert CAMP.approve_step(s, c, 5)[0] is False
    _cycle(t + timedelta(minutes=15))
    assert sorted(m["To"] for m in FakeSMTP.sent[3:]) == ["buyer0@b0.example", "buyer1@b1.example"]
    with Session(e) as s:
        for m in FakeSMTP.sent[3:]:
            assert str(m["Subject"]) == "Re: " + SUBJECT_1 and m["In-Reply-To"] == _mid_of(s, m["To"], 0)


# ---------------------------------------------------------------------------------------------- append + reopen
def test_append_step_adds_in_place_and_never_touches_email_1(world):
    e, ids = world
    one, two = _templates(delay=7, hold=True)
    with Session(e) as s:
        _buyers(s, ids, ["Alpha Ltd"])
        cid = _campaign(s, ids, [one])
        c = s.get(Campaign, cid)
        st0 = CAMP.steps_for(s, c)[0]
        snap = (st0.id, st0.subject, st0.body, st0.body_html, st0.attachment_path, st0.plain_text_only,
                st0.list_unsubscribe, st0.delay_days, st0.manual_review)
        assert "pause the campaign first" in CAMP.append_step(s, c, two)["error"]
        assert len(CAMP.steps_for(s, c)) == 1
        CAMP.transition(s, c, "paused"); s.commit()
        res = CAMP.append_step(s, c, two)
        assert res == {"step_index": 1, "added": True, "error": ""}
        c = s.get(Campaign, cid)
        assert c.sequence_version == 1
        st0, st1 = CAMP.steps_for(s, c)
        assert (st0.id, st0.subject, st0.body, st0.body_html, st0.attachment_path, st0.plain_text_only,
                st0.list_unsubscribe, st0.delay_days, st0.manual_review) == snap
        assert st0.attachment_path == "campaigns/trsharks-anchors/pricelist.pdf" and st0.list_unsubscribe is False
        assert (st1.version, st1.subject, st1.delay_days, st1.manual_review, st1.attachment_path,
                st1.list_unsubscribe) == (1, "Re: " + SUBJECT_1, 7, True, "", False)
        assert CAMP.append_step(s, c, two) == {"step_index": 1, "added": False, "error": ""}   # a re-run adds nothing
        assert len(CAMP.steps_for(s, c)) == 2
        assert len(s.exec(select(AuditLog).where(AuditLog.action == "append_step")).all()) == 1
        # a changed text never queues behind the held draft (approving email 2 would send the old one)
        assert "still held and never sent" in CAMP.append_step(s, c, dict(two, body="Revised {company}."))["error"]
        assert CAMP.approve_step(s, c, 1)[0]
        bad = dict(two, body="Hi {company}, see go4it.vip for prices.")
        assert "internal platform name" in CAMP.append_step(s, c, bad)["error"]
        assert "names the seller" in CAMP.append_step(s, c, dict(two, body="TRSHARKS says hi"))["error"]
        assert CAMP.append_step(s, c, dict(two, body="Third, {company}."), apply=False) == {
            "step_index": 2, "added": False, "error": ""}                  # a check only — nothing written
        assert len(CAMP.steps_for(s, c)) == 2
        s.add(CampaignStep(campaign_id=cid, version=1, step_index=3, subject="x", body="y")); s.commit()
        assert "already exists" in CAMP.append_step(s, c, dict(two, body="Another one."))["error"]
        c.status = "cancelled"; s.add(c); s.commit()
        assert "cancelled" in CAMP.append_step(s, c, dict(two, body="Late."))["error"]


def test_a_revised_draft_replaces_the_held_email_and_stays_held(world):
    e, ids = world
    one, two = _templates(delay=7, hold=True)
    revised = dict(two, body="Hi {company} team,\n\nA shorter note.", body_html="")
    with Session(e) as s:
        _buyers(s, ids, ["Alpha Ltd"])
        cid = _campaign(s, ids, [one])
        c = s.get(Campaign, cid)
        assert "no follow-up to replace" in CAMP.replace_held_step(s, c, revised, apply=False)["error"]
        CAMP.transition(s, c, "paused"); s.commit()
        assert CAMP.append_step(s, c, two)["added"]
        sid = CAMP.steps_for(s, c)[1].id
        assert "internal platform name" in CAMP.replace_held_step(s, c, dict(revised, body="go4it {company}"))["error"]
        assert "keeps its 7-day delay" in CAMP.replace_held_step(s, c, dict(revised, delay_days=3))["error"]
        assert CAMP.replace_held_step(s, c, revised, apply=False)["error"] == ""
        assert CAMP.steps_for(s, c)[1].body != revised["body"]             # a check only
        assert CAMP.replace_held_step(s, c, dict(revised, manual_review=False)) == {
            "step_index": 1, "replaced": True, "error": ""}
        st = CAMP.steps_for(s, c)[1]
        assert (st.id, st.body, st.body_html, st.manual_review, st.delay_days) == (sid, revised["body"], "", True, 7)
        assert len(s.exec(select(AuditLog).where(AuditLog.action == "replace_held_step")).all()) == 1
        CAMP.transition(s, c, "running"); s.commit()
        assert "pause the campaign first" in CAMP.replace_held_step(s, c, two)["error"]
        r = s.exec(select(CampaignRecipient)).one()
        s.add(CampaignSend(campaign_id=cid, recipient_id=r.id, sequence_version=1, step_index=1, status="retryable"))
        s.commit()
        CAMP.transition(s, c, "paused"); s.commit()
        assert "already went out" in CAMP.replace_held_step(s, c, two)["error"]
        st.manual_review = False; s.add(st); s.commit()
        assert "is approved" in CAMP.replace_held_step(s, c, two)["error"]


def test_reopen_gives_it_only_to_buyers_who_may_still_get_email(world):
    e, ids = world
    names = ["Plain Ltd", "Human Ltd", "Bounce Ltd", "Unsub Ltd", "Supp Ltd", "Elsewhere Ltd", "Soft Ltd",
             "Waiting Ltd"]
    with Session(e) as s:
        _buyers(s, ids, names)
        cid = _campaign(s, ids, [_templates()[0]], daily_limit=7)       # the 8th waits: the campaign keeps running
    _cycle(MON)
    with Session(e) as s:
        assert sorted(r.status for r in s.exec(select(CampaignRecipient)).all()) == ["completed"] * 7 + ["pending"]
        mid = {m["To"]: m["Message-ID"] for m in FakeSMTP.sent}
        IE.handle_inbound(s, "buyer1@b1.example", "Re: " + SUBJECT_1, "Interested, please send prices.",
                          "<r1@buyer>", mid["buyer1@b1.example"])
        IE.handle_bounce(s, "buyer2@b2.example", "550 5.1.1 user unknown")
        IE.handle_inbound(s, "buyer3@b3.example", "Re: " + SUBJECT_1, "Please unsubscribe us.", "<r3@buyer>",
                          mid["buyer3@b3.example"])
        SUP.suppress(s, "buyer4@b4.example", "manual"); s.commit()
        ld = s.exec(select(Lead).where(Lead.email == "buyer5@b5.example")).one()   # replied from another address
        ld.buyer_replied_at = MON; s.add(ld); s.commit()
        OE.on_bounce(s, s.exec(select(Lead).where(Lead.email == "buyer6@b6.example")).one(), "buyer6@b6.example",
                     "451 4.7.1 try again later")
        lone, = _buyers(s, ids, ["Never Sent Ltd"])                    # 'completed' with no email to reply to
        s.add(CampaignRecipient(campaign_id=cid, tenant_id=ids["seller"], lead_id=lone.id, to_email=lone.email,
                                sequence_version=1, current_step=1, status="completed"))
        s.commit()
    _cycle(MON + timedelta(minutes=5))                               # the soft-bounced one settles 'completed' again
    with Session(e) as s:
        c = s.get(Campaign, cid)
        CAMP.transition(s, c, "paused"); s.commit()
        assert CAMP.append_step(s, c, _templates(delay=7, hold=True)[1])["added"]
        statuses = {r.id: r.status for r in s.exec(select(CampaignRecipient)).all()}
        plan = CAMP.reopen_completed(s, c)                            # dry run
        assert plan["eligible"] == 1 and plan["reopened"] == 0
        assert plan["left"] == {"suppressed": 1, "replied": 1, "soft-bounced": 1, "no sent email to reply to": 1}
        assert {r.id: r.status for r in s.exec(select(CampaignRecipient)).all()} == statuses   # wrote nothing
        res = CAMP.reopen_completed(s, c, apply=True)
        assert res["reopened"] == 1
        r = _rcpt(s, "buyer0@b0.example")
        sent_at = s.exec(select(CampaignSend).where(CampaignSend.recipient_id == r.id)).one().sent_at
        assert r.status == "sent" and r.next_action_at == sent_at + timedelta(days=7) and r.current_step == 1
        assert {x.to_email: x.status for x in s.exec(select(CampaignRecipient)).all()
                if x.to_email != "buyer0@b0.example"} == {
            "buyer1@b1.example": "replied", "buyer2@b2.example": "hard_bounced",
            "buyer3@b3.example": "unsubscribed", "buyer4@b4.example": "completed",
            "buyer5@b5.example": "completed", "buyer6@b6.example": "completed", "buyer7@b7.example": "pending",
            "buyer8@b8.example": "completed"}
        assert CAMP.reopen_completed(s, c, apply=True)["reopened"] == 0          # idempotent
        assert len(s.exec(select(AuditLog).where(AuditLog.action == "reopen_completed")).all()) == 1
        with pytest.raises(ValueError):
            CAMP.reopen_completed(s, c, apply=True, steps=CAMP.steps_for(s, c))


def test_a_reopened_buyer_never_gets_email_1_again(world):
    e, ids = world
    with Session(e) as s:
        _buyers(s, ids, ["Alpha Ltd"])
        cid = _campaign(s, ids, [_templates()[0]])
    _cycle(MON)
    with Session(e) as s:
        c = s.get(Campaign, cid)
        CAMP.transition(s, c, "paused"); s.commit()
        CAMP.append_step(s, c, _templates(delay=7)[1])
        CAMP.reopen_completed(s, c, apply=True)
        CAMP.transition(s, c, "running"); s.commit()
        r = _rcpt(s, "buyer0@b0.example")
        r.current_step = 0; s.add(r); s.commit()                          # a wrong cursor …
        out = CAMP.send_step(s, s.get(Campaign, cid), r, s.get(MailAccount, ids["mailbox"]),
                             now=MON + timedelta(days=8))
        assert out["status"] == "already_sent"                            # … is stopped by the v1 duplicate guard
        r.current_step = 1; s.add(r); s.commit()
    _cycle(MON + timedelta(days=8))
    _cycle(MON + timedelta(days=8, minutes=5))
    assert [str(m["Subject"]) for m in FakeSMTP.sent] == [SUBJECT_1, "Re: " + SUBJECT_1]
    with Session(e) as s:
        steps = [o.campaign_step for o in s.exec(select(Outreach).where(Outreach.campaign_id == cid)).all()]
        assert sorted(steps) == [0, 1]


def test_a_followup_without_a_sent_email_1_is_never_sent(world):
    e, ids = world
    with Session(e) as s:
        _buyers(s, ids, ["Alpha Ltd"])
        cid = _campaign(s, ids, list(_templates(delay=0)))
        r = s.exec(select(CampaignRecipient)).one()
        r.current_step, r.status = 1, "sent"; s.add(r); s.commit()        # at email 2, but email 1 never went out
        _inbox_read(MON)
        out = CAMP.send_step(s, s.get(Campaign, cid), r, s.get(MailAccount, ids["mailbox"]), now=MON,
                             sender=lambda *a, **k: pytest.fail("must not send"))
        assert out == {"status": "render_failed", "scope": "recipient",
                       "reason": "follow-up has no sent email to reply to"}
        assert s.get(CampaignRecipient, r.id).status == "skipped"
        assert s.exec(select(CampaignSend)).all() == []                   # nothing claimed
        assert s.get(MailAccount, ids["mailbox"]).sent_today == 0          # no slot used
        assert s.exec(select(WorkItem).where(WorkItem.idempotency_key == f"campaign_render_skip:{cid}")).first()
        assert s.get(Campaign, cid).status == "running"


# ---------------------------------------------------------------------------------------------- stop rules
def test_reply_bounce_and_unsubscribe_stop_the_followup(world, monkeypatch):
    e, ids = world
    names = ["Human Ltd", "Auto Ltd", "Unsub Ltd", "Hard Ltd", "Soft Ltd", "Soft Two Ltd", "Quiet Ltd"]
    with Session(e) as s:
        _buyers(s, ids, names)
        cid = _campaign(s, ids, list(_templates(delay=7)))
    _cycle(MON)
    mid = {m["To"]: m["Message-ID"] for m in FakeSMTP.sent}
    assert len(mid) == 7

    def refill(session, lead, apply=True):          # the buyer's site lists another address — a review candidate only
        if lead.buyer_company == "Soft Ltd":
            return {"status": "enriched", "email": "fresh@b4.example", "site": "https://b4.example"}
        return {"status": "nohit", "site": ""}
    monkeypatch.setattr(IE, "enrich_lead", refill)
    with Session(e) as s:
        IE.handle_inbound(s, "buyer0@b0.example", "Re: " + SUBJECT_1, "Interested — please send prices.",
                          "<r0@buyer>", mid["buyer0@b0.example"])
        IE.handle_inbound(s, "buyer1@b1.example", "Automatic reply: " + SUBJECT_1,
                          "I am out of the office until Monday.", "<r1@buyer>", mid["buyer1@b1.example"])
        IE.handle_inbound(s, "buyer2@b2.example", "Re: " + SUBJECT_1, "Please unsubscribe us.", "<r2@buyer>",
                          mid["buyer2@b2.example"])
        IE.handle_bounce(s, "buyer3@b3.example", "550 5.1.1 user unknown")
        IE.handle_bounce(s, "buyer4@b4.example", "451 4.7.1 try again later")          # soft, then refilled
        # Phase 12 (enrichment): a bounce never re-arms an address — the candidate goes to review, not onto the lead
        assert s.exec(select(Lead).where(Lead.email == "fresh@b4.example")).first() is None
        OE.on_bounce(s, s.exec(select(Lead).where(Lead.email == "buyer5@b5.example")).one(), "buyer5@b5.example",
                     "451 4.7.1 try again later")
    _cycle(MON + timedelta(days=7))
    follow = [m for m in FakeSMTP.sent[7:]]
    assert [m["To"] for m in follow] == ["buyer6@b6.example"]               # only the quiet buyer
    assert follow[0]["In-Reply-To"] == mid["buyer6@b6.example"]
    with Session(e) as s:
        st = {r.to_email: r.status for r in s.exec(select(CampaignRecipient)).all()}
        assert st == {"buyer0@b0.example": "replied", "buyer1@b1.example": "replied",
                      "buyer2@b2.example": "unsubscribed", "buyer3@b3.example": "hard_bounced",
                      "buyer4@b4.example": "skipped", "buyer5@b5.example": "skipped",
                      "buyer6@b6.example": "completed"}
        assert len(s.exec(select(CampaignSend).where(CampaignSend.step_index == 1)).all()) == 1
        assert s.get(Campaign, cid).status == "completed"                  # everyone settled → the campaign finishes


# ---------------------------------------------------------------------------------------------- legacy sweep
def test_the_legacy_followup_sweep_never_touches_a_managed_buyer(monkeypatch):
    sent, calls = [], []
    monkeypatch.setattr("app.followups.send_email", lambda to, *a, **k: (sent.append(to), (True, "", "<fu@x>"))[1])
    monkeypatch.setattr("app.followups.notify_needs_call", lambda lead: calls.append(lead.email))
    monkeypatch.setattr("app.followups.notify_outreach_sent", lambda *a, **k: None)
    eng = create_engine("sqlite://")
    SQLModel.metadata.create_all(eng)
    due = datetime.utcnow() - timedelta(minutes=1)
    with Session(eng) as s:
        for em, managed, note in (("managed@x.com", True, "followup-1"), ("managed-call@x.com", True, "call"),
                                  ("own@x.com", False, "followup-1")):
            s.add(Lead(product="Anchors", buyer_company="B", email=em, status="new", managed=managed,
                       next_action_at=due, next_action_note=note))
        s.commit()
        summary = process_followups(s, now=datetime.utcnow(), log=lambda *_: None)
        assert sent == ["own@x.com"] and calls == [] and summary["due"] == 1
        for ld in s.exec(select(Lead).where(Lead.managed == True)).all():   # noqa: E712
            assert ld.next_action_at is not None and ld.next_action_note in ("followup-1", "call")


def test_a_manual_email_to_a_managed_buyer_never_arms_the_legacy_followup(world, monkeypatch):
    e, ids = world
    monkeypatch.setattr(main, "send_email", lambda *a, **k: (True, "", "<mid@test>"))
    monkeypatch.setattr(main, "notify_outreach_sent", lambda *a, **k: None)
    monkeypatch.setattr(main, "FOLLOWUP_ENABLED", True)
    with Session(e) as s:
        boss = User(email="boss@t", name="Boss", role="admin", active=True, password_hash=hash_password("pw"))
        s.add(boss); s.commit(); s.refresh(boss)
        s.add(UserProfile(user_id=boss.id, account_class="internal", role_key="founder",
                          scope=P.ROLE_TEMPLATES["founder"]["scope"], account_status="active"))
        managed, = _buyers(s, ids, ["Managed Ltd"])
        own = Lead(product="Anchors", owner_id=boss.id, email="own@x.com", buyer_company="Own Buyer")
        s.add(own); s.commit(); s.refresh(own)
        lids = (managed.id, own.id)
    c = TestClient(main.app)
    assert c.post("/login", data={"email": "boss@t", "password": "pw"}, follow_redirects=False).status_code == 303
    for lid in lids:
        r = c.post(f"/leads/{lid}/outreach", data={"channel": "email", "subject": "hi", "body": "b", "send": "1"},
                   follow_redirects=False)
        assert r.status_code == 303
    with Session(e) as s:
        assert (s.get(Lead, lids[0]).next_action_note or "") == ""        # managed: campaigns only
        assert s.get(Lead, lids[1]).next_action_note == "followup-1"      # an own lead keeps the legacy sequence


# ---------------------------------------------------------------------------------------------- campaign_setup
def test_setup_never_resets_a_campaign_that_has_sent(world, capsys):
    e, ids = world
    with Session(e) as s:
        _buyers(s, ids, ["Alpha Ltd", "Beta Ltd"])
    base = ["--template", TEMPLATE, "--mailbox", "info@qmatalsaha.com", "--request", "SR-202608-0001"]
    assert SETUP.main(base + ["--enrol", "--start"]) == 0
    with Session(e) as s:
        cid = s.exec(select(Campaign)).one().id
    _cycle(MON)
    with Session(e) as s:
        c = s.get(Campaign, cid)
        CAMP.transition(s, c, "paused"); s.commit()
        assert CAMP.append_step(s, c, _templates(delay=7, hold=True)[1])["added"]
    capsys.readouterr()
    assert SETUP.main(base + ["--campaign", str(cid)]) == 0
    assert "sequence kept" in capsys.readouterr().out
    with Session(e) as s:
        steps = CAMP.steps_for(s, s.get(Campaign, cid))
        assert [st.subject for st in steps] == [SUBJECT_1, "Re: " + SUBJECT_1] and steps[1].manual_review


def test_setup_never_lowers_a_live_campaigns_daily_limit(world, capsys):
    e, ids = world
    with Session(e) as s:
        _buyers(s, ids, ["Alpha Ltd", "Beta Ltd"])
    base = ["--template", TEMPLATE, "--mailbox", "info@qmatalsaha.com", "--request", "SR-202608-0001"]
    assert SETUP.main(base) == 0
    with Session(e) as s:
        c = s.exec(select(Campaign)).one()
        assert c.daily_limit == SETUP.DEFAULT_DAILY_LIMIT
        cid = c.id
    assert SETUP.main(base + ["--campaign", str(cid), "--daily-limit", "12"]) == 0    # a draft may still change
    assert SETUP.main(base + ["--campaign", str(cid), "--enrol", "--start"]) == 0     # no flag → kept
    with Session(e) as s:
        assert (s.get(Campaign, cid).daily_limit, s.get(Campaign, cid).status) == (12, "running")
    _cycle(MON)
    with Session(e) as s:
        c = s.get(Campaign, cid)
        c.daily_limit, c.warmup_plan = 35, "10,20,35,50"                   # the warm-up ramp took it to 35
        s.add(c); s.commit()
    capsys.readouterr()
    for extra in (["--daily-limit", "10"], ["--enrol"], ["--daily-limit", "10", "--enrol"]):
        assert SETUP.main(base + ["--campaign", str(cid)] + extra) == 0
        with Session(e) as s:
            assert s.get(Campaign, cid).daily_limit == 35, extra
    assert "daily limit kept at 35/day" in capsys.readouterr().out
    with Session(e) as s:                                                  # paused, no plan, but it has sent
        c = s.get(Campaign, cid)
        CAMP.transition(s, c, "paused"); c.warmup_plan = ""; s.add(c); s.commit()
    assert SETUP.main(base + ["--campaign", str(cid), "--daily-limit", "10"]) == 0
    with Session(e) as s:
        assert s.get(Campaign, cid).daily_limit == 35


def test_followup_is_for_smoke_tests_only(world):
    with pytest.raises(SystemExit):
        SETUP.main(["--template", TEMPLATE, "--mailbox", "info@qmatalsaha.com", "--request", "SR-202608-0001",
                    "--followup", FOLLOWUP])


def test_a_two_email_smoke_test_lands_as_one_thread(world, capsys):
    e, ids = world
    rc = SETUP.main(["--template", TEMPLATE, "--mailbox", "info@qmatalsaha.com",
                     "--smoke", "admirable.parham+1@gmail.com|IHL Canada (Investments Hardware Ltd.)|CA",
                     "--smoke", "admirable.parham+2@gmail.com|Inoxa Sp. z o.o.|PL",
                     "--followup", FOLLOWUP, "--followup-delay", "0", "--start"])
    assert rc == 0 and "STARTED" in capsys.readouterr().out
    with Session(e) as s:
        c = s.exec(select(Campaign)).one()
        assert c.daily_limit == 4 and [st.delay_days for st in CAMP.steps_for(s, c)] == [0, 0]
        assert not any(st.manual_review for st in CAMP.steps_for(s, c))
    sat = datetime(2026, 10, 10, 22, 0)                           # a smoke test goes out whatever the day or hour
    _cycle(sat)
    _cycle(sat + timedelta(minutes=5))
    by_to = {}
    for m in FakeSMTP.sent:
        by_to.setdefault(m["To"], []).append(m)
    assert sorted(by_to) == ["admirable.parham+1@gmail.com", "admirable.parham+2@gmail.com"]   # never a real buyer
    for to, (m1, m2) in by_to.items():
        assert str(m1["Subject"]) == SUBJECT_1 and str(m2["Subject"]) == "Re: " + SUBJECT_1
        assert m2["In-Reply-To"] == m2["References"] == m1["Message-ID"]
    assert by_to["admirable.parham+2@gmail.com"][1].get_body(("plain",)).get_content().startswith("Hi Inoxa team,")


# ---------------------------------------------------------------------------------------------- the ops script
def _live_33(e, ids, n=3, sent=2):
    """A live campaign like #33: email 1 only, `sent` buyers already emailed (→ 'completed'), the rest waiting."""
    with Session(e) as s:
        _buyers(s, ids, [f"Buyer {i} Ltd" for i in range(n)])
        cid = _campaign(s, ids, [_templates()[0]], daily_limit=sent)
    _cycle(MON)
    return cid


def _snapshot(e):
    with Session(e) as s:
        return ([(st.id, st.step_index, st.manual_review) for st in s.exec(select(CampaignStep)).all()],
                [(r.id, r.status, r.next_action_at, r.current_step) for r in s.exec(select(CampaignRecipient)).all()],
                [(c.status, c.pause_reason, c.bounce_baseline) for c in s.exec(select(Campaign)).all()],
                len(s.exec(select(AuditLog)).all()))


ADD = ["add", "{cid}", "--template", FOLLOWUP, "--delay-days", "7", "--hold"]


def _add(cid, *extra):
    return FU.main([a.format(cid=cid) for a in ADD] + list(extra))


def test_script_dry_run_changes_nothing_and_shows_the_threaded_email(world, capsys):
    e, ids = world
    cid = _live_33(e, ids)
    before = _snapshot(e)
    capsys.readouterr()
    assert _add(cid) == 0
    out = capsys.readouterr().out
    assert _snapshot(e) == before
    assert "DRY RUN — nothing changed" in out and "HELD until approve" in out
    assert "re-open: 2 buyer(s) who had finished get it, each 7 day(s) after their own last email" in out
    assert "1 buyer(s) still before it get it 7 day(s) after their own email 1" in out
    with Session(e) as s:
        mid = s.exec(select(CampaignSend)).first().rfc_message_id
    assert f"Subject: Re: {SUBJECT_1}" in out and f"In-Reply-To: {mid}" in out and "Hi Buyer 0 team," in out


def test_script_refuses_while_a_send_is_in_flight(world, capsys):
    e, ids = world
    cid = _live_33(e, ids)
    with Session(e) as s:
        r = s.exec(select(CampaignRecipient).where(CampaignRecipient.status == "pending")).one()
        s.add(CampaignSend(campaign_id=cid, recipient_id=r.id, sequence_version=1, step_index=0, status="sending",
                           claim_token="t", lease_expires_at=datetime.utcnow() + timedelta(minutes=2)))
        s.commit()
    before = _snapshot(e)
    assert _add(cid, "--apply") == 1
    assert "REFUSED — nothing changed" in capsys.readouterr().out
    assert _snapshot(e) == before
    with Session(e) as s:
        assert s.get(Campaign, cid).status == "running"


def test_script_adds_reopens_and_resumes(world, capsys):
    e, ids = world
    cid = _live_33(e, ids)
    with Session(e) as s:
        baseline = s.get(Campaign, cid).bounce_baseline
    assert _add(cid, "--apply") == 0
    out = capsys.readouterr().out
    assert "ADDED email 2" in out and "re-opened 2 buyer(s)" in out and "RESUMED campaign" in out
    assert f"approve {cid} --email 2 --apply" in out
    with Session(e) as s:
        c = s.get(Campaign, cid)
        assert c.status == "running" and c.sequence_version == 1 and c.bounce_baseline == baseline
        steps = CAMP.steps_for(s, c)
        assert [(st.step_index, st.delay_days, st.manual_review) for st in steps] == [(0, 0, False), (1, 7, True)]
        for r in s.exec(select(CampaignRecipient)).all():
            if r.current_step == 1:
                sent_at = s.exec(select(CampaignSend).where(CampaignSend.recipient_id == r.id)).one().sent_at
                assert r.status == "sent" and r.next_action_at == sent_at + timedelta(days=7)
            else:
                assert r.status == "pending"
        assert s.exec(select(WorkItem).where(WorkItem.type == "campaign_paused")).first() is None   # no task raised
        actions = [a.action for a in s.exec(select(AuditLog).where(AuditLog.entity_id == cid)).all()]
        assert {"status_paused", "append_step", "reopen_completed", "status_running"} <= set(actions)
    with Session(e, autoflush=False) as s:
        assert DRY.check(s, s.get(Campaign, cid))["errors"] == {}          # the real follow-up passes the dry run
    assert _add(cid, "--apply") == 0                                       # a re-run adds nothing twice
    assert "already has this text — it is not added again" in capsys.readouterr().out
    with Session(e) as s:
        assert len(CAMP.steps_for(s, s.get(Campaign, cid))) == 2
        assert s.get(Campaign, cid).status == "running"
    # the held email waits through the next weeks, then 'approve' releases it — as a reply in each thread
    _cycle(MON + timedelta(days=1))                                       # the third buyer's email 1
    _cycle(MON + timedelta(days=7, hours=1))
    assert len(FakeSMTP.sent) == 3
    assert FU.main(["approve", str(cid), "--email", "2"]) == 0
    assert "DRY RUN" in capsys.readouterr().out
    with Session(e) as s:
        assert CAMP.steps_for(s, s.get(Campaign, cid))[1].manual_review
    assert FU.main(["approve", str(cid), "--email", "2", "--apply"]) == 0
    assert "APPROVED: email 2 approved" in capsys.readouterr().out
    _cycle(MON + timedelta(days=7, hours=2))
    follow = FakeSMTP.sent[3:]
    assert sorted(m["To"] for m in follow) == ["buyer0@b0.example", "buyer1@b1.example"]
    with Session(e) as s:
        for m in follow:
            assert str(m["Subject"]) == "Re: " + SUBJECT_1 and m["In-Reply-To"] == _mid_of(s, m["To"], 0)
    _cycle(MON + timedelta(days=8, hours=2))                               # the third: its own email 1 + 7 days
    assert [m["To"] for m in FakeSMTP.sent[5:]] == ["buyer2@b2.example"]


def test_script_replaces_a_held_draft_and_never_stacks_a_new_one(world, capsys, tmp_path):
    e, ids = world
    cid = _live_33(e, ids)
    assert _add(cid, "--apply") == 0
    (tmp_path / FOLLOWUP / "body.txt").write_text("Hi {company} team,\n\nA shorter note.\n\nBest regards,\n")
    (tmp_path / FOLLOWUP / "body.html").unlink()                         # the founder revised the draft
    capsys.readouterr()
    before = _snapshot(e)
    assert _add(cid, "--apply") == 2                                       # it would queue behind the held email 2
    assert "REFUSED: email 2 is still held and never sent" in capsys.readouterr().out
    assert _snapshot(e) == before                                          # refused up front: never even paused
    assert _add(cid, "--replace") == 0
    assert "replace the text of email 2" in capsys.readouterr().out and _snapshot(e) == before   # dry run
    assert _add(cid, "--replace", "--apply") == 0
    out = capsys.readouterr().out
    assert "REPLACED the text of email 2" in out and f"RESUMED campaign #{cid}" in out
    with Session(e) as s:
        c = s.get(Campaign, cid)
        st = CAMP.steps_for(s, c)
        assert c.status == "running" and len(st) == 2 and st[1].manual_review and st[1].body_html == ""
        assert st[1].body == "Hi {company} team,\n\nA shorter note.\n\nBest regards,"


def test_script_leaves_the_campaign_paused_when_it_cannot_restart(world, capsys, monkeypatch):
    e, ids = world
    cid = _live_33(e, ids)
    real = CAMP.start_problems

    def picky(s, c):                                                       # fine before, broken after the append
        return real(s, c) + (["the mailbox lost its password"] if len(CAMP.steps_for(s, c)) > 1 else [])
    monkeypatch.setattr(CAMP, "start_problems", picky)
    assert _add(cid, "--apply") == 1
    out = capsys.readouterr().out
    assert f"CAMPAIGN #{cid} IS PAUSED" in out and "the mailbox lost its password" in out
    with Session(e) as s:
        c = s.get(Campaign, cid)
        assert c.status == "paused" and c.pause_reason == ""
        assert len(CAMP.steps_for(s, c)) == 2
    _cycle(MON + timedelta(days=1))
    assert len(FakeSMTP.sent) == 2                                         # nothing goes out while paused


def test_script_reopen_and_approve_refuse_safely(world, capsys):
    e, ids = world
    cid = _live_33(e, ids)
    capsys.readouterr()
    assert FU.main(["reopen", str(cid)]) == 0
    out = capsys.readouterr().out
    assert "re-open: 0 buyer(s)" in out and "left alone: 2 × no next email" in out and "DRY RUN" in out
    assert FU.main(["approve", str(cid), "--email", "2", "--apply"]) == 2
    assert "there is no email 2" in capsys.readouterr().out
    assert FU.main(["approve", "999", "--email", "2"]) == 2
    assert FU.main(["add", str(cid), "--template", FOLLOWUP, "--delay-days", "0", "--hold", "--apply"]) == 2
    assert "--delay-days must be at least 1" in capsys.readouterr().out
