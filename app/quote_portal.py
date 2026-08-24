"""Phase 6 — secure buyer quote portal (hashed tokens, version-scoped, idempotent).

A QuoteAccessToken is a cryptographically-secure random string shown ONCE in the send link and stored only as
its sha256 hash. Each token is scoped to one QuoteVersion, expires, and is revocable/rotatable. The raw token
is never persisted or logged. Buyer acceptance is idempotent and never creates a Deal directly — it records the
commercial event and raises an admin Work Queue task (accepted_quote_needs_deal).
"""
import hashlib
import json
import secrets
from datetime import datetime, timedelta

from sqlmodel import select

from . import quote_workflow as QW

PORTAL_TTL_MIN = 30      # a portal cookie session lives this long after the one-time token exchange


def hash_token(raw: str) -> str:
    return hashlib.sha256((raw or "").encode()).hexdigest()


# --- one-time link exchange → a SERVER-SIDE session. The raw token arrives via a URL FRAGMENT (never sent to
# --- the server / proxy logs), is POSTed to the exchange, atomically CONSUMED (single-use), and swapped for a
# --- PortalSession row. The browser cookie carries ONLY the opaque `sid` — no quote id, token, expiry, or CSRF
# --- ever lives in a client-readable cookie.
def open_session(session, raw, *, now=None):
    """Consume a link token (single-use, atomic) and open a server-side PortalSession. Returns
    (portal_session, quote, version) or None. A re-open of an already-consumed token re-issues its still-valid
    session (so a buyer reopening the email link is not broken), else fails safely."""
    from sqlalchemy import update as _upd
    from .models import PortalSession, Quote, QuoteAccessToken, QuoteVersion
    now = now or datetime.utcnow()
    if not (raw or "").strip():
        return None
    tok = session.exec(select(QuoteAccessToken).where(QuoteAccessToken.token_hash == hash_token(raw))).first()
    if not tok or tok.revoked:
        return None
    if tok.expires_at and now > tok.expires_at:
        return None
    quote = session.get(Quote, tok.quote_id)
    version = session.get(QuoteVersion, tok.quote_version_id)
    if not quote or not version:
        return None
    if version.id != quote.current_version_id and quote.status == "superseded":
        return None
    # ATOMIC single-use consume: only the first exchange flips consumed_at (CAS on NULL).
    res = session.execute(_upd(QuoteAccessToken).where(
        QuoteAccessToken.id == tok.id, QuoteAccessToken.consumed_at.is_(None)).values(consumed_at=now))
    if res.rowcount == 1:
        ps = PortalSession(sid=secrets.token_urlsafe(32), quote_id=quote.id, quote_version_id=version.id,
                           token_id=tok.id, token_hash=tok.token_hash, csrf=secrets.token_urlsafe(24),
                           expires_at=now + timedelta(minutes=PORTAL_TTL_MIN))
        session.add(ps); session.flush()
        return ps, quote, version
    # already consumed → reuse the still-valid session opened from this token (buyer refreshed the link)
    ps = session.exec(select(PortalSession).where(
        PortalSession.token_id == tok.id, PortalSession.revoked == False)   # noqa: E712
        .order_by(PortalSession.id.desc())).first()
    if ps and (not ps.expires_at or now <= ps.expires_at):
        return ps, quote, version
    return None


def load_session(session, sid, *, now=None):
    """Resolve an opaque portal-session id (from the cookie) → (quote, version, csrf) or None. All validity
    (revocation, expiry, version) is checked against the SERVER-SIDE record, not the cookie."""
    from .models import PortalSession, Quote, QuoteVersion
    now = now or datetime.utcnow()
    if not (sid or "").strip():
        return None
    ps = session.exec(select(PortalSession).where(PortalSession.sid == sid)).first()
    if not ps or ps.revoked:
        return None
    if ps.expires_at and now > ps.expires_at:
        return None
    q = session.get(Quote, ps.quote_id)
    ver = session.get(QuoteVersion, ps.quote_version_id)
    if not q or not ver:
        return None
    return q, ver, ps.csrf


def revoke_session(session, sid):
    from .models import PortalSession
    ps = session.exec(select(PortalSession).where(PortalSession.sid == sid)).first()
    if ps:
        ps.revoked = True; session.add(ps)


def csrf_ok(good_csrf, submitted) -> bool:
    return bool(good_csrf) and secrets.compare_digest(str(good_csrf), str(submitted or ""))


# Buyer-portal security headers: no token/URL leakage, no caching, no framing, no indexing. A per-render CSP
# nonce lets ONLY the view-beacon inline script run (everything else is 'none').
def portal_headers(nonce: str) -> dict:
    csp = ("default-src 'none'; style-src 'unsafe-inline'; img-src 'self' data:; "
           f"script-src 'nonce-{nonce}'; connect-src 'self'; form-action 'self'; base-uri 'none'; "
           "frame-ancestors 'none'")
    return {"Referrer-Policy": "no-referrer", "Cache-Control": "no-store, max-age=0",
            "Pragma": "no-cache", "X-Robots-Tag": "noindex, nofollow", "X-Frame-Options": "DENY",
            "Content-Security-Policy": csp}


def buyer_options(version):
    """Buyer-facing commercial options on a version (e.g. an EXW option + a delivered/CIF option), each stating
    what is included/excluded. INTERNAL EXW *cost* is never here — these are approved buyer prices only."""
    try:
        opts = json.loads(version.options or "[]")
        return opts if isinstance(opts, list) else []
    except Exception:  # noqa: BLE001
        return []


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


def record_view(session, quote, version, now=None):
    """Record a controlled buyer view — IDEMPOTENT, via an explicit POST after the page renders (never on a
    GET). Sets viewed_at once + transitions sent→viewed once; a repeat is a no-op. Scoped to the exact version
    the buyer saw."""
    now = now or datetime.utcnow()
    if quote.current_version_id and version and quote.current_version_id != version.id:
        return "stale"                              # the version moved on — don't record against an old one
    if quote.viewed_at is not None and quote.status != "sent":
        return "already_viewed"
    QW.record_view(session, quote, now=now)
    return "viewed"


def record_buyer_action(session, quote, version, action, *, message="", actor=None, now=None):
    """Idempotently record accept/reject/change against the EXACT version the buyer saw. Returns (status, msg).
    A repeat of ANY decision is a safe no-op (already_accepted/already_rejected/already_changed). Accept raises
    the admin 'create deal' task — it never creates a Deal here."""
    now = now or datetime.utcnow()
    action = (action or "").strip().lower()
    if action not in ("accept", "reject", "changes"):
        return "invalid", "unknown action"
    # the decision must be against the CURRENT version the buyer was shown (replay/stale-version guard)
    if quote.current_version_id and version and quote.current_version_id != version.id:
        return "stale", "this quote has been revised"
    # idempotency: a terminal buyer decision is never re-processed
    if quote.status == "accepted" or quote.buyer_response == "accepted":
        return "already_accepted", ""
    if quote.status == "rejected":
        return "already_rejected", ""
    if action == "changes" and quote.buyer_response == "changes":
        return "already_changed", ""                # a repeat change-request is a no-op
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
