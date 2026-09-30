"""Phase 11 — STRESS: ~470 synthetic buyers through the whole real pipeline (loader → preview → enrol → dry-run →
start → weeks of worker cycles on a simulated clock) with a fake SMTP server that builds and records every real MIME
message. Mid-run: Pause-All, a mailbox auth failure + reconnect, replies that quote the footer, unsubscribes, a
warm-up ramp day. Nothing leaves the process."""
import io
import smtplib
from collections import Counter, defaultdict
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
from app import permissions as P
from app import send_guard as SG
from app import suppression as SUP
from app.auth import hash_password
from app.models import (Campaign, CampaignRecipient, CampaignSend, Lead, MailAccount, Outreach, ServiceRequest, User,
                        UserProfile)
from scripts import campaign_dryrun as DRY
from scripts import load_managed_buyers as LMB

START = datetime(2026, 10, 5)                         # a Monday
COUNTRIES = ["PL", "IT", "CA", "ZA", "AE", "NZ", "GB", "IE", "SK", "ES"]
SUPPRESSED = {f"buyer{i}@b{i}.example" for i in range(100, 105)}
REJECTED = {f"buyer{i}@b{i}.example" for i in (200, 201)}
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
)
HTML = ('<!DOCTYPE html><html><head><style>td{font-family:Arial}</style></head><body><table width="600"><tr><td>'
        '<p>Dear <b>{company}</b> team,</p><p>We supply metal drywall anchors to importers in {country}.</p>'
        '<a href="https://qmat.example/anchors">Catalogue</a><script>x()</script></td></tr></table></body></html>')


class FakeSMTP:
    sent, logins_failed, clock, fail_auth, on_send = [], 0, None, False, None

    def __init__(self, host, port, timeout=None):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def starttls(self, context=None):
        pass

    def login(self, user, pw):
        if FakeSMTP.fail_auth:
            FakeSMTP.logins_failed += 1
            raise smtplib.SMTPAuthenticationError(535, b"5.7.8 Username and Password not accepted.")

    def send_message(self, msg):
        if msg["To"] in REJECTED:
            raise smtplib.SMTPRecipientsRefused({msg["To"]: (550, b"5.1.1 The email account does not exist")})
        FakeSMTP.sent.append((FakeSMTP.clock, BytesParser(policy=policy.default).parsebytes(msg.as_bytes())))
        if FakeSMTP.on_send:
            FakeSMTP.on_send()


def _buyers():
    rows = []
    for i in range(450):
        name = f"Hurtownia Żelazna Śruba {i}" if i < 40 else f"Buyer Co {i}"
        rows.append({"company": name, "dest_iso": COUNTRIES[i % 10], "city": "Warsaw",
                     "email": f"buyer{i}@b{i}.example", "buys": ["anchors"], "match_score": 500 - i})
    rows += [{"company": f"US Box {i}", "dest_iso": "US", "email": f"us{i}@us.example"} for i in range(12)]
    rows += [{"company": f"Dup {i}", "dest_iso": "PL", "email": f"buyer{i}@b{i}.example"} for i in range(8)]
    rows += [{"company": f"Junk {i}", "dest_iso": "IT", "email": f"www.junk{i}.example/contact"} for i in range(6)]
    rows += [{"company": f"NoMail {i}", "dest_iso": "ES", "email": ""} for i in range(4)]
    return rows


