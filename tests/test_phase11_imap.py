"""Phase 11 — the IMAP poller never marks a person's mail as read (read-only mailbox + PEEK fetches + a processed-
message ledger), baselines on its first run, still processes replies a person already opened, reads HTML-only
replies, and never mistakes our quoted footer for an unsubscribe."""
from email.message import EmailMessage

import pytest
from sqlalchemy import text
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine, select

from app import inbound_email as IE
from app import outreach_events as OE
from app import suppression as SUP
from app.campaign_render import OPT_OUT_LINE
from app.models import InboundSeen, Lead, Outreach, StageEvent, WorkItem


class FakeIMAP:
    """A tiny in-memory IMAP server. Fails the test on anything that could change a message's flags."""
    box = []                   # list of raw messages (bytes); index+1 = sequence number
    specs, selected_readonly, stores = [], [], []

    def __init__(self, host, port, timeout=None):
        assert timeout, "the IMAP connection must have a timeout"

    def login(self, user, pw):
        return "OK", [b""]

    def select(self, mailbox, readonly=False):
        FakeIMAP.selected_readonly.append(readonly)
        return "OK", [str(len(FakeIMAP.box)).encode()]

    def search(self, charset, *criteria):
        assert criteria[0] == "SINCE"
        return "OK", [" ".join(str(i + 1) for i in range(len(FakeIMAP.box))).encode()]

    def fetch(self, num, spec):
        FakeIMAP.specs.append(spec)
        assert "PEEK" in spec, "a non-PEEK fetch would mark the message as read"
        raw = FakeIMAP.box[int(num) - 1]
        if "HEADER.FIELDS" in spec:
            head = raw.split(b"\n\n", 1)[0].split(b"\r\n\r\n", 1)[0]
            return "OK", [(b"1 (BODY[HEADER.FIELDS] {1}", head + b"\r\n\r\n"), b")"]
        return "OK", [(b"1 (BODY[] {1}", raw), b")"]

    def store(self, *a):
        FakeIMAP.stores.append(a)
        raise AssertionError("the poller must never change flags")

    def logout(self):
        return "BYE", [b""]


def _msg(frm, subject, body="", html="", mid="", irt=""):
    m = EmailMessage()
    m["From"], m["To"], m["Subject"] = frm, "info@qmat.example", subject
    if mid:
        m["Message-ID"] = mid
    if irt:
        m["In-Reply-To"] = irt
    if body:
        m.set_content(body)
        if html:
            m.add_alternative(html, subtype="html")
    else:
        m.set_content(html, subtype="html")
    return m.as_bytes()


@pytest.fixture
def ctx(monkeypatch):
    e = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    SQLModel.metadata.create_all(e)
    with e.connect() as c:
        c.execute(text("CREATE UNIQUE INDEX IF NOT EXISTS uq_workitem_idem_open ON workitem(idempotency_key) "
                       "WHERE idempotency_key != '' AND status IN ('open','in_progress','waiting')"))
        c.commit()
    FakeIMAP.box, FakeIMAP.specs, FakeIMAP.selected_readonly, FakeIMAP.stores = [], [], [], []
    monkeypatch.setattr(IE.imaplib, "IMAP4_SSL", FakeIMAP)
    for k, v in (("IMAP_ENABLED", True), ("IMAP_HOST", "imap.test"), ("IMAP_USER", "info@qmat.example"),
                 ("IMAP_PASSWORD", "pw")):
        monkeypatch.setattr(IE, k, v)
    monkeypatch.setattr(IE, "notify_buyer_reply", lambda *a, **k: None)
    monkeypatch.setattr(IE, "notify_bounce", lambda *a, **k: None)
    monkeypatch.setattr(IE, "send_message", lambda *a, **k: None)
    monkeypatch.setattr(IE, "enrich_lead", lambda *a, **k: None)
    with Session(e) as s:
        ld = Lead(product="Anchors", managed=True, seller_id=None, buyer_company="Acme", email="buyer@acme.example",
                  pipeline_stage="contacted", status="new")
        s.add(ld); s.commit(); s.refresh(ld)
        s.add(Outreach(lead_id=ld.id, direction="out", channel="email", recipient=ld.email, status="sent",
                       message_id="<sent-1@qmat.example>", subject="Offer for Acme"))
        s.commit()
        lid = ld.id
    return e, lid


def _poll(e):
    with Session(e) as s:
        return IE.poll_inbox(s, log=lambda *_: None)


