"""Instant Telegram alerts to you and your colleagues.

If TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID aren't set, these are silent no-ops,
so the app runs fine with alerts turned off.
"""
import httpx

from .config import TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID


def send_message(text: str) -> bool:
    """Send a plain HTML message to the configured chat. Returns True on success."""
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        return False
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    try:
        r = httpx.post(
            url,
            json={"chat_id": TELEGRAM_CHAT_ID, "text": text, "parse_mode": "HTML"},
            timeout=10,
        )
        return r.status_code == 200
    except Exception:
        # Never let a notification failure break the trade flow.
        return False


def notify_match(demand, offer, score, reasons) -> bool:
    """Format and send a 'new match' alert your team can act on immediately."""
    text = (
        f"\U0001F3AF <b>New match — {score}%</b>\n\n"
        f"\U0001F7E2 <b>Buyer wants:</b> {demand.product}\n"
        f"    {demand.quantity} {demand.unit} · budget ≤ {demand.target_price} {demand.currency}\n"
        f"    {demand.location or '-'} · {demand.contact or 'no contact'}\n\n"
        f"\U0001F535 <b>Seller has:</b> {offer.product}\n"
        f"    {offer.quantity} {offer.unit} · price {offer.price} {offer.currency}\n"
        f"    {offer.location or '-'} · {offer.contact or 'no contact'}\n\n"
        f"<i>why:</i> {reasons}"
    )
    return send_message(text)