@pytest.fixture
def world(monkeypatch):
    e = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    SQLModel.metadata.create_all(e)
    with e.connect() as c:
        for ddl in IDX:
            c.execute(text(ddl))
        c.commit()
    for mod in (main, worker, LMB):
        monkeypatch.setattr(mod, "engine", e)
    monkeypatch.setattr(LMB, "init_db", lambda: None)
    monkeypatch.setattr(OUT.smtplib, "SMTP", FakeSMTP)
    monkeypatch.setattr(OUT, "mail_decrypt", lambda enc: "app-password" if enc else "")
    monkeypatch.setattr(config, "IMAP_ENABLED", True)
    monkeypatch.setattr(config, "IMAP_INTERVAL", 120)
    monkeypatch.setattr(config, "IMAP_USER", "info@qmat.example")
    monkeypatch.setattr(worker, "CAMPAIGN_SEND_MAX_PER_RUN", 20)
    monkeypatch.setattr(worker, "CAMPAIGN_SEND_DEADLINE_SEC", 10_000)
    for name in ("notify_buyer_reply", "notify_bounce", "send_message", "enrich_lead"):
        monkeypatch.setattr(IE, name, lambda *a, **k: None)
    FakeSMTP.sent, FakeSMTP.logins_failed, FakeSMTP.fail_auth, FakeSMTP.on_send = [], 0, False, None
    with Session(e) as s:
        founder = User(email="founder@t", name="Founder", role="admin", active=True, password_hash="x")
        seller = User(email="sharks@t", name="sharkline", role="agent", active=True, password_hash=hash_password("pw"))
        s.add(founder); s.add(seller); s.commit(); s.refresh(founder); s.refresh(seller)
        s.add(UserProfile(user_id=seller.id, account_class="seller", role_key="seller", company="TRSHARKS",
                          scope=P.ROLE_TEMPLATES["seller"]["scope"], account_status="active"))
        sr = ServiceRequest(tracking_code="SR-202608-0001", request_type="buyer_hunt", product="Drywall anchors",
                            status="done", owner_id=seller.id, requester_id=seller.id)
        mb = MailAccount(user_id=founder.id, email="info@qmat.example", from_name="Qmat Trading", admin_owned=True,
                         active=True, smtp_password_enc="enc", daily_limit=200, sender_company="Qmat Trading LLC",
                         postal_address="Office 1, Test Tower\nDubai, UAE")
        s.add(sr); s.add(mb); s.commit(); s.refresh(sr); s.refresh(mb)
        for em in SUPPRESSED:
            SUP.suppress(s, em, "unsubscribe")
        s.commit()
        return e, {"seller": seller.id, "founder": founder.id, "request": sr.id, "mailbox": mb.id}


