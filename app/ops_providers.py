"""Phase 7 — external operations provider interfaces (honest "Not configured" abstractions).

Go4it does not currently integrate any live carrier-tracking, freight-quotation, customs-status, or payment/
remittance provider. These interfaces exist so the app is HONEST about that: each reports `configured: False`
and offers no endpoint. When a real provider is wired in a future phase it will be:
  - environment-configured (credentials read from env, encrypted at rest via outreach.mail_encrypt),
  - strictly timed out, retried a bounded number of times, idempotent,
  - webhook-verified (HMAC signature) with replay protection,
  - circuit-broken on persistent failure (a Work Queue task is raised, other workers are never blocked),
  - sanitized in logs (no secrets, no raw payloads).
NO live/paid provider is ever called from tests or local development.
"""
import hashlib
import hmac
import os
from datetime import datetime, timedelta

# in-memory replay guard for webhook delivery ids (single-process; a real deployment uses a shared store)
_SEEN_DELIVERIES = {}
_REPLAY_TTL_S = 3600


def _status(env_key, name):
    configured = bool(os.environ.get(env_key, "").strip())
    return {"configured": configured, "provider": name if configured else "",
            "message": "" if configured else "Provider integration not configured"}


def tracking_status():
    return _status("CARRIER_TRACKING_PROVIDER", "carrier-tracking")


def freight_quote_status():
    return _status("FREIGHT_QUOTE_PROVIDER", "freight-quote")


def customs_status():
    return _status("CUSTOMS_STATUS_PROVIDER", "customs-status")


def payment_status():
    return _status("PAYMENT_PROVIDER", "payment")


def remittance_status():
    return _status("REMITTANCE_PROVIDER", "remittance")


def webhook_secret():
    """The shared secret for inbound tracking webhooks (env-configured). Empty → the endpoint rejects all
    deliveries (no provider is configured, so no legitimate signed call can arrive)."""
    return os.environ.get("OPS_WEBHOOK_SECRET", "").strip()


def verify_webhook(secret: str, raw_body: bytes, signature: str) -> bool:
    """Constant-time HMAC-SHA256 verification of a webhook body. Returns False when no secret is configured
    (fail-closed) or the signature does not match — a forged/unsigned call is never trusted."""
    if not secret or not signature:
        return False
    expected = hmac.new(secret.encode(), raw_body or b"", hashlib.sha256).hexdigest()
    try:
        return hmac.compare_digest(expected, signature.strip().lower())
    except Exception:  # noqa: BLE001
        return False


def replay_seen(delivery_id: str, now=None) -> bool:
    """True if this webhook delivery id was already processed within the TTL (replay). Records it otherwise."""
    if not delivery_id:
        return False
    now = now or datetime.utcnow()
    cutoff = now - timedelta(seconds=_REPLAY_TTL_S)
    for k in [k for k, ts in _SEEN_DELIVERIES.items() if ts < cutoff]:
        _SEEN_DELIVERIES.pop(k, None)
    if delivery_id in _SEEN_DELIVERIES:
        return True
    _SEEN_DELIVERIES[delivery_id] = now
    return False


def reset():
    """Test hook — clear the in-memory replay guard."""
    _SEEN_DELIVERIES.clear()


def all_statuses():
    return {"tracking": tracking_status(), "freight_quote": freight_quote_status(),
            "customs": customs_status(), "payment": payment_status(), "remittance": remittance_status()}
