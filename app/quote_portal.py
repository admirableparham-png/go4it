"""Phase 6 — secure buyer quote portal (hashed tokens, version-scoped, idempotent).

A QuoteAccessToken is a cryptographically-secure random string shown ONCE in the send link and stored only as
its sha256 hash. Each token is scoped to one QuoteVersion, expires, and is revocable/rotatable. The raw token
is never persisted or logged. Buyer acceptance is idempotent and never creates a Deal directly — it records the
commercial event and raises an admin Work Queue task (accepted_quote_needs_deal).
"""
import hashlib
import secrets
from datetime import datetime, timedelta

from sqlmodel import select

from . import quote_workflow as QW


def hash_token(raw: str) -> str:
    return hashlib.sha256((raw or "").encode()).hexdigest()


def mint_token(session, quote, version, *, actor=None, valid_days=14, rotate=True):
    """Create a new access token for a quote VERSION; returns the RAW token (show once). If `rotate`, revoke
    the quote's other live tokens first. The raw value is never stored — only its hash."""
    from .models import QuoteAccessToken
    if rotate:
        for t in session.exec(select(QuoteAccessToken).where(
                QuoteAccessToken.quote_id == quote.id, QuoteAccessToken.revoked == False)).all():  # noqa: E712
            t.revoked = True
            session.add(t)
    raw = secrets.token_urlsafe(32)
    tok = QuoteAccessToken(quote_id=quote.id, quote_version_id=version.id, token_hash=hash_token(raw),
                           expires_at=datetime.utcnow() + timedelta(days=valid_days),
                           created_by=getattr(actor, "email", "") or "")
    session.add(tok)
    session.flush()
    return raw, tok


def resolve_token(session, raw, now=None):
    """Look a raw token up by HASH. Returns (token, quote, version) only if valid (not revoked, not expired,
    quote presentable). Returns None on any failure — a revoked/expired/unknown/altered token fails safely.
    Never logs the raw token."""
    from .models import Quote, QuoteAccessToken, QuoteVersion
    now = now or datetime.utcnow()
    if not (raw or "").strip():
        return None
    tok = session.exec(select(QuoteAccessToken).where(
        QuoteAccessToken.token_hash == hash_token(raw))).first()
    if not tok or tok.revoked:
        return None
    if tok.expires_at and now > tok.expires_at:
        return None
    quote = session.get(Quote, tok.quote_id)
    version = session.get(QuoteVersion, tok.quote_version_id)
    if not quote or not version:
        return None
    # the token is scoped to THIS version; a superseded version's token stops serving
    if version.id != quote.current_version_id and quote.status == "superseded":
        return None
    return tok, quote, version


def revoke_token(session, token):
    token.revoked = True
    session.add(token)


def register_view(session, tok, quote, now=None):
    """Record a controlled buyer view (view_count + last_viewed_at + a `viewed` status event). No term change."""
    now = now or datetime.utcnow()
    tok.view_count += 1
    tok.last_viewed_at = now
    session.add(tok)
    QW.record_view(session, quote, now=now)


def record_buyer_action(session, quote, version, action, *, message="", actor=None, now=None):
    """Idempotently record accept/reject/change. Returns (status, message). A repeat accept is a safe no-op
    (returns 'already_accepted'). Accept raises the admin 'create deal' task — it never creates a Deal here."""
    now = now or datetime.utcnow()
    action = (action or "").strip().lower()
    if action not in ("accept", "reject", "changes"):
        return "invalid", "unknown action"
    # idempotency: a terminal buyer decision is never re-processed
    if quote.status == "accepted" or quote.buyer_response == "accepted":
        return "already_accepted", ""
    if quote.status == "rejected":
        return "already_rejected", ""
    to = {"accept": "accepted", "reject": "rejected", "changes": "change_requested"}[action]
    ok, why = QW.transition(session, quote, to, actor=actor, actor_kind="buyer",
                            reason=(message or "")[:500], now=now)
    if not ok:
        return "unavailable", why
    if to == "accepted":
        _raise_deal_task(session, quote, version)
    return to, ""


def _raise_deal_task(session, quote, version):
    """Buyer acceptance → an admin Work Queue task to create the Deal (idempotent; unique per version)."""
    try:
        from . import work_queue as WQ
        WQ.create_work_item_safe(
            session, tenant_id=quote.owner_id, type="accepted_quote_needs_deal", source="automatic",
            title=f"Accepted quote {quote.tracking_code} needs a deal",
            description="The buyer accepted this quote version. Create the Deal (idempotent, one per version).",
            related_quote_id=quote.id, related_lead_id=quote.lead_id,
            idempotency_key=f"accepted_quote_needs_deal:qv:{version.id}", condition_version="accepted")
    except Exception:  # noqa: BLE001
        pass