def test_stress_full_pipeline(world):
    e, ids = world
    # ---- load (confidential, US excluded) -------------------------------------------------------------------------
    with Session(e) as s:
        sr = s.get(ServiceRequest, ids["request"])
        rows, report = LMB.plan(s, sr, _buyers(), exclude_countries=["US"])
        LMB.load(s, sr, rows)
        s.commit()
        assert len(rows) == 450 + 8 + 6 + 4                           # dups/junk/no-mail kept, address-less
    # ---- campaign: preview → enrol with the seen count → dry-run → start ------------------------------------------
    with Session(e) as s:
        c = Campaign(name="TRSHARKS anchors", tenant_id=ids["seller"], request_id=ids["request"],
                     owner_id=ids["founder"], mailbox_id=ids["mailbox"], status="draft", daily_limit=50,
                     send_window_start=8, send_window_end=18, send_days="0,1,2,3,4")
        s.add(c); s.commit(); s.refresh(c)
        CAMP.set_sequence(s, c, [
            {"subject": "Drywall anchors for {company}", "body": "", "body_html": HTML},
            {"subject": "Following up — anchors for {country}", "body": "Just checking in, {company}.",
             "delay_days": 3}])
        f = {"request_id": ids["request"]}
        prev = CAMP.audience_preview(s, c, f)
        assert prev["final_eligible"] == 445 and prev["suppressed"] == 5 and prev["missing_email"] == 18
        assert CAMP.enroll(s, c, None, f, expected=444).get("error")    # stale count → nothing enrolled
        assert CAMP.enroll(s, c, None, f, expected=445)["created"] == 445
        assert CAMP.enroll(s, c, None, f, expected=0)["created"] == 0   # a double submit enrols nobody twice
        us = Lead(product="Drywall anchors", managed=True, seller_id=ids["seller"], request_id=ids["request"],
                  buyer_company="Sneaky US", dest_country="US", email="sneaky@us.example")
        s.add(us); s.commit(); s.refresh(us)
        s.add(CampaignRecipient(campaign_id=c.id, tenant_id=ids["seller"], lead_id=us.id, to_email=us.email))
        s.commit()
        cid = c.id
    with Session(e, autoflush=False) as s:
        rep = DRY.check(s, s.get(Campaign, cid), exclude_countries=["US"])
        assert list(rep["errors"]) == ["excluded country US"]            # the dry-run catches the stray US buyer
    with Session(e) as s:
        s.delete(s.exec(select(CampaignRecipient).where(CampaignRecipient.to_email == "sneaky@us.example")).one())
        s.commit()
    with Session(e, autoflush=False) as s:
        assert DRY.check(s, s.get(Campaign, cid), exclude_countries=["US"])["errors"] == {}
    with Session(e) as s:
        c = s.get(Campaign, cid)
        assert CAMP.start_problems(s, c) == []
        CAMP.transition(s, c, "running"); s.commit()

    # ---- weeks of worker cycles on a simulated clock --------------------------------------------------------------
    replies_done = False
    _events.fired = []
    for d in range(45):
        day = START + timedelta(days=d)
        with Session(e) as s:
            c = s.get(Campaign, cid)
            c.daily_limit = 10 if d == 1 else 50                          # Tuesday = warm-up ramp day
            s.add(c); s.commit()
            if c.status == "completed":
                break
        if d == 3 and not replies_done:
            _replies(e)
            replies_done = True
        for minute in [7 * 60 + 55] + list(range(8 * 60, 18 * 60, 5)) + [18 * 60]:
            t = day + timedelta(minutes=minute)
            FakeSMTP.clock = t
            event = _events(e, d, minute)
            res = worker.run_campaign_send(now=t)
            assert "error" not in res and res.get("errors", 0) == 0, res
            if 8 * 60 <= minute < 18 * 60 and res["sent"] == 0 and not event and day.weekday() < 5:
                break                                                     # today's cap reached

    # ---- assertions ----------------------------------------------------------------------------------------------
    sent = FakeSMTP.sent
    per_day, per_cycle, per_step_addr = Counter(), Counter(), Counter()
    mids = set()
    for t, m in sent:
        per_day[t.date()] += 1
        per_cycle[t] += 1
        assert t.weekday() < 5 and 8 <= t.hour < 18                       # window + weekdays only
        subj = str(m["Subject"])
        step = 1 if subj.startswith("Drywall anchors for") else 2
        per_step_addr[(m["To"], step)] += 1
        assert m["From"] == "Qmat Trading <info@qmat.example>"
        assert m["List-Unsubscribe"] == "<mailto:info@qmat.example?subject=unsubscribe>"
        txt = m.get_body(("plain",)).get_content()
        html = m.get_body(("html",)).get_content()
        assert CR.OPT_OUT_LINE in txt and "Qmat Trading LLC" in html and "Dubai, UAE" in txt
        for part in (subj, txt, html):
            assert "{" not in part.replace("td{font-family:Arial}", "") and "TRSHARKS" not in part
            assert not CR.INTERNAL_BRAND.search(part)
        assert "<script" not in html
        mids.add(m["Message-ID"])
    assert max(per_cycle.values()) <= 20
    assert per_day[(START + timedelta(days=1)).date()] <= 10              # ramp day
    assert all(n <= 50 for n in per_day.values())
    assert max(per_step_addr.values()) == 1                               # nobody got the same email twice
    addrs = {a for a, _ in per_step_addr}
    assert not addrs & (SUPPRESSED | REJECTED) and not any("us.example" in a or "junk" in a for a in addrs)
    assert any("Żelazna" in str(m["Subject"]) for _, m in sent)          # unicode subjects decode
    assert len(mids) == len(sent)
    assert _events.fired == ["pause", "auth"]
    assert FakeSMTP.logins_failed == 1                                    # the worker stopped at the first 535
    assert per_day[START.date()] <= 50 and per_cycle[START + timedelta(minutes=8 * 60 + 5)] == 5   # paused mid-cycle
    with Session(e) as s:
        outs = s.exec(select(Outreach).where(Outreach.campaign_id == cid, Outreach.direction == "out")).all()
        css = s.exec(select(CampaignSend).where(CampaignSend.status == "sent")).all()
        assert len(outs) == len(css) == len(sent)
        assert {o.message_id for o in outs} == {cs.rfc_message_id for cs in css} == mids
        assert s.get(Campaign, cid).status == "completed"
        rc = Counter(r.status for r in s.exec(select(CampaignRecipient)).all())
        assert rc["replied"] == 10 and rc["unsubscribed"] == 3 and rc["skipped"] == 2
        assert rc["completed"] == 445 - 10 - 3 - 2
        first_email = {a for a, st in per_step_addr if st == 1}
        for ld in s.exec(select(Lead).where(Lead.request_id == ids["request"])).all():
            if ld.email in first_email:
                assert ld.pipeline_stage in ("contacted", "responded")
        assert sum(1 for ld in s.exec(select(Lead)).all() if ld.pipeline_stage == "responded") == 10
        for em in _replies.unsubscribed:
            assert SUP.is_suppressed(s, em)
        mb = s.get(MailAccount, ids["mailbox"])
        assert not mb.paused
    # ---- the seller only ever sees anonymized progress ------------------------------------------------------------
    cl = TestClient(main.app)
    assert cl.post("/login", data={"email": "sharks@t", "password": "pw"}, follow_redirects=False).status_code == 303
    for path in ("/requests", "/leads", "/dashboard"):
        body = cl.get(path).text
        assert "Buyer Co 1" not in body and "b1.example" not in body and "Żelazna" not in body


