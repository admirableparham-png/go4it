"""Daily operations summary (Phase 12) — one Telegram message a day to the ADMIN chat (TELEGRAM_CHAT_ID; never a
seller's own Telegram) about the campaign worker since the previous summary: sends, replies by kind, bounces,
rejections, mailbox + inbox health, open alert tasks and tomorrow's (warm-up) limit.

COUNTS ONLY — never a buyer's company, name or email. Every number comes from the rows the sender, the bounce
breaker and the inbox poller already write, with the same queries they use, and every value is HTML-escaped.
Sent by app.worker.run_daily_summary (DAILY_SUMMARY_AT); `python -m app.worker --daily-summary` prints it.
"""
from collections import Counter
from datetime import datetime, time as dtime, timedelta

from sqlalchemy import and_, or_
from sqlmodel import func, select

from . import campaign_service as CS
from . import local_time as LT
from . import outreach_events as OE
from . import send_guard as SG
from .config import BASE_URL
from .models import (BounceRecord, Campaign, CampaignRecipient, CampaignSend, InboundSeen, IngestionRun, Lead,
                     MailAccount, Outreach, WorkItem)
from .telegram import _esc

MAX_LEN = 3900                      # Telegram caps a message at 4096 characters
HARD_BOUNCES = ("hard", "domain_failure", "spam_complaint")      # what on_bounce treats as an invalid address
ALERT_TYPES = ("campaign_paused", "campaign_warmup_held", "high_bounce_rate", "mailbox_auth_failure",
               "spam_complaint", "review_inbound_reply", "unmatched_inbound")
_SYSTEM_PAUSES = ("bounce breaker:", "render:")   # pause reasons the system writes (an admin's own text isn't echoed)


def _n(session, stmt) -> int:
    return session.exec(stmt).one() or 0


def _error_kind(err) -> str:
    """Only the KIND of the mailbox's last send error (auth/quota/config/transient) — never the provider's text."""
    kind = (err or "").split(":", 1)[0].strip()
    return kind if kind in ("auth", "quota", "config", "transient") else ("other" if err else "")


def _next_send_day(campaign, now, session=None, mailbox=None):
    """The next date the campaign sends on (the same weekday rule as send_guard.within_window): today while its
    window hasn't opened yet, else the first sending day after today. With buyer-local hours: today while today's
    plan still has a buyer to send, else the next UTC day on which any pending buyer has local hours."""
    hours = LT.parse_hours(getattr(campaign, "local_hours", ""))
    if hours and session is not None:
        if mailbox is not None and CS.local_plan(session, campaign, mailbox, now):
            return now.date()
        places = session.exec(
            select(Lead.dest_country, Lead.dest_city).join(CampaignRecipient, CampaignRecipient.lead_id == Lead.id)
            .where(CampaignRecipient.campaign_id == campaign.id,
                   CampaignRecipient.status.not_in(CS.TERMINAL_RECIPIENT)).distinct()).all()
        zones = {(tz, weekend) for tz, _name, weekend in (LT.buyer_zone(c, city) for c, city in places)}
        day0 = now.replace(hour=0, minute=0, second=0, microsecond=0)
        for i in range(1, 8):
            day = day0 + timedelta(days=i)
            if any(LT.windows_on_utc_day(tz, weekend, hours, day) for tz, weekend in zones):
                return day.date()
        return None
    days = {int(d) for d in (campaign.send_days or "").split(",") if d.strip().isdigit()}
    for i in range(0 if now.hour < campaign.send_window_start else 1, 8):
        d = now.date() + timedelta(days=i)
        if not days or d.weekday() in days:
            return d
    return None


