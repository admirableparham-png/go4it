"""Two-way confidentiality + send-safety gates for ALL buyer outreach (Phase 4).

Buyers never learn the seller (strip_seller_identity, now wired here); sellers never learn the buyer (that
stays in the confidential pipeline). No buyer send happens while outreach is globally paused, the mailbox is
not a healthy Go4it admin mailbox, the recipient is suppressed/replied, daily limits are hit, the sending
window is closed, or the message fails a header-injection check. These helpers are used by every send path
(campaign worker, follow-ups, bulk, manual thread reply, quote email, test send).
"""
from datetime import datetime

from sqlmodel import select

from . import pipeline
from .models import MailAccount, OutreachControl, User


def sanitize_header(value: str) -> str:
    """Block email-header injection: a header value can never span lines. CR/LF become a space and any other
    control char is dropped, so subject/recipient/from-name can't inject Bcc:/extra headers."""
    v = (value or "").replace("\r", " ").replace("\n", " ")
    return "".join(ch for ch in v if ord(ch) >= 32).strip()


def outreach_paused(session) -> bool:
    """The global 'Pause all outreach' kill switch — when set, every send path refuses new sends."""
    ctl = session.get(OutreachControl, 1)
    return bool(ctl and ctl.paused_all)


def set_pause_all(session, paused, actor=None):
    ctl = session.get(OutreachControl, 1) or OutreachControl(id=1)
    ctl.paused_all = bool(paused)
    ctl.updated_by = getattr(actor, "id", None)
    ctl.updated_at = datetime.utcnow()
    session.add(ctl)
    try:
        pipeline.audit(session, actor, "outreach_control", 1, "pause_all" if paused else "resume_all", {})
    except Exception:  # noqa: BLE001
        pass
    return ctl


def mailbox_ok(mailbox) -> tuple:
    """A buyer send may go ONLY from a healthy, enabled, Go4it-controlled admin mailbox — never a seller's."""
    if not mailbox:
        return False, "no mailbox"
    if not mailbox.admin_owned:
        return False, "not a Go4it admin mailbox"    # buyers must never be contacted from a seller mailbox
    if not mailbox.active or mailbox.paused:
        return False, "mailbox paused or disabled"
    return True, ""


def mailbox_take_slot(mailbox, now=None) -> bool:
    """Consume one daily send slot; False if the mailbox's daily_limit is already reached. Resets the counter
    when the calendar date rolls. NEVER auto-increases the limit."""
    now = now or datetime.utcnow()
    today = now.strftime("%Y-%m-%d")
    if mailbox.sent_today_date != today:
        mailbox.sent_today_date = today
        mailbox.sent_today = 0
    if mailbox.sent_today >= max(0, mailbox.daily_limit):
        return False
    mailbox.sent_today += 1
    mailbox.last_outbound_at = now
    return True


def within_window(campaign, now=None) -> bool:
    """True if now is inside the campaign's allowed weekday + hour window (campaign timezone approximated as
    server time; deterministic for tests). No campaign → always allowed (manual/quote sends)."""
    if campaign is None:
        return True
    now = now or datetime.utcnow()
    days = {int(d) for d in (campaign.send_days or "").split(",") if d.strip().isdigit()}
    if days and now.weekday() not in days:
        return False
    return campaign.send_window_start <= now.hour < campaign.send_window_end


def guard_buyer_text(session, text, seller_id):
    """Redact the SELLER's identity out of buyer-facing text — buyers must never learn who the seller is.
    Wires pipeline.strip_seller_identity (previously defined-but-uncalled) with the seller's own name/email +
    their connected mailbox addresses."""
    if not seller_id or not text:
        return text
    seller = session.get(User, seller_id)
    if not seller:
        return text
    emails = [m.email for m in session.exec(select(MailAccount).where(MailAccount.user_id == seller_id)).all()
              if m.email]
    return pipeline.strip_seller_identity(text, seller, seller_emails=emails)


# --- honest email-authentication status (Phase 4 hardening) ---------------------------------------
# No live SPF/DKIM/DMARC probing exists yet, so a saved DB value is NEVER shown as "pass/healthy/configured".
# Empty → "Not checked". A live result (added later) is shown verbatim and flagged stale past its freshness.
_AUTH_FRESH_DAYS = 7


def mail_auth_display(value, checked_at=None, now=None):
    """Return (label, tone) for an SPF/DKIM/DMARC field. tone ∈ unknown|pass|fail|stale — the UI colors it.
    Until live verification is wired, a stored value with no checked_at reads as 'Not checked' (unknown),
    never as passing."""
    v = (value or "").strip().lower()
    if not v or checked_at is None:
        return "Not checked", "unknown"
    now = now or datetime.utcnow()
    stale = (now - checked_at).days > _AUTH_FRESH_DAYS
    if v in ("pass", "ok", "valid", "aligned"):
        return ("Pass (stale)" if stale else "Pass"), ("stale" if stale else "pass")
    if v in ("fail", "invalid", "missing", "none"):
        return ("Fail (stale)" if stale else "Fail"), ("stale" if stale else "fail")
    return (f"{value} (stale)" if stale else str(value)), ("stale" if stale else "unknown")