def _events(e, d, minute):
    """Mid-run incidents. Returns True for a cycle that is EXPECTED to send nothing."""
    if d == 0 and minute == 8 * 60 + 5:              # Pause-All flips on after 5 sends of this cycle …
        _events.fired.append("pause")
        n0 = len(FakeSMTP.sent)

        def hook():
            if len(FakeSMTP.sent) - n0 == 5:
                with Session(e) as s:
                    SG.set_pause_all(s, True); s.commit()
        FakeSMTP.on_send = hook
        return True
    if d == 0 and minute == 8 * 60 + 10:             # … and nothing more goes out until it is lifted
        with Session(e) as s:
            assert SG.outreach_paused(s)
            SG.set_pause_all(s, False); s.commit()
        FakeSMTP.on_send = None
        return True
    if d == 2 and minute == 8 * 60:                  # the App Password stops working
        _events.fired.append("auth")
        FakeSMTP.fail_auth = True
        return True
    if d == 2 and minute == 8 * 60 + 5:              # founder re-enters it; the mailbox resumes
        FakeSMTP.fail_auth = False
        with Session(e) as s:
            mb = s.exec(select(MailAccount)).one()
            assert mb.paused and mb.last_send_error.startswith("auth")
            mb.paused, mb.last_send_error = False, ""
            s.add(mb); s.commit()
        return True
    return False


def _replies(e):
    """10 real replies that quote our footer + 3 unsubscribes, threaded by In-Reply-To like the IMAP poller does."""
    with Session(e) as s:
        outs = s.exec(select(Outreach).where(Outreach.direction == "out").order_by(Outreach.id)).all()
        assert len(outs) >= 13
        _replies.unsubscribed = []
        for i, o in enumerate(outs[:13]):
            quoted = "\n".join("> " + ln for ln in o.body.splitlines())
            words = "Please unsubscribe us." if i >= 10 else "Interested — please send prices for 10,000 pcs."
            body = f"{words}\n\nOn Mon, 5 Oct 2026 at 08:00, Qmat Trading <info@qmat.example> wrote:\n{quoted}"
            assert IE.handle_inbound(s, o.recipient, "Re: " + o.subject, body, f"<reply-{i}@buyer>",
                                     o.message_id) == "threaded"
            if i >= 10:
                _replies.unsubscribed.append(o.recipient)
