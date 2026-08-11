"""Inbound buyer email -> conversation thread (the receive half of the Close Deal panel).

When IMAP_* is set in .env, the worker polls the mailbox, matches each sender to an existing Lead
(find_lead_by_contact) and threads the reply as an INBOUND Outreach row, so it shows in the
/leads/{id} Conversation panel and stamps buyer_replied_at. Unmatched senders are skipped (NOT
auto-made into leads). OFF by default — nothing runs until IMAP_HOST/USER/PASSWORD are configured.

Sending stays in app/outreach.py (SMTP); this module is receive-only.
"""
import email as emaillib
import imaplib
import logging
import re
from datetime import datetime
from email.utils import parseaddr

from sqlmodel import Session, select

from .config import (IMAP_ENABLED, IMAP_HOST, IMAP_PASSWORD, IMAP_PORT, IMAP_USER)
from .enrich_service import enrich_lead
from .lead_service import find_lead_by_contact
from .models import IngestionRun, Lead, Outreach
from .telegram import notify_bounce, notify_buyer_reply, send_message

logger = logging.getLogger("go4it")


def _msgid(s):
    """Extract the first <...> Message-ID token from a header value (In-Reply-To / References)."""
    m = re.search(r"<[^>]+>", s or "")
    return m.group(0) if m else (s or "").strip()


def _referenced_lead(session: Session, in_reply_to: str):
    """The lead a reply belongs to by its In-Reply-To — matched to the outbound Outreach we sent
    (its Message-ID). More reliable than sender identity when the buyer replies from another address."""
    if not in_reply_to:
        return None
    o = session.exec(select(Outreach).where(Outreach.message_id == in_reply_to)
                     .order_by(Outreach.id.desc())).first()
    return session.get(Lead, o.lead_id) if o else None


def _plain_body(msg) -> str:
    """Best-effort plain-text body (prefer text/plain, skip attachments)."""
    parts = msg.walk() if msg.is_multipart() else [msg]
    for part in parts:
        if part.get_content_type() == "text/plain" \
                and "attachment" not in str(part.get("Content-Disposition", "")):
            try:
                payload = part.get_payload(decode=True)
                if payload is not None:
                    return payload.decode(part.get_content_charset() or "utf-8", "ignore").strip()
            except Exception:  # noqa: BLE001
                continue
    return ""


def parse_email(raw: bytes):
    """Parse a raw RFC822 message -> (from_addr, subject, body, message_id, in_reply_to).
    in_reply_to is the <Message-ID> this replies to (In-Reply-To, else last of References)."""
    msg = emaillib.message_from_bytes(raw)
    from_addr = (parseaddr(msg.get("From", ""))[1] or "").strip().lower()
    subject = str(msg.get("Subject", "")).strip()
    message_id = (msg.get("Message-ID", "") or "").strip()
    irt = (msg.get("In-Reply-To", "") or "").strip()
    if not irt:
        refs = (msg.get("References", "") or "").strip().split()
        irt = refs[-1] if refs else ""
    return from_addr, subject, _plain_body(msg), message_id, _msgid(irt)


def handle_inbound(session: Session, from_addr: str, subject: str, body: str,
                   message_id: str = "", in_reply_to: str = "") -> str:
    """Thread one parsed inbound email onto its lead. Returns 'threaded' | 'duplicate' | 'unmatched'.
    Matches by the In-Reply-To header first (the outbound we sent), then by sender email/phone.
    Pure of IMAP so it's unit-testable without a live mailbox."""
    from_addr = (from_addr or "").strip().lower()
    message_id = (message_id or "").strip()
    in_reply_to = _msgid(in_reply_to)
    if message_id and session.exec(select(Outreach).where(
            Outreach.message_id == message_id, Outreach.direction == "in")).first():
        return "duplicate"
    lead = _referenced_lead(session, in_reply_to) or find_lead_by_contact(session, email=from_addr)
    if lead is None:
        return "unmatched"
    session.add(Outreach(
        lead_id=lead.id, direction="in", channel="email", from_addr=from_addr,
        subject=subject[:200], body=(body or "")[:8000], message_id=message_id[:400],
        status="received"))
    if lead.buyer_replied_at is None:
        lead.buyer_replied_at = datetime.utcnow()
    lead.next_action_at = None            # a reply stops the auto follow-up sequence
    lead.next_action_note = "replied"
    session.add(lead)
    session.commit()
    try:
        notify_buyer_reply(lead, from_addr, subject, snippet=body)
    except Exception:  # noqa: BLE001 - alerts are best-effort
        pass
    return "threaded"


