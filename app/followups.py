"""Auto follow-up sequence — the worker calls process_followups() on a timer.

Only ever touches leads whose FIRST email the founder already sent (that send stamped
next_action_note="followup-1" + next_action_at). It NEVER cold-emails a buyer on its own. Cadence:
  followup-1  -> send threaded FU#1, schedule FU#2 at +FOLLOWUP_BDAYS_2 business days
  followup-2  -> send threaded FU#2, schedule "call" at +FOLLOWUP_GRACE_BDAYS business days
  call        -> alert the founder to phone the buyer, stop
A buyer reply (inbound_email) clears next_action_at, so a replied lead never gets a follow-up.
"""
from datetime import datetime

from sqlmodel import Session, select

from .config import FOLLOWUP_BDAYS_2, FOLLOWUP_GRACE_BDAYS
from .models import Lead, Outreach
from .outreach import add_business_days, build_parts, followup_message, send_email
from .telegram import notify_needs_call, notify_outreach_sent, notify_send_failed

_STEPS = ["followup-1", "followup-2", "call"]


def _safe(fn, *a, **k):
    try:
        fn(*a, **k)
    except Exception:  # noqa: BLE001 - alerts are best-effort, never block the sweep
        pass


def _last_out_email(session: Session, lead_id: int):
    """The most recent SENT outbound email with a Message-ID — the thread anchor for a reply."""
    return session.exec(select(Outreach).where(
        Outreach.lead_id == lead_id, Outreach.direction == "out", Outreach.channel == "email",
        Outreach.message_id != "").order_by(Outreach.id.desc())).first()


def process_followups(session: Session, now=None, log=print) -> dict:
    """One sweep of due follow-ups. Returns a summary dict. Does NOT check FOLLOWUP_ENABLED (the worker
    wrapper does) so it is directly unit-testable."""
    now = now or datetime.utcnow()
    summary = {"due": 0, "sent": 0, "calls": 0, "failed": 0}
    due = session.exec(select(Lead).where(
        Lead.next_action_at != None,                       # noqa: E711
        Lead.next_action_at <= now,
        Lead.buyer_replied_at == None,                     # noqa: E711
        Lead.status.in_(["new", "quoted", "negotiating"]),
        Lead.next_action_note.in_(_STEPS))).all()
    for lead in due:
        summary["due"] += 1
        note = lead.next_action_note

        if note == "call":                                 # both follow-ups done, still no reply
            _safe(notify_needs_call, lead)
            lead.next_action_at, lead.next_action_note = None, "sequence-done"
            session.add(lead); session.commit(); summary["calls"] += 1
            continue

        if not (lead.email or "").strip():                 # nothing to send to — stop + move on
            lead.next_action_at, lead.next_action_note = None, "no-email"
            session.add(lead); session.commit()
            continue

        step = 1 if note == "followup-1" else 2
        last = _last_out_email(session, lead.id)
        subject, body = followup_message(lead, step, last.subject if last else "")
        text, html = build_parts(body)
        ok, err, mid = send_email(lead.email, subject, text, html=html,
                                  in_reply_to=last.message_id if last else "")
        session.add(Outreach(
            lead_id=lead.id, direction="out", channel="email", recipient=lead.email[:200],
            subject=subject[:200], body=body[:4000], status="sent" if ok else "failed",
            error=err, message_id=mid))
        if ok:
            summary["sent"] += 1
            if step == 1:
                lead.next_action_at = add_business_days(now, FOLLOWUP_BDAYS_2)
                lead.next_action_note = "followup-2"
            else:
                lead.next_action_at = add_business_days(now, FOLLOWUP_GRACE_BDAYS)
                lead.next_action_note = "call"
            _safe(notify_outreach_sent, lead, subject, f"Follow-up {step}")
        else:
            summary["failed"] += 1
            lead.next_action_at, lead.next_action_note = None, "send-failed"
            _safe(notify_send_failed, lead, lead.email, err)
        session.add(lead); session.commit()

    log(f"followups: {summary}")
    return summary