def test_first_poll_baselines_then_only_new_mail_is_processed_never_marked_read(ctx):
    e, lid = ctx
    FakeIMAP.box.append(_msg("old@news.example", "Weekly digest", "old news", mid="<old-1@news>"))
    first = _poll(e)
    assert first["baseline"] == 1 and first["seen"] == 0
    FakeIMAP.box.append(_msg("buyer@acme.example", "Re: Offer for Acme", "Please send the price list.",
                             mid="<r-1@acme>", irt="<sent-1@qmat.example>"))
    second = _poll(e)
    assert second["threaded"] == 1 and second["baseline"] == 0
    assert _poll(e)["seen"] == 0                                   # nothing processed twice
    assert FakeIMAP.stores == [] and all(FakeIMAP.selected_readonly)
    assert all("PEEK" in sp for sp in FakeIMAP.specs)
    with Session(e) as s:
        assert len(s.exec(select(Outreach).where(Outreach.direction == "in")).all()) == 1
        keys = {r.message_key for r in s.exec(select(InboundSeen)).all()}
        assert {"<old-1@news>", "<r-1@acme>", "__baseline__"} <= keys


def test_reply_quoting_our_footer_is_not_an_unsubscribe_and_advances_the_stage(ctx):
    e, lid = ctx
    _poll(e)                                                        # baseline an empty box
    body = ("Please send the price list for 10,000 pcs.\n\n"
            "On Mon, 5 Oct 2026 at 10:00, Qmat Trading <info@qmat.example> wrote:\n"
            "> Hello Acme team.\n> -- \n> Qmat Trading LLC\n> " + OPT_OUT_LINE + "\n")
    FakeIMAP.box.append(_msg("buyer@acme.example", "Re: Offer for Acme", body, mid="<r-2@acme>",
                             irt="<sent-1@qmat.example>"))
    assert _poll(e)["threaded"] == 1
    with Session(e) as s:
        assert not SUP.is_suppressed(s, "buyer@acme.example")
        ld = s.get(Lead, lid)
        assert ld.pipeline_stage == "responded"
        assert s.exec(select(StageEvent).where(StageEvent.lead_id == lid, StageEvent.to_stage == "responded")).first()


def test_html_only_unsubscribe_is_honoured(ctx):
    e, lid = ctx
    _poll(e)
    FakeIMAP.box.append(_msg("buyer@acme.example", "Re: Offer for Acme",
                             html="<div>Please unsubscribe us.</div><div class='gmail_quote'>old</div>",
                             mid="<r-3@acme>", irt="<sent-1@qmat.example>"))
    _poll(e)
    with Session(e) as s:
        assert SUP.is_suppressed(s, "buyer@acme.example")
        assert s.get(Lead, lid).pipeline_stage == "contacted"          # an opt-out never advances the funnel


def test_bounce_is_processed_once(ctx):
    e, lid = ctx
    _poll(e)
    dsn = _msg("mailer-daemon@googlemail.com", "Delivery Status Notification (Failure)",
               "Address not found. 550 5.1.1 The email account buyer@acme.example does not exist.",
               mid="<dsn-1@google>")
    FakeIMAP.box.append(dsn)
    assert _poll(e)["bounced"] == 1
    assert _poll(e)["bounced"] == 0
    with Session(e) as s:
        assert SUP.is_suppressed(s, "buyer@acme.example")


def test_newsletters_make_no_task_but_unmatched_optouts_are_honoured(ctx):
    e, lid = ctx
    _poll(e)
    FakeIMAP.box.append(_msg("promo@shop.example", "Big sale", "50% off", mid="<n-1@shop>"))
    FakeIMAP.box.append(_msg("alias@acme-group.example", "unsubscribe", "This message was sent by Gmail.",
                             mid="<u-1@acme>"))
    out = _poll(e)
    assert out["ignored"] == 1 and out["unsubscribed"] == 1
    with Session(e) as s:
        assert SUP.is_suppressed(s, "alias@acme-group.example")
        items = s.exec(select(WorkItem)).all()
        assert [w.title for w in items] == ["Unsubscribe request from an unmatched address"]


@pytest.mark.parametrize("body", [
    "Yes, interested.\n\nOn Tue, Oct 6, 2026 at 9:14 AM Qmat <info@qmat.example> wrote:\n> " + OPT_OUT_LINE,
    "Yes, interested.\n\nFrom: Qmat Trading <info@qmat.example>\nSent: Tuesday, October 6, 2026 9:14\n"
    "Subject: Offer\n\n" + OPT_OUT_LINE,
    "Yes, interested.\n\nAm Di., 6. Okt. 2026 um 09:14 Uhr schrieb Qmat <info@qmat.example>:\n" + OPT_OUT_LINE,
    "Yes, interested.\n-----Original Message-----\n" + OPT_OUT_LINE,
    "Yes, interested.\n\n" + OPT_OUT_LINE,
])
def test_reply_text_keeps_only_the_buyers_words(body):
    assert OE.reply_text(body) == "Yes, interested."
    assert not OE.is_unsubscribe("Re: Offer", OE.reply_text(body))