def _dsn_details(msg):
    """From a bounce (DSN) message, pull (failed_recipient, diagnostic) out of its
    message/delivery-status part when present."""
    recipient = diag = ""
    for part in (msg.walk() if msg.is_multipart() else [msg]):
        if part.get_content_type() == "message/delivery-status":
            for blk in (part.get_payload() if isinstance(part.get_payload(), list) else []):
                fr = blk.get("Final-Recipient") or blk.get("Original-Recipient") or ""
                if ";" in fr:
                    recipient = fr.split(";", 1)[1].strip().lower()
                dc = blk.get("Diagnostic-Code") or ""
                if dc:
                    diag = " ".join(dc.split())
    return recipient, diag


def detect_bounce(raw: bytes):
    """If a raw message is a delivery-failure (DSN), return (failed_recipient, reason); else None."""
    msg = emaillib.message_from_bytes(raw)
    frm = (parseaddr(msg.get("From", ""))[1] or "").lower()
    subj = str(msg.get("Subject", "")).lower()
    ctype = str(msg.get("Content-Type", "")).lower()
    looks = ("mailer-daemon" in frm or "postmaster" in frm or "report-type=delivery-status" in ctype
             or msg.get_content_type() == "multipart/report"
             or any(k in subj for k in ("delivery status notification", "undelivered", "delivery failure",
                                        "returned mail", "mail delivery failed", "undeliverable")))
    if not looks:
        return None
    rcpt, diag = _dsn_details(msg)
    if not rcpt:                                    # fallback: scrape the body
        body = _plain_body(msg)
        m = re.search(r"[\w.+-]+@[\w-]+\.[\w.-]+", body)
        rcpt = m.group(0).lower() if m else ""
        d = re.search(r"55\d[ -].{0,120}", body)
        diag = d.group(0).strip() if d else "delivery failed"
    return (rcpt, diag or "delivery failed") if rcpt else None


def handle_bounce(session: Session, failed_email: str, reason: str) -> str:
    """Mark the outbound as failed, clear the bad address, try to re-enrich, and alert. Returns a status."""
    lead = find_lead_by_contact(session, email=failed_email)
    if lead is None:
        try:
            send_message(f"⚠️ Email bounced for {failed_email} (no matching lead)\n{reason[:160]}")
        except Exception:  # noqa: BLE001
            pass
        return "bounce-unmatched"
    o = session.exec(select(Outreach).where(
        Outreach.lead_id == lead.id, Outreach.direction == "out",
        Outreach.recipient == failed_email).order_by(Outreach.id.desc())).first()
    if o:
        o.status = "failed"
        o.error = reason[:400]
        session.add(o)
    if (lead.email or "").lower() == failed_email:
        lead.email = ""                              # the address is wrong — drop it
    lead.next_action_at = None                       # stop the sequence
    lead.next_action_note = "bounced"
    session.add(lead)
    session.commit()
    new_email = ""
    try:
        enrich_lead(session, lead)                   # scrape the buyer's own site for a fresh mailbox
        session.refresh(lead)
        new_email = lead.email or ""
    except Exception:  # noqa: BLE001
        pass
    try:
        notify_bounce(lead, failed_email, reason, new_email=new_email)
    except Exception:  # noqa: BLE001
        pass
    return "bounced"


def poll_inbox(session: Session, log=logger.info) -> dict:
    """Pull UNSEEN mail over IMAP and thread each reply. No-op unless IMAP is configured."""
    summary = {"seen": 0, "threaded": 0, "unmatched": 0, "duplicate": 0, "bounced": 0}
    if not IMAP_ENABLED:
        return summary
    run = IngestionRun(source="email-inbound", status="running")
    session.add(run)
    session.commit()
    session.refresh(run)
    try:
        M = imaplib.IMAP4_SSL(IMAP_HOST, IMAP_PORT)
        M.login(IMAP_USER, IMAP_PASSWORD)
        M.select("INBOX")
        _, data = M.search(None, "UNSEEN")
        ids = data[0].split() if data and data[0] else []
        for num in ids:
            _, msgdata = M.fetch(num, "(RFC822)")
            if not msgdata or not msgdata[0]:
                continue
            summary["seen"] += 1
            raw = msgdata[0][1]
            bounce = detect_bounce(raw)               # delivery-failure notice?
            if bounce:
                handle_bounce(session, bounce[0], bounce[1])
                summary["bounced"] += 1
            else:
                frm, subj, body, mid, irt = parse_email(raw)
                summary[handle_inbound(session, frm, subj, body, mid, irt)] += 1
            M.store(num, "+FLAGS", "\\Seen")
        M.logout()
        run.status = "ok"
    except Exception as exc:  # noqa: BLE001
        run.status = "error"
        run.error = str(exc)[:400]
        logger.exception("inbound email poll failed")
    run.leads_seen = summary["seen"]
    run.leads_new = summary["threaded"]
    run.finished_at = datetime.utcnow()
    session.add(run)
    session.commit()
    log(f"inbound email: {summary}")
    return summary
