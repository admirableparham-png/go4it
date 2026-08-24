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


# --- one-time link exchange: the URL token is swapped for a short-lived cookie session, so the token never
# --- reappears in referrers / history / logs for the buyer's subsequent navigation + decisions.
def new_portal_session(quote, version, now=None) -> dict:
    now = now or datetime.utcnow()
    return {"qid": quote.id, "vid": version.id, "csrf": secrets.token_urlsafe(24),
            "exp": (now + timedelta(minutes=PORTAL_TTL_MIN)).isoformat()}


def portal_session_ok(sess, now=None):
    """Return (quote_id, version_id, csrf) from a valid portal session, else None (expired/malformed)."""
    if not isinstance(sess, dict) or not sess.get("qid") or not sess.get("vid") or not sess.get("csrf"):
        return None
    exp = sess.get("exp")
    try:
        if exp and (now or datetime.utcnow()) > datetime.fromisoformat(exp):
            return None
    except ValueError:
        return None
    return sess["qid"], sess["vid"], sess["csrf"]


def csrf_ok(sess, submitted) -> bool:
    good = (sess or {}).get("csrf", "")
    return bool(good) and secrets.compare_digest(str(good), str(submitted or ""))


# Buyer-portal security headers: no token/URL leakage, no caching, no framing, no indexing. A per-render CSP
# nonce lets ONLY the view-beacon inline script run (everything else is 'none').
def portal_headers(nonce: str) -> dict:
    csp = ("default-src 'none'; style-src 'unsafe-inline'; img-src 'self' data:; "
           f"script-src 'nonce-{nonce}'; form-action 'self'; base-uri 'none'; frame-ancestors 'none'")
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