def _campaign(session, c, now, since, day0) -> dict:
    sent = select(func.count()).where(CampaignSend.campaign_id == c.id, CampaignSend.status == "sent")
    leads = select(CampaignRecipient.lead_id).where(CampaignRecipient.campaign_id == c.id,
                                                    CampaignRecipient.lead_id.is_not(None))
    emails = select(CampaignRecipient.to_email).where(CampaignRecipient.campaign_id == c.id)
    replies = Counter()
    for subject, body in session.exec(select(Outreach.subject, Outreach.body).where(
            Outreach.direction == "in", Outreach.channel == "email", Outreach.created_at >= since,
            Outreach.created_at < now, Outreach.lead_id.in_(leads))).all():
        own = OE.reply_text(body)              # the buyer's own words, checked in on_reply's order
        replies["unsubscribe" if OE.is_unsubscribe(subject, own) else "auto" if OE.is_auto_reply(subject, own)
                else "human"] += 1
    hard, since_start = CS.bounce_stats(session, c)
    mb = session.get(MailAccount, c.mailbox_id) if c.mailbox_id else None
    out = {
        "id": c.id, "name": c.name, "status": c.status,
        "pause": ("" if c.status != "paused" else (c.pause_reason or "")[:120]
                  if (c.pause_reason or "").startswith(_SYSTEM_PAUSES) else "manual"),
        "day_n": _n(session, select(func.count(func.distinct(func.date(CampaignSend.sent_at)))).where(
            CampaignSend.campaign_id == c.id, CampaignSend.status == "sent")),
        "sent_today": _n(session, sent.where(CampaignSend.sent_at >= day0)), "limit": c.daily_limit,
        "sent_total": _n(session, sent),
        "enrolled": _n(session, select(func.count()).where(CampaignRecipient.campaign_id == c.id)),
        "remaining": _n(session, select(func.count()).where(
            CampaignRecipient.campaign_id == c.id, CampaignRecipient.status.not_in(CS.TERMINAL_RECIPIENT))),
        "replies": {k: replies[k] for k in ("human", "auto", "unsubscribe")},
        "held": [(st.step_index + 1, (st.release_when or "") == CS.RELEASE_EARLIER_DONE)
                 for st in CS.steps_for(session, c) if st.manual_review],
        "hard_new": _n(session, select(func.count()).where(
            BounceRecord.bounce_type.in_(HARD_BOUNCES), BounceRecord.last_bounce_at >= since,
            BounceRecord.last_bounce_at < now, BounceRecord.email_normalized.in_(emails))),
        "hard": hard, "since_start": since_start,
        "rejected_today": _n(session, select(func.count()).where(
            CampaignSend.campaign_id == c.id, CampaignSend.status == "permanently_failed",
            CampaignSend.updated_at >= day0)),
        "needs_review": _n(session, select(func.count()).where(
            CampaignSend.campaign_id == c.id, CampaignSend.status == "unknown_needs_review")),
        "mailbox": None, "next_day": None, "next_limit": c.daily_limit, "warmup": None,
    }
    if mb is not None:
        ok, why = SG.mailbox_ok(mb)
        today = now.strftime("%Y-%m-%d")
        out["mailbox"] = {"email": mb.email, "ok": ok, "why": why, "limit": mb.daily_limit,
                          "sent_today": mb.sent_today if mb.sent_today_date == today else 0,
                          "error": _error_kind(mb.last_send_error)}
        out["next_limit"] = min(c.daily_limit, max(0, mb.daily_limit))
    if c.status == "running":
        nd = _next_send_day(c, now, session, mb)
        out["next_day"] = nd
        decided = nd == now.date() and c.warmup_checked_on == now.strftime("%Y-%m-%d")   # today's step is taken
        if nd is not None and (c.warmup_plan or "").strip() and not decided:
            d = CS.warmup_decision(session, c, mb, datetime.combine(nd, dtime.min), now)   # what that day's run does
            out["warmup"] = d
            if d["action"] == "advance":
                out["next_limit"] = d["to"]
    return out


def collect(session, now, since) -> dict:
    """Everything the summary shows, as plain numbers. `since` = the previous summary (or now - 24 h)."""
    day0 = now.replace(hour=0, minute=0, second=0, microsecond=0)
    camps = session.exec(select(Campaign).where(or_(
        Campaign.status == "running",
        and_(Campaign.status == "paused", Campaign.paused_at >= since),
        and_(Campaign.status == "completed", Campaign.completed_at >= since))).order_by(Campaign.id)).all()
    last = session.exec(select(IngestionRun).where(IngestionRun.source == "email-inbound")
                        .order_by(IngestionRun.id.desc())).first()
    ok_at = CS.last_inbox_success(session, complete_only=True)     # what follow-ups wait for (complete reads only)
    seen = session.exec(select(InboundSeen.outcome, func.count()).where(
        InboundSeen.created_at >= since, InboundSeen.created_at < now, InboundSeen.outcome != "baseline")
        .group_by(InboundSeen.outcome)).all()
    alerts = session.exec(select(WorkItem.type, func.count()).where(
        WorkItem.status.in_(("open", "in_progress", "waiting")), WorkItem.type.in_(ALERT_TYPES))
        .group_by(WorkItem.type)).all()
    return {
        "now": now, "since": since,
        "campaigns": [_campaign(session, c, now, since, day0) for c in camps],
        "paused_all": SG.outreach_paused(session),
        "inbox": {"status": last.status if last else "", "ok_at": ok_at,
                  "problem": ok_at is None or (now - ok_at).total_seconds() > CS.WARMUP_IMAP_FRESH_SEC},
        "inbox_outcomes": {k: v for k, v in sorted(seen) if v},
        "alerts": {k: v for k, v in sorted(alerts) if v},
    }


