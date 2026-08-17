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

from .config import BASE_URL, TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID, TELEGRAM_NOTIFY_SENDS

logger = logging.getLogger("go4it.telegram")

stats = {"sent": 0, "failed": 0}

_TAG_RE = re.compile(r"<[^>]+>")


def _esc(value) -> str:
    """HTML-escape a user-supplied value for safe use in parse_mode=HTML."""
    return html.escape(str(value if value is not None else ""))


def _chat_ids():
    """The recipient chat IDs — TELEGRAM_CHAT_ID may be a comma-separated list (multiple users)."""
    return [c.strip() for c in str(TELEGRAM_CHAT_ID or "").split(",") if c.strip()]


def _send_one(chat_id: str, text: str, _plain: bool = False) -> bool:
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {"chat_id": chat_id, "text": text}
    if not _plain:
        payload["parse_mode"] = "HTML"
    try:
        r = httpx.post(url, json=payload, timeout=10)
        if r.status_code == 200:
            stats["sent"] += 1
            return True
        logger.warning("Telegram send failed (chat %s): HTTP %s — %s", chat_id, r.status_code, r.text[:300])
        stats["failed"] += 1
        if not _plain:
            return _send_one(chat_id, _TAG_RE.sub("", text), _plain=True)
        return False
    except Exception as exc:  # never let a notification failure break the trade flow
        logger.warning("Telegram send error (chat %s): %s", chat_id, exc)
        stats["failed"] += 1
        return False


def send_message(text: str, _plain: bool = False) -> bool:
    """Send a message to every configured chat (one or many). Returns True if at least one delivered.

    On a non-200, logs the reason and retries once in plain text per chat, so a formatting problem
    never silently drops the alert. Silent no-op if the bot token / chat IDs aren't configured.
    """
    if not TELEGRAM_BOT_TOKEN or not _chat_ids():
        return False
    ok_any = False
    for chat_id in _chat_ids():
        ok_any = _send_one(chat_id, text, _plain) or ok_any
    return ok_any


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


def _loc(lead) -> str:
    return _esc(lead.dest_country or "-")


def notify_outreach_sent(lead, subject, kind="Email") -> bool:
    """Alert: an outreach email went out (main email or an auto follow-up) — who + where.
    Muted by default (TELEGRAM_NOTIFY_SENDS): the founder only wants to be pinged on real buyer replies,
    not on every outgoing email. Flip TELEGRAM_NOTIFY_SENDS=true in .env to watch a batch send live."""
    if not TELEGRAM_NOTIFY_SENDS:
        return False
    return send_message(
        f"\U0001F4E4 <b>{_esc(kind)} sent</b> → {_esc(lead.buyer_company) or '-'} ({_loc(lead)})\n"
        f"{_esc(lead.email) or '-'}\n<i>{_esc(subject)}</i>\n{BASE_URL}/leads/{lead.id}")


def notify_send_failed(lead, email, reason) -> bool:
    """Alert: an email could not be sent (bad/missing address or SMTP error) — go find a correct one."""
    return send_message(
        f"⚠️ <b>Email FAILED</b> → {_esc(lead.buyer_company) or '-'} ({_loc(lead)})\n"
        f"{_esc(email) or '(no address on file)'}\n{_esc(reason)[:200]}\n"
        f"Find a correct address → {BASE_URL}/leads/{lead.id}")


def notify_bounce(lead, email, reason, new_email="") -> bool:
    """Alert: a sent email bounced (undeliverable) — the address was wrong."""
    tail = (f"\nNew address found: <b>{_esc(new_email)}</b> — review & resend"
            if new_email else "\nNo new address found automatically — find manually or call.")
    return send_message(
        f"⚠️ <b>Bounced (undeliverable)</b> → {_esc(lead.buyer_company) or '-'} ({_loc(lead)})\n"
        f"{_esc(email)}\n{_esc(reason)[:160]}{tail}\n{BASE_URL}/leads/{lead.id}")


def notify_needs_call(lead) -> bool:
    """Alert: both follow-ups sent, still no reply — time to phone the buyer."""
    return send_message(
        f"\U0001F4DE <b>Time to call</b> — no reply after 2 follow-ups\n"
        f"{_esc(lead.buyer_company) or '-'} ({_loc(lead)})\n"
        f"☎ {_esc(lead.phone) or 'no phone on file'}\n{BASE_URL}/leads/{lead.id}")


def notify_service_request(sr, requester) -> bool:
    """Alert the founder: a trader submitted a concierge request (e.g. a buyer search) to approve.
    Not gated by TELEGRAM_NOTIFY_SENDS — this is exactly what the founder wants to be pinged about."""
    who = _esc(getattr(requester, "name", "") or getattr(requester, "email", "") or "a trader")
    kind = _esc((getattr(sr, "request_type", "") or "request").replace("_", " "))
    where = _esc(sr.market) or "-"
    return send_message(
        f"\U0001F195 <b>New {kind} request</b> from {who}\n"
        f"Product: {_esc(sr.product) or '-'}  →  {where}\n"
        f"{('<i>' + _esc(sr.details)[:160] + '</i>') if sr.details else ''}\n"
        f"Approve → {BASE_URL}/admin/requests")


def notify_request_update(sr, requester=None) -> bool:
    """Alert the requester their request changed state (approved / rejected / delivered). Best-effort:
    routes to the requester's own Telegram if set, else the default chat."""
    labels = {"approved": "✅ approved — research starting",
              "rejected": "❌ not accepted", "done": "\U0001F4E6 delivered"}
    tail = ""
    if sr.status == "done" and sr.leads_delivered:
        tail = f"\n{sr.leads_delivered} buyers delivered — see them under My Buyers"
    elif sr.status == "rejected" and sr.admin_note:
        tail = f"\n{_esc(sr.admin_note)[:160]}"
    text = (f"<b>Your request {_esc(sr.tracking_code)}: {labels.get(sr.status, _esc(sr.status))}</b>\n"
            f"{_esc(sr.product) or '-'}{tail}\n{BASE_URL}/requests")
    chat = str(getattr(requester, "telegram_user_id", "") or "").strip()
    if chat:
        return _send_one(chat, text)      # the requester's OWN Telegram
    # Fail closed: with no personal chat, don't broadcast this trader's request to the shared channel —
    # they see the update in-app. Only fan out to the shared chat if the requester is the admin/founder.
    if requester is not None and getattr(requester, "role", "") == "admin":
        return send_message(text)
    return False


def notify_buyer_reply(lead, from_addr, subject, snippet="") -> bool:
    """Alert: a buyer replied — the founder should answer personally. Leads with a row of money emojis
    so a hot reply jumps out in the chat."""
    snip = f"\n<i>{_esc(snippet)[:180]}</i>" if snippet else ""
    return send_message(
        f"\U0001F4B0\U0001F4B5\U0001F4B0\U0001F4B5\U0001F4B0\U0001F4B5 "
        f"<b>BUYER REPLIED — reply now!</b>\n"
        f"{_esc(lead.buyer_company) or _esc(from_addr)} ({_loc(lead)})\n{_esc(from_addr)}\n"
        f"<b>{_esc(subject)[:90]}</b>{snip}\n{BASE_URL}/leads/{lead.id}")
