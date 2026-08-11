"""Tests for the auto follow-up sequence + bounce detection."""
from datetime import datetime, timedelta

from sqlmodel import Session, SQLModel, create_engine

from app.followups import process_followups
from app.inbound_email import detect_bounce
from app.models import Lead, Outreach
from app.outreach import add_business_days, followup_message


def _mem_session():
    eng = create_engine("sqlite://")
    SQLModel.metadata.create_all(eng)
    return Session(eng)


def test_add_business_days_skips_weekends():
    start = datetime(2026, 1, 5, 12, 0)
    r = add_business_days(start, 5)
    assert r.weekday() < 5                       # never lands on Sat/Sun
    bd, c = 0, start
    while c.date() < r.date():
        c += timedelta(days=1)
        if c.weekday() < 5:
            bd += 1
    assert bd == 5                               # exactly 5 business days later


def test_followup_message_threaded_and_soft():
    lead = Lead(buyer_company="ACME", dest_country="IQ")
    s1, b1 = followup_message(lead, 1, "KIMIEL - honey supply")
    assert s1 == "Re: KIMIEL - honey supply" and "following up" in b1.lower()
    s2, b2 = followup_message(lead, 2, "Re: KIMIEL - honey supply")   # de-dupes an existing Re:
    assert s2 == "Re: KIMIEL - honey supply" and "final follow-up" in b2.lower()


def test_detect_bounce_extracts_recipient():
    raw = (b"From: Mail Delivery Subsystem <mailer-daemon@googlemail.com>\r\n"
           b"Subject: Delivery Status Notification (Failure)\r\n"
           b"Content-Type: multipart/report; report-type=delivery-status; boundary=B\r\n\r\n"
           b"--B\r\nContent-Type: text/plain\r\n\r\nyour message could not be delivered\r\n"
           b"--B\r\nContent-Type: message/delivery-status\r\n\r\n"
           b"Final-Recipient: rfc822; nobody@invalid-domain-xyz.com\r\n"
           b"Diagnostic-Code: smtp; 550 5.1.1 user unknown\r\n\r\n--B--\r\n")
    res = detect_bounce(raw)
    assert res is not None
    rcpt, reason = res
    assert rcpt == "nobody@invalid-domain-xyz.com" and "550" in reason


def test_detect_bounce_ignores_normal_reply():
    raw = b"From: buyer@acme.com\r\nSubject: Re: your offer\r\n\r\nInterested, please send details.\r\n"
    assert detect_bounce(raw) is None


def test_process_followups_sends_and_advances(monkeypatch):
    monkeypatch.setattr("app.followups.send_email", lambda *a, **k: (True, "", "<fu-mid@kimiel.com>"))
    s = _mem_session()
    lead = Lead(product="Honey", buyer_company="ACME", email="buyer@acme.com", dest_country="IQ",
                status="new", next_action_at=datetime.utcnow() - timedelta(minutes=1),
                next_action_note="followup-1")
    s.add(lead); s.commit(); s.refresh(lead)
    s.add(Outreach(lead_id=lead.id, direction="out", channel="email", status="sent",
                   message_id="<orig@kimiel.com>", subject="KIMIEL - honey supply offer"))
    s.commit()

    summary = process_followups(s, now=datetime.utcnow(), log=lambda *_: None)

    assert summary["sent"] == 1
    s.refresh(lead)
    assert lead.next_action_note == "followup-2"          # advanced to the next step
    assert lead.next_action_at > datetime.utcnow()        # rescheduled into the future
    outs = [o for o in lead_outreach(s, lead.id) if o.direction == "out"]
    assert any(o.subject.startswith("Re: ") for o in outs)  # follow-up went as a threaded reply


def test_process_followups_skips_replied_lead(monkeypatch):
    calls = {"n": 0}
    monkeypatch.setattr("app.followups.send_email",
                        lambda *a, **k: (calls.__setitem__("n", calls["n"] + 1), (True, "", "<x>"))[1])
    s = _mem_session()
    lead = Lead(product="Honey", buyer_company="ACME", email="buyer@acme.com", status="new",
                next_action_at=datetime.utcnow() - timedelta(minutes=1), next_action_note="followup-1",
                buyer_replied_at=datetime.utcnow())      # already replied -> must be skipped
    s.add(lead); s.commit()
    summary = process_followups(s, now=datetime.utcnow(), log=lambda *_: None)
    assert summary["due"] == 0 and calls["n"] == 0


def lead_outreach(session, lead_id):
    from sqlmodel import select
    return session.exec(select(Outreach).where(Outreach.lead_id == lead_id)).all()