def _ago(now, then) -> str:
    if then is None:
        return "never"
    mins = max(0, int((now - then).total_seconds() // 60))
    return f"{mins} min ago" if mins < 120 else f"{mins // 60} h ago" if mins < 48 * 60 else f"{mins // 1440} days ago"


def _campaign_block(c) -> str:
    head = f"<b>#{c['id']} {_esc(c['name'])}</b> · {_esc(c['status'])}"
    if c["day_n"]:
        head += f" · sending day {c['day_n']}"
    lines = [head]
    if c["pause"]:
        lines.append(f"Paused: {_esc(c['pause'])}")
    lines.append(f"Sent today {c['sent_today']}/{c['limit']} · total {c['sent_total']} of {c['enrolled']} · "
                 f"{c['remaining']} left")
    r = c["replies"]
    lines.append(f"Replies: {r['human']} human · {r['auto']} auto-reply · {r['unsubscribe']} unsubscribe")
    rate = f"{c['hard'] / c['since_start']:.0%}" if c["since_start"] else "—"
    lines.append(f"Hard bounces: {c['hard_new']} new · {c['hard']}/{c['since_start']} since (re)start = {rate} "
                 f"(breaker {CS.BOUNCE_BREAKER_RATE:.0%} from {CS.BOUNCE_BREAKER_MIN_SENT})")
    lines.append(f"Rejected today: {c['rejected_today']} · needs review: {c['needs_review']}")
    mb = c["mailbox"]
    if mb is None:
        lines.append("Mailbox: none assigned")
    else:
        state = "OK" if mb["ok"] else f"⚠️ {_esc(mb['why'])}"
        if mb["error"]:
            state += f" (last error: {_esc(mb['error'])})"
        lines.append(f"Mailbox {_esc(mb['email'])}: {state} · {mb['sent_today']}/{mb['limit']} today")
    if c["status"] == "running":
        if c["next_day"] is None:
            lines.append("Next: no sending day set")
        else:
            nxt = f"Next: {c['next_day']:%a %d %b} — {c['next_limit']}/day"
            w = c["warmup"]
            if w is not None:
                nxt += {"advance": f" (warm-up {w['from']}→{w['to']}, projected)",
                        "hold": f" (⚠️ warm-up held: {_esc(w['why'])})",
                        "wait": f" (warm-up waits: {_esc(w['why'])})"}.get(w["action"], f" (warm-up: {_esc(w['why'])})")
            lines.append(nxt)
    for n, auto in c.get("held", []):
        lines.append(f"Email {n}: held — starts by itself once every earlier email is out" if auto
                     else f"Email {n}: held — waits for your approval")
    lines.append(f"{BASE_URL}/campaigns/{c['id']}")
    return "\n".join(lines)


def render(d) -> str:
    """The Telegram text (parse_mode=HTML), at most MAX_LEN characters: campaign blocks that don't fit are counted,
    never cut mid-tag."""
    now, since = d["now"], d["since"]
    head = f"📊 <b>Daily summary</b> — {now:%a %d %b %H:%M} UTC\n<i>since {since:%a %d %b %H:%M} UTC</i>"
    ib = d["inbox"]
    tail = [("⚠️ " if ib["problem"] else "") + f"Inbox reading: last success {_ago(now, ib['ok_at'])}"
            + (f" (last poll: {_esc(ib['status'])})" if ib["status"] and ib["status"] != "ok" else "")]
    if d["inbox_outcomes"]:
        tail.append("Inbox since then: " + " · ".join(f"{_esc(k)} {v}" for k, v in d["inbox_outcomes"].items()))
    if d["alerts"]:
        from .work_queue import TYPE_LABELS
        tail.append("Open tasks: " + " · ".join(f"{_esc(TYPE_LABELS.get(k, k))} {v}" for k, v in d["alerts"].items()))
    if d["paused_all"]:
        tail.append("⛔ Pause-All is ON — nothing is being sent")
    if not d["campaigns"]:
        tail.insert(0, "No running campaign.")
    foot = "\n".join(tail)
    parts, used, left = [head], len(head) + len(foot) + 80, 0
    for c in d["campaigns"]:
        block = _campaign_block(c)
        if left or used + len(block) + 2 > MAX_LEN:
            left += 1
            continue
        parts.append(block)
        used += len(block) + 2
    if left:
        parts.append(f"… {left} more campaign(s): {BASE_URL}/campaigns")
    return "\n\n".join(parts + [foot])[:4096]


def build(session, now, since) -> tuple:
    """(text, has_news). Nothing to report = no campaign running or changed, no inbox activity, no open alert task
    and reply reading healthy — the worker then marks the day without sending."""
    d = collect(session, now, since)
    news = bool(d["campaigns"] or d["inbox_outcomes"] or d["alerts"] or d["inbox"]["problem"])
    return render(d), news
