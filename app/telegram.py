"""Instant Telegram alerts to you and your colleagues.

Every user-supplied value is HTML-escaped before it goes into the
``parse_mode=HTML`` message — without this, ordinary data like ``<jane@acme.com>``
or ``AT&T`` makes Telegram reject the whole message (HTTP 400) and the alert is
silently lost. Failures are logged, counted, and retried once in plain text.

If TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID aren't set, sending is a silent no-op.
"""
import html
import logging
import re

import httpx

from .config import BASE_URL, TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID

logger = logging.getLogger("go4it.telegram")

stats = {"sent": 0, "failed": 0}

_TAG_RE = re.compile(r"<[^>]+>")


def _esc(value) -> str:
    """HTML-escape a user-supplied value for safe use in parse_mode=HTML."""
    return html.escape(str(value if value is not None else ""))


def send_message(text: str, _plain: bool = False) -> bool:
    """Send a message to the configured chat. Returns True on success.

    On a non-200, logs the reason and retries once in plain text so a formatting
    problem never silently drops the alert.
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
            return send_message(_TAG_RE.sub("", text), _plain=True)
        return False
    except Exception as exc:  # never let a notification failure break the trade flow
        logger.warning("Telegram send error: %s", exc)
        stats["failed"] += 1
        return False


def notify_lead_matches(lead, matches) -> bool:
    """Alert the team about a new buyer lead and its best catalog matches.

    ``matches`` is a list of (product, score, reasons), best first.
    """
    lines = [
        f"\U0001F3AF <b>New lead {_esc(lead.tracking_code)} — {len(matches)} match(es)</b>",
        "",
        f"\U0001F7E2 <b>Buyer wants:</b> {_esc(lead.product)}",
        f"    {_esc(lead.quantity)} {_esc(lead.unit)} → {_esc(lead.dest_country) or '-'} · "
        f"budget ≤ {_esc(lead.target_price)} {_esc(lead.currency)}",
        f"    {_esc(lead.buyer_company) or '-'} · {_esc(lead.contact_name)} {_esc(lead.email)}".rstrip(),
        "",
        "<b>Top products:</b>",
    ]
    for product, score, reasons in matches:
        lines.append(
            f"  • {score}% {_esc(product.name)} — EXW {_esc(product.exw_price)} "
            f"{_esc(product.currency)}/{_esc(product.unit)}  <i>({_esc(reasons)})</i>"
        )
    lines.append(f"\n{BASE_URL}/leads/{lead.id}")
    return send_message("\n".join(lines))


def notify_quote_ready(quote, lead, product) -> bool:
    """Tell the team a draft quote is ready for a manager to review/approve."""
    text = (
        f"\U0001F9FE <b>Quote ready for review</b>\n"
        f"{_esc(quote.tracking_code)} · {_esc(product.name)}\n"
        f"delivered {_esc(quote.delivered_unit)} {_esc(quote.quote_currency)}/"
        f"{_esc(product.unit)} · buyer {_esc(lead.buyer_company) or '-'} "
        f"→ {_esc(lead.dest_country) or '-'}\n"
        f"{BASE_URL}/quotes/{quote.id}"
    )
    return send_message(text)


def notify_status_change(lead, old, new, actor_name) -> bool:
    """Tell the team a lead moved stage in the pipeline."""
    text = (
        f"\U0001F501 <b>Lead {_esc(lead.tracking_code)}: {_esc(old)} → {_esc(new)}</b>\n"
        f"{_esc(lead.product)} · buyer {_esc(lead.buyer_company) or '-'}\n"
        f"by {_esc(actor_name)}\n{BASE_URL}/leads/{lead.id}"
    )
    return send_message(text)
