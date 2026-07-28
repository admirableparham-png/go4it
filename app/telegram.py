"""Instant Telegram alerts to you and your colleagues.

Two hard-won details baked in here:

1. Every user-supplied value is HTML-escaped before it goes into the
   ``parse_mode=HTML`` message. Without this, a perfectly ordinary contact like
   ``<jane@acme.com>`` or a company name like ``AT&T`` makes Telegram reject the
   whole message (HTTP 400) and the alert is silently lost.
2. Failures are *observable* — logged with the status + body, counted, and
   retried once in plain text — instead of being swallowed.

If TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID aren't set, sending is a silent no-op,
so the app runs fine with alerts turned off.
"""
import html
import logging
import re

import httpx

from .config import TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID

logger = logging.getLogger("go4it.telegram")

# Simple observable counter so a monitor/endpoint can see if alerts are failing.
stats = {"sent": 0, "failed": 0}

_TAG_RE = re.compile(r"<[^>]+>")


def _esc(value) -> str:
    """HTML-escape a user-supplied value for safe use in parse_mode=HTML."""
    return html.escape(str(value if value is not None else ""))


def send_message(text: str, _plain: bool = False) -> bool:
    """Send a message to the configured chat. Returns True on success.

    On a non-200 (e.g. a markup parse error), logs the reason and retries once
    in plain text so a formatting problem never silently drops the alert.
    """
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        return False
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {"chat_id": TELEGRAM_CHAT_ID, "text": text}
    if not _plain:
        payload["parse_mode"] = "HTML"
    try:
        r = httpx.post(url, json=payload, timeout=10)
        if r.status_code == 200:
            stats["sent"] += 1
            return True
        logger.warning("Telegram send failed: HTTP %s — %s", r.status_code, r.text[:300])
        stats["failed"] += 1
        if not _plain:
            # Fall back to plain text in case the HTML markup was the problem.
            return send_message(_TAG_RE.sub("", text), _plain=True)
        return False
    except Exception as exc:  # never let a notification failure break the trade flow
        logger.warning("Telegram send error: %s", exc)
        stats["failed"] += 1
        return False


def notify_match(demand, offer, score, reasons) -> bool:
    """Format and send a 'new match' alert your team can act on immediately."""
    text = (
        f"\U0001F3AF <b>New match — {score}%</b>\n\n"
        f"\U0001F7E2 <b>Buyer wants:</b> {_esc(demand.product)}\n"
        f"    {_esc(demand.quantity)} {_esc(demand.unit)} · budget ≤ "
        f"{_esc(demand.target_price)} {_esc(demand.currency)}\n"
        f"    {_esc(demand.location) or '-'} · {_esc(demand.contact) or 'no contact'}\n\n"
        f"\U0001F535 <b>Seller has:</b> {_esc(offer.product)}\n"
        f"    {_esc(offer.quantity)} {_esc(offer.unit)} · price "
        f"{_esc(offer.price)} {_esc(offer.currency)}\n"
        f"    {_esc(offer.location) or '-'} · {_esc(offer.contact) or 'no contact'}\n\n"
        f"<i>why:</i> {_esc(reasons)}"
    )
    return send_message(text)
