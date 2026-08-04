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

from .config import (BASE_URL, IMAP_ENABLED, IMAP_HOST, IMAP_PASSWORD, IMAP_PORT, IMAP_USER)
from .lead_service import find_lead_by_contact
from .models import IngestionRun, Lead, Outreach
from .telegram import send_message

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
        session.add(lead)
    session.commit()
    try:
        send_message(f"Buyer replied: {lead.buyer_company or from_addr}\n"
                     f"{subject[:80]}\n{BASE_URL}/leads/{lead.id}")
    except Exception:  # noqa: BLE001 - alerts are best-effort
        pass
    return "threaded"


def poll_inbox(session: Session, log=logger.info) -> dict:
    """Pull UNSEEN mail over IMAP and thread each reply. No-op unless IMAP is configured."""
    summary = {"seen": 0, "threaded": 0, "unmatched": 0, "duplicate": 0}
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
            frm, subj, body, mid, irt = parse_email(msgdata[0][1])
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
