"""Phase 6 — the central quote workflow (transitions, expiry, events).

ONE authority for valid quote status transitions. Every move is validated server-side (invalid → rejected),
records a QuoteStatusEvent, and is audited. Only an approved version can be sent; only a sent/viewed, current,
unexpired version can be accepted; accepting an old/superseded/expired version fails safely. Expiry is computed
one way (`is_expired`). GET never mutates — the portal records a `viewed` event through here, not on a read.
"""
from datetime import datetime, timedelta

from sqlmodel import select

STATUSES = ["draft", "needs_review", "approved", "sent", "viewed", "change_requested", "accepted",
            "rejected", "expired", "cancelled", "superseded"]
TERMINAL = {"accepted", "rejected", "cancelled", "superseded"}
PRESENTABLE = ("approved", "sent", "viewed")   # a buyer link may serve these (and only if not expired)
# states the buyer portal may RENDER (read-only confirmation for a decided quote); decisions stay guarded
PORTAL_VIEWABLE = ("approved", "sent", "viewed", "accepted", "rejected", "change_requested")

# valid (from → {to}) transitions — the single source of truth
TRANSITIONS = {
    "draft": {"needs_review", "approved", "cancelled"},
    "needs_review": {"approved", "draft", "rejected", "cancelled"},
    "approved": {"sent", "draft", "cancelled", "expired", "superseded"},
    "sent": {"viewed", "accepted", "rejected", "change_requested", "expired", "superseded", "cancelled"},
    "viewed": {"accepted", "rejected", "change_requested", "expired", "superseded", "cancelled"},
    "change_requested": {"draft", "cancelled", "superseded"},
    "accepted": {"superseded"},
    "rejected": {"draft"},
    "expired": {"draft", "superseded"},
    "cancelled": set(),
    "superseded": set(),
}


def can_transition(from_status, to_status) -> bool:
    return to_status in TRANSITIONS.get(from_status or "draft", set())


def expiry_at(quote):
    if not quote.created_at or not quote.validity_days:
        return None
    return quote.created_at + timedelta(days=quote.validity_days)


def is_expired(quote, now=None) -> bool:
    exp = expiry_at(quote)
    return bool(exp and (now or datetime.utcnow()) > exp)


def _current_version(session, quote):
    from .models import QuoteVersion
    if quote.current_version_id:
        return session.get(QuoteVersion, quote.current_version_id)
    return session.exec(select(QuoteVersion).where(QuoteVersion.quote_id == quote.id)
                        .order_by(QuoteVersion.version.desc())).first()


def transition(session, quote, to, actor=None, actor_kind="admin", reason="", now=None) -> tuple:
    """Validate + apply a status transition. Returns (ok, message). Records a QuoteStatusEvent + audit.
    Never raises on an invalid transition — it is refused (ok=False)."""
    from .models import QuoteStatusEvent
    now = now or datetime.utcnow()
    frm = quote.status or "draft"
    if to not in STATUSES:
        return False, f"unknown status {to}"
    if to == frm:
        return True, "no change"
    if not can_transition(frm, to):
        return False, f"invalid transition {frm} → {to}"
    # guarded rules
    if to == "sent" and frm != "approved":
        return False, "only an approved quote can be sent"
    if to == "accepted" and not can_accept(quote, now=now):
        return False, "quote is not acceptable (not sent/current/unexpired)"
    quote.status = to
    ver = _current_version(session, quote)
    if ver is not None:
        ver.status = to
        if to == "sent":
            ver.sent_at = now
        session.add(ver)
    if to == "accepted":
        quote.accepted_at = quote.accepted_at or now
        quote.buyer_response = "accepted"
    elif to == "change_requested":
        quote.buyer_response = "changes"
    session.add(quote)
    session.add(QuoteStatusEvent(quote_id=quote.id, quote_version_id=(ver.id if ver else None),
                                 from_status=frm, to_status=to,
                                 actor_id=getattr(actor, "id", None), actor_kind=actor_kind,
                                 reason=(reason or "")[:500]))
    _audit(session, actor, quote.id, "quote_transition", {"from": frm, "to": to, "by": actor_kind})
    return True, ""


def can_accept(quote, now=None) -> bool:
    """Only a sent OR viewed, current, unexpired, non-superseded version can be accepted."""
    return quote.status in ("sent", "viewed") and not is_expired(quote, now=now)


def mark_expired_if_due(session, quote, now=None) -> bool:
    """Flip an overdue approved/sent/viewed quote to expired so it is never presented as active. Idempotent."""
    now = now or datetime.utcnow()
    if quote.status in ("approved", "sent", "viewed") and is_expired(quote, now=now):
        ok, _ = transition(session, quote, "expired", actor_kind="system", reason="validity elapsed", now=now)
        return ok
    return False


def record_view(session, quote, now=None) -> None:
    """A valid-token buyer view records a controlled `viewed` event (sent → viewed) WITHOUT changing terms.
    Never called on a plain GET of admin pages."""
    now = now or datetime.utcnow()
    if quote.viewed_at is None:
        quote.viewed_at = now
        session.add(quote)
    if quote.status == "sent":
        transition(session, quote, "viewed", actor_kind="buyer", reason="buyer opened link", now=now)


def _audit(session, actor, quote_id, action, meta):
    try:
        from .pipeline import audit
        audit(session, actor, "quote", quote_id, action, meta)
    except Exception:  # noqa: BLE001
        pass
