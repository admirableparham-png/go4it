"""Phase 11 — regression tests for the pre-release review findings (one test per finding)."""
import imaplib
import smtplib
import socket
from datetime import datetime
from email import message_from_bytes, policy
from email.message import EmailMessage

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
from app.models import (Campaign, CampaignRecipient, CampaignSend, Lead, MailAccount, ServiceRequest, User,
                        UserProfile)

NOW = datetime(2026, 10, 5, 10, 0)


def _mk(s, email, role, account_class, role_key, name=None):
    u = User(email=email, name=name or email.split("@")[0], role=role, active=True, password_hash=hash_password("pw"))
    s.add(u); s.commit(); s.refresh(u)
    s.add(UserProfile(user_id=u.id, account_class=account_class, role_key=role_key, company="Sharkline Trading & Sons",
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
                    "CREATE UNIQUE INDEX IF NOT EXISTS uq_campaignsend_crvs ON "
                    "campaignsend(campaign_id,recipient_id,sequence_version,step_index)",
                    "CREATE UNIQUE INDEX IF NOT EXISTS uq_userprofile_user ON userprofile(user_id) WHERE user_id IS NOT NULL"):
            c.execute(text(ddl))
        c.commit()
    for mod in (main, worker):
        monkeypatch.setattr(mod, "engine", e)
    monkeypatch.setattr(config, "IMAP_ENABLED", True)
    monkeypatch.setattr(config, "IMAP_INTERVAL", 120)
    monkeypatch.setattr(config, "IMAP_USER", "info@qmat.example")
    monkeypatch.setattr(OUT, "mail_decrypt", lambda enc: "app-password" if enc else "")
    with Session(e) as s:
        f = _mk(s, "founder@t", "admin", "internal", "founder")
        _mk(s, "analyst@t", "admin", "internal", "analyst")
        seller = _mk(s, "sharks@t", "agent", "seller", "seller", name="Sharkline")
        mb = MailAccount(user_id=f.id, email="info@qmat.example", from_name="Qmat Trading", admin_owned=True,
                         active=True, smtp_password_enc="enc", sender_company="Qmat Trading LLC",
                         postal_address="Dubai, UAE", daily_limit=100)
        sr = ServiceRequest(request_type="buyer_hunt", product="Anchors", status="done", owner_id=seller.id,
                            requester_id=seller.id)
        s.add(mb); s.add(sr); s.commit(); s.refresh(mb); s.refresh(sr)
        c = Campaign(name="C", tenant_id=seller.id, request_id=sr.id, owner_id=f.id, mailbox_id=mb.id, status="draft",
                     daily_limit=100, send_days="0,1,2,3,4,5,6", send_window_start=0, send_window_end=24)
        s.add(c); s.commit(); s.refresh(c)
        CAMP.set_sequence(s, c, [{"subject": "Offer for {company}", "body": "Hello {company}."}])
        c.status = "running"; s.add(c); s.commit()
        return e, {"founder": f.id, "seller": seller.id, "mailbox": mb.id, "campaign": c.id, "request": sr.id}


def _buyers(s, ids, n):
    out = []
    for i in range(n):
        ld = Lead(product="Anchors", managed=True, seller_id=ids["seller"], request_id=ids["request"],
                  buyer_company=f"Buyer {i}", dest_country="PL", email=f"b{i}@x.example")
        s.add(ld); s.commit(); s.refresh(ld)
        r = CampaignRecipient(campaign_id=ids["campaign"], tenant_id=ids["seller"], lead_id=ld.id, to_email=ld.email)
        s.add(r); s.commit(); s.refresh(r)
        out.append(r)
    return out


def _step(s, ids, **fields):
    st = CAMP.steps_for(s, s.get(Campaign, ids["campaign"]))[0]
    for k, v in fields.items():
        setattr(st, k, v)
    return st


# 1 — the seller's identity is caught in HTML (escaped &, split by tags, &nbsp;), in the From name/footer, and inside
#     longer tokens such as a domain
@pytest.mark.parametrize("html", [
    "<html><body><p>By Sharkline Trading &amp; Sons</p></body></html>",
    "<html><body><p><b>Sharkline</b> Trading</p></body></html>",
    "<html><body><p>Sharkline&nbsp;Trading</p></body></html>",
    '<html><body><a href="https://sharklinetrading.com">catalogue</a></body></html>',
])
def test_seller_name_in_html_is_caught(ctx, html):
    e, ids = ctx
    with Session(e) as s:
        st = _step(s, ids, body_html=html)
        errs = CR.template_problems(s, s.get(Campaign, ids["campaign"]), st, s.get(MailAccount, ids["mailbox"]))
        assert any("names the seller" in x for x in errs), errs


def test_seller_name_in_from_or_footer_blocks_the_start(ctx):
    e, ids = ctx
    with Session(e) as s:
        mb = s.get(MailAccount, ids["mailbox"]); mb.from_name = "Sharkline"; s.add(mb); s.commit()
        assert any("names the seller" in p for p in CAMP.start_problems(s, s.get(Campaign, ids["campaign"])))


# 2 — connection problems are the mailbox's: slot refunded, no attempt charged, nothing paused, pacing respected;
#     "too many login attempts" pauses the mailbox
def test_connect_failures_charge_no_buyer_and_keep_the_pace(ctx, monkeypatch):
    e, ids = ctx
    with Session(e) as s:
        _buyers(s, ids, 5)

    def refuse(*a, **k):
        raise ConnectionRefusedError(61, "Connection refused")
    monkeypatch.setattr(OUT.smtplib, "SMTP", refuse)
    monkeypatch.setattr(worker, "CAMPAIGN_SEND_MAX_PER_RUN", 1)
    worker.run_campaign_send(now=NOW)
    with Session(e) as s:
        mb = s.get(MailAccount, ids["mailbox"])
        assert not mb.paused and mb.sent_today == 0 and mb.last_send_error.startswith("transient")
        rows = s.exec(select(CampaignSend)).all()
        assert len(rows) == 1                                           # ONE attempt this cycle (pacing held)
        assert rows[0].status == "retryable" and rows[0].attempt_count == 0 and rows[0].next_attempt_at > NOW
        assert all(r.status == "pending" for r in s.exec(select(CampaignRecipient)).all())


def test_too_many_login_attempts_pauses_the_mailbox(ctx, monkeypatch):
    e, ids = ctx

    class Busy:
        def __init__(self, *a, **k): pass
        def starttls(self, **k): pass
        def login(self, *a): raise smtplib.SMTPAuthenticationError(454, b"4.7.0 Too many login attempts, try later")
        def quit(self): pass
    monkeypatch.setattr(OUT.smtplib, "SMTP", Busy)
    with Session(e) as s:
        (r,) = _buyers(s, ids, 1)
        out = CAMP.send_step(s, s.get(Campaign, ids["campaign"]), r, s.get(MailAccount, ids["mailbox"]), NOW)
        assert out["status"] == "mailbox_failed" and out["kind"] == "quota"
        assert s.get(MailAccount, ids["mailbox"]).paused


# 3 — a resumed campaign is judged only on what it sends after the resume
def test_bounce_breaker_can_be_cleared_by_a_resume(ctx):
    e, ids = ctx
    with Session(e) as s:
        rs = _buyers(s, ids, 20)
        for r in rs[:2]:
            r.status = "hard_bounced"; s.add(r)
        for r in rs[2:]:
            r.status = "sent"; s.add(r)
        s.commit()
        c = s.get(Campaign, ids["campaign"])
        assert CAMP.bounce_breaker(s, c) and c.status == "paused"
        CAMP.transition(s, c, "running"); s.commit()
        assert c.bounce_baseline == "2:20"
        assert not CAMP.bounce_breaker(s, c) and c.status == "running"


# 4 — a dropped IMAP connection never loses a message; the baseline isn't written over a gap
class FlakyIMAP:
    box, fail_body_once = [], True

    def __init__(self, host, port, timeout=None): pass
    def login(self, *a): return "OK", [b""]
    def select(self, mailbox, readonly=False): return "OK", [b"1"]
    def search(self, *a): return "OK", [" ".join(str(i + 1) for i in range(len(FlakyIMAP.box))).encode()]
    def logout(self): return "BYE", [b""]

    def fetch(self, num, spec):
        raw = FlakyIMAP.box[int(num) - 1]
        if "HEADER.FIELDS" in spec:
            return "OK", [(b"1 (BODY[HEADER] {1}", raw.split(b"\n\n", 1)[0] + b"\n\n"), b")"]
        if FlakyIMAP.fail_body_once:
            FlakyIMAP.fail_body_once = False
            raise imaplib.IMAP4.abort("socket error: EOF")
        return "OK", [(b"1 (BODY[] {1}", raw), b")"]


def _raw(frm, subj, body, mid, irt=""):
    m = EmailMessage()
    m["From"], m["Subject"], m["Message-ID"] = frm, subj, mid
    if irt:
        m["In-Reply-To"] = irt
    m.set_content(body)
    return m.as_bytes()


def test_a_dropped_connection_never_loses_an_unsubscribe(ctx, monkeypatch):
    e, ids = ctx
    for k, v in (("IMAP_ENABLED", True), ("IMAP_HOST", "imap.test"), ("IMAP_USER", "info@qmat.example"),
                 ("IMAP_PASSWORD", "pw")):
        monkeypatch.setattr(IE, k, v)
    monkeypatch.setattr(IE.imaplib, "IMAP4_SSL", FlakyIMAP)
    monkeypatch.setattr(IE, "notify_buyer_reply", lambda *a, **k: None)
    FlakyIMAP.box, FlakyIMAP.fail_body_once = [], True
    with Session(e) as s:
        assert IE.poll_inbox(s, log=lambda *_: None)["baseline"] == 0             # empty box → baseline marker
        (r,) = _buyers(s, ids, 1)
    FlakyIMAP.box.append(_raw("b0@x.example", "Re: Offer", "Please unsubscribe us.", "<u1@b>"))
    with Session(e) as s:
        assert IE.poll_inbox(s, log=lambda *_: None)["errors"] == 1               # dropped mid-fetch
        assert not SUP.is_suppressed(s, "b0@x.example")
        IE.poll_inbox(s, log=lambda *_: None)                                    # retried next poll
        assert SUP.is_suppressed(s, "b0@x.example")


# 5 — a display name with a comma stays one address
def test_from_name_with_a_comma_is_one_address(ctx, monkeypatch):
    e, ids = ctx
    captured = {}

    class Fake:
        def __init__(self, *a, **k): pass
        def starttls(self, **k): pass
        def login(self, *a): pass
        def quit(self): pass
        def send_message(self, msg): captured["raw"] = msg.as_bytes()
    monkeypatch.setattr(OUT.smtplib, "SMTP", Fake)
    with Session(e) as s:
        mb = s.get(MailAccount, ids["mailbox"]); mb.from_name = "Qmat Al Saha Co., LLC"
        ok, err, _ = OUT.send_via_account(mb, "b@x.example", "S", "t")
        assert ok, err
    msg = message_from_bytes(captured["raw"], policy=policy.default)
    assert [a.addr_spec for a in msg["From"].addresses] == ["info@qmat.example"]
    assert msg["From"].addresses[0].display_name == "Qmat Al Saha Co., LLC"


# 6 — no request, no audience
def test_a_campaign_without_a_request_cannot_enrol_or_start(ctx):
    e, ids = ctx
    with Session(e) as s:
        c = s.get(Campaign, ids["campaign"]); c.request_id = None; s.add(c); s.commit()
        assert any("not linked to a request" in p for p in CAMP.start_problems(s, c))
    cl = TestClient(main.app)
    assert cl.post("/login", data={"email": "founder@t", "password": "pw"}, follow_redirects=False).status_code == 303
    cl.post(f"/campaigns/{ids['campaign']}/audience", data={"do": "enroll", "expected": "0"}, follow_redirects=False)
    with Session(e) as s:
        assert s.exec(select(CampaignRecipient)).all() == []


# 7 — a newsletter that merely contains "unsubscribe" is ignored, not suppressed
def test_newsletters_are_not_opt_outs(ctx):
    e, ids = ctx
    with Session(e) as s:
        out = IE.handle_inbound(s, "news@vendor.example", "October deals",
                                "Big sale!\n\nTo unsubscribe click here.", "<n1@v>")
        assert out == "ignored" and not SUP.is_suppressed(s, "news@vendor.example")
        assert IE.handle_inbound(s, "alias@b.example", "unsubscribe", "", "<n2@v>") == "unsubscribed"


# 8 — replies must be read from the SAME mailbox the campaign sends from
def test_start_needs_the_polled_mailbox(ctx, monkeypatch):
    e, ids = ctx
    monkeypatch.setattr(config, "IMAP_USER", "other@qmat.example")
    with Session(e) as s:
        assert any("are not read" in p for p in CAMP.start_problems(s, s.get(Campaign, ids["campaign"])))


# 9 — bottom-posted opt-outs count; our unsubscribe link inside an unknown quote header never does
def test_reply_text_reads_below_the_quote_and_ignores_our_link():
    body = ("\nOn Mon, 5 Oct 2026, Qmat <info@qmat.example> wrote:\n> Hello Acme.\n> " + CR.OPT_OUT_LINE +
            "\n\nPlease remove me from your list.")
    assert OE.is_unsubscribe("Re: Offer", OE.reply_text(body))
    turkish = ("Fiyat listesini gönderir misiniz?\n\n5 Eki 2026 Pzt, 10:00 tarihinde Qmat şunu yazdı:\n"
               "Hello Acme.\nStop receiving these emails\n<mailto:info@qmat.example?subject=unsubscribe>")
    assert not OE.is_unsubscribe("Re: Offer", OE.reply_text(turkish))


# 10 — editing what buyers read needs the campaign permission
def test_analyst_cannot_edit_the_emails(ctx):
    e, ids = ctx
    cl = TestClient(main.app)
    assert cl.post("/login", data={"email": "analyst@t", "password": "pw"}, follow_redirects=False).status_code == 303
    r = cl.post(f"/campaigns/{ids['campaign']}/sequence", data={"subjects": ["Hijack"], "bodies": ["x"],
                                                                "bodies_html": [""], "delays": ["0"], "confirm": "1"},
                follow_redirects=False)
    assert r.status_code == 403


# 11 — new credentials lift only an authentication pause
def test_credentials_do_not_lift_a_deliberate_pause(ctx, monkeypatch):
    e, ids = ctx
    monkeypatch.setattr(main, "verify_smtp", lambda *a: (True, ""))
    monkeypatch.setattr(main, "mail_encrypt", lambda pw: "enc2")
    with Session(e) as s:
        mb = s.get(MailAccount, ids["mailbox"]); mb.paused = True; mb.last_send_error = ""; s.add(mb); s.commit()
    cl = TestClient(main.app)
    assert cl.post("/login", data={"email": "founder@t", "password": "pw"}, follow_redirects=False).status_code == 303
    cl.post(f"/mail/{ids['mailbox']}/credentials", data={"app_password": "x"}, follow_redirects=False)
    with Session(e) as s:
        assert s.get(MailAccount, ids["mailbox"]).paused


# 12 — the IMAP connection always has a timeout (FakeIMAP in test_phase11_imap asserts it on every connect)
def test_imap_timeout_is_set():
    assert IE.IMAP_TIMEOUT and IE.IMAP_TIMEOUT <= 60
