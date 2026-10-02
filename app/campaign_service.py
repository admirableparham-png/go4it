"""Campaign engine (Phase 4) — audience building, idempotent enrolment, sequence versioning and the
send-safety chain. A Campaign is NEVER an independent contact database: recipients come only from the Trade
Network (managed buyer Leads + their canonical Company/Contact). Two-way confidentiality and suppression are
enforced on every send here (buyers never learn the seller; suppressed/replied addresses are never sent).
"""
import os
import re
import secrets
from datetime import datetime, timedelta

from sqlalchemy import update as _sa_update
from sqlalchemy.exc import IntegrityError
from sqlmodel import func, select

from . import campaign_render as CR
from . import local_time as LT
from . import pipeline
from . import send_guard as SG
from . import suppression as SUP
from .models import Campaign, CampaignRecipient, CampaignSend, CampaignStep, IngestionRun, Lead, Outreach

# --- crash-safe send lifecycle (Phase 4 hardening) ---
RETRY_MAX = int(os.getenv("CAMPAIGN_SEND_RETRY_MAX", "5"))            # attempts before permanently_failed
LEASE_SECONDS = int(os.getenv("CAMPAIGN_SEND_LEASE_SEC", "120"))     # a claim is valid this long
RETRY_BACKOFF_SEC = int(os.getenv("CAMPAIGN_SEND_BACKOFF_SEC", "300"))  # linear backoff unit between retries
SEND_TERMINAL = ("sent", "permanently_failed", "unknown_needs_review")  # never auto-reclaimed

CAMPAIGN_STATUSES = ["draft", "ready_for_review", "scheduled", "running", "paused", "completed",
                     "cancelled", "archived"]
RECIPIENT_STATUSES = ["pending", "ready", "sent", "delivered", "soft_bounced", "hard_bounced", "replied",
                      "positive_reply", "negative_reply", "follow_up_later", "unsubscribed", "suppressed",
                      "completed", "skipped"]
TERMINAL_RECIPIENT = ("replied", "positive_reply", "negative_reply", "unsubscribed", "hard_bounced",
                      "suppressed", "completed", "skipped")


# --------------------------------------------------------------------- audience (Trade Network only)
def audience_leads(session, tenant_id, f: dict):
    """The candidate buyer Leads for a campaign's tenant, filtered by Trade Network facets. Recipients only
    ever come from here — managed buyer records with a real contact. `f` is the filter dict from the builder."""
    stmt = select(Lead)
    # scope: the seller the campaign is for (managed buyers). Internal campaigns (tenant None) span the pool.
    if tenant_id is not None:
        stmt = stmt.where(Lead.seller_id == tenant_id, Lead.managed == True)   # noqa: E712
    else:
        stmt = stmt.where(Lead.owner_id.is_(None) if f.get("managed_only") else Lead.id.is_not(None))
    if f.get("product"):
        stmt = stmt.where(Lead.product.ilike(f"%{f['product']}%"))
    if f.get("category"):
        stmt = stmt.where(Lead.category == f["category"])
    if f.get("country"):
        stmt = stmt.where(Lead.dest_country == f["country"])
    if f.get("source"):
        stmt = stmt.where(Lead.source == f["source"])
    if f.get("engagement"):
        stmt = stmt.where(Lead.engagement_class == f["engagement"])
    if f.get("assigned_admin") and str(f["assigned_admin"]).isdigit():
        stmt = stmt.where(Lead.assigned_admin_id == int(f["assigned_admin"]))
    if f.get("request_id") and str(f["request_id"]).isdigit():
        stmt = stmt.where(Lead.request_id == int(f["request_id"]))
    if f.get("lead_ids") is not None:      # an exact set (scripts only — the builder UI never sets it); [] = none
        stmt = stmt.where(Lead.id.in_([int(i) for i in f["lead_ids"]]))
    rows = session.exec(stmt.order_by(Lead.id)).all()
    # exclusions (evidence-based)
    now = datetime.utcnow()
    recent_days = int(f.get("recent_days", 14) or 14)
    out = []
    for ld in rows:
        if f.get("exclude_customers") and (ld.engagement_class == "customer" or ld.status == "won"):
            continue
        if f.get("exclude_negative") and ld.reply_outcome == "negative":
            continue
        if f.get("exclude_negotiating") and (ld.status == "negotiating"
                                             or ld.pipeline_stage == "negotiating"):
            continue
        if f.get("exclude_recent") and _last_out(session, ld.id, since=now - timedelta(days=recent_days)):
            continue
        out.append(ld)
    return out


def _last_out(session, lead_id, since=None):
    stmt = select(func.count()).where(Outreach.lead_id == lead_id, Outreach.direction == "out",
                                      Outreach.channel == "email", Outreach.status == "sent")
    if since is not None:
        stmt = stmt.where(Outreach.created_at >= since)
    return session.exec(stmt).one() > 0


_EMAIL_RE = re.compile(r"^[^@\s,;<>]+@[^@\s,;<>]+\.[a-z]{2,}$", re.I)


def audience_preview(session, campaign, f: dict) -> dict:
    """The pre-launch breakdown an admin confirms before recipient records are created."""
    leads = audience_leads(session, campaign.tenant_id, f)
    existing = {r.lead_id for r in session.exec(select(CampaignRecipient)
                .where(CampaignRecipient.campaign_id == campaign.id)).all()}
    seen_email, dupes, valid, missing, suppressed, prev_contacted, already = set(), 0, 0, 0, 0, 0, 0
    invalid = 0
    eligible = []
    for ld in leads:
        em = SUP.normalize_email(ld.email)
        if not em:
            missing += 1
            continue
        if not _EMAIL_RE.match(em):          # a phone/URL/two addresses in the email field never becomes a send
            invalid += 1
            continue
        if em in seen_email:
            dupes += 1
            continue
        seen_email.add(em)
        valid += 1
        if SUP.is_suppressed(session, em, tenant_id=campaign.tenant_id):
            suppressed += 1
            continue
        if _last_out(session, ld.id):
            prev_contacted += 1
        if ld.id in existing:
            already += 1
            continue
        eligible.append(ld)
    return {"total_companies": len(leads), "valid_email": valid, "missing_email": missing,
            "invalid_email": invalid, "suppressed": suppressed, "previously_contacted": prev_contacted,
            "already_in_campaign": already, "duplicates": dupes, "final_eligible": len(eligible),
            "eligible_lead_ids": [ld.id for ld in eligible]}


def enroll(session, campaign, actor, f: dict, expected=None) -> dict:
    """Create durable recipient records for the eligible audience — idempotent (unique campaign_id+contact/
    lead), never enrolling a suppressed or duplicate address. Requires the admin to have confirmed the
    preview: when `expected` (the count the admin saw) no longer matches, nothing is enrolled.
    Returns {created, skipped_suppressed, skipped_existing[, error]}."""
    prev = audience_preview(session, campaign, f)
    if expected is not None and prev["final_eligible"] != expected:
        return {"created": 0, "skipped_suppressed": 0, "skipped_existing": 0,
                "error": f"the audience changed since the preview ({expected} → {prev['final_eligible']}) — "
                         "preview again"}
    created = skipped_sup = skipped_dupe = 0
    for lid in prev["eligible_lead_ids"]:
        ld = session.get(Lead, lid)
        if not ld:
            continue
        em = SUP.normalize_email(ld.email)
        if not em or SUP.is_suppressed(session, em, tenant_id=campaign.tenant_id):
            skipped_sup += 1
            continue
        try:
            with session.begin_nested():
                session.add(CampaignRecipient(
                    campaign_id=campaign.id, tenant_id=campaign.tenant_id, company_id=ld.company_id,
                    contact_id=None, lead_id=ld.id, request_id=ld.request_id, to_email=em,
                    sequence_version=campaign.sequence_version, current_step=0, status="pending"))
            created += 1
        except IntegrityError:
            skipped_dupe += 1
    session.commit()
    try:
        pipeline.audit(session, actor, "campaign", campaign.id, "enroll",
                       {"created": created}, tenant_id=campaign.tenant_id)
    except Exception:  # noqa: BLE001
        pass
    return {"created": created, "skipped_suppressed": skipped_sup, "skipped_existing": skipped_dupe}


# --------------------------------------------------------------------- sequence versioning
def steps_for(session, campaign, version=None):
    v = campaign.sequence_version if version is None else version
    return session.exec(select(CampaignStep).where(CampaignStep.campaign_id == campaign.id,
                        CampaignStep.version == v).order_by(CampaignStep.step_index)).all()


def set_sequence(session, campaign, steps: list, actor=None) -> int:
    """Replace the campaign's sequence. If the campaign is RUNNING, this creates a NEW version (old steps and
    already-sent messages are preserved; only NEW recipients / next steps use the new version) and requires
    the caller to have confirmed. Returns the version written."""
    running = campaign.status == "running"
    version = campaign.sequence_version + 1 if running else campaign.sequence_version
    if running:
        campaign.sequence_version = version
        campaign.updated_at = datetime.utcnow()
        session.add(campaign)
    else:
        for st in steps_for(session, campaign, version):   # draft edit: clear the current version's steps
            session.delete(st)
        session.flush()        # deletes must hit the DB before the re-inserted steps reuse their (version, index)
    for i, s in enumerate(steps):
        raw_html = (s.get("body_html") or "")[:100_000]
        body_html = CR.sanitize_html(raw_html) if raw_html.strip() else ""
        # the text part is required; an HTML-only step gets its text part derived from the design
        body = (s.get("body") or "")[:8000] or (CR.html_to_text(body_html)[:8000] if body_html else "")
        session.add(CampaignStep(campaign_id=campaign.id, version=version, step_index=i,
                                 subject=SG.sanitize_header((s.get("subject") or "")[:200]),
                                 body=body, body_html=body_html, template_id=s.get("template_id"),
                                 attachment_path=(s.get("attachment_path") or "")[:300],
                                 plain_text_only=bool(s.get("plain_text_only")),
                                 list_unsubscribe=bool(s.get("list_unsubscribe", True)),
                                 delay_days=int(s.get("delay_days") or 0),
                                 manual_review=bool(s.get("manual_review"))))
    session.commit()
    try:
        pipeline.audit(session, actor, "campaign", campaign.id, "set_sequence",
                       {"version": version, "running_edit": running}, tenant_id=campaign.tenant_id)
    except Exception:  # noqa: BLE001
        pass
    return version


def start_problems(session, campaign) -> list:
    """Everything that must hold before a campaign may RUN — the start route refuses (and the dry-run reports)
    while this list is non-empty: a healthy Go4it mailbox with its footer, valid steps, a daily limit, and reply
    reading switched on (the 'reply unsubscribe' promise in every footer depends on it)."""
    from . import config
    from .models import MailAccount
    probs = []
    if not campaign.request_id:
        probs.append("the campaign is not linked to a request — its audience would not be scoped to one seller")
    mb = session.get(MailAccount, campaign.mailbox_id) if campaign.mailbox_id else None
    ok, why = SG.mailbox_ok(mb)
    if not ok:
        probs.append(f"mailbox: {why}")
    steps = steps_for(session, campaign)
    if not steps:
        probs.append("no email sequence yet")
        if mb is not None:
            probs += CR.validate_sender(mb)
    for st in steps:
        for e in CR.template_problems(session, campaign, st, mb):
            p = f"email {st.step_index + 1}: {e}" if not e.startswith(("the mailbox", "no sending")) else e
            if p not in probs:
                probs.append(p)
    if campaign.daily_limit <= 0:
        probs.append("the campaign's daily limit is 0")
    if not (config.IMAP_ENABLED and config.IMAP_INTERVAL > 0):
        probs.append("reply reading (IMAP) is off — the footer promises 'reply unsubscribe', so it must be on")
    elif mb is not None and (mb.email or "").strip().lower() != (config.IMAP_USER or "").strip().lower():
        probs.append(f"replies to {mb.email} are not read — IMAP polls {config.IMAP_USER or 'another mailbox'}")
    elif mb is not None and not config.IMAP_PASSWORD and not mb.smtp_password_enc:
        probs.append("reply reading has no password — connect the mailbox on /mail (or set IMAP_PASSWORD)")
    return probs


# --------------------------------------------------------------------- follow-ups (Phase 12)
# A follow-up is the NEXT step of the SAME sequence version, sent as a reply in the thread of the emails the buyer
# really got. Adding one to a live campaign never re-versions it (set_sequence on a running campaign makes a version
# no enrolled buyer is on): append_step adds the email in place while the campaign is paused, reopen_completed gives
# it to the buyers who had already finished, and a held step (manual_review) waits — using no send slot — until
# approve_step releases it. Ops entry point: scripts/campaign_followup.py.
FINAL_CAMPAIGN = ("cancelled", "archived")


def thread_anchor(session, campaign, rcpt) -> dict:
    """The thread a follow-up replies in: every email of this campaign the recipient was really sent (any version),
    oldest first. {} when there is none — a follow-up then has nothing to reply to and is never sent.
    {in_reply_to: the newest Message-ID, references: all of them, subject: the first email's subject as sent,
    last_sent_at: when the newest went out}."""
    rows = session.exec(select(CampaignSend).where(
        CampaignSend.campaign_id == campaign.id, CampaignSend.recipient_id == rcpt.id,
        CampaignSend.status == "sent", CampaignSend.rfc_message_id != "")
        .order_by(CampaignSend.sent_at, CampaignSend.id)).all()
    if not rows:
        return {}
    ids = [cs.rfc_message_id for cs in rows]
    first = session.exec(select(Outreach).where(Outreach.message_id == ids[0], Outreach.direction == "out")).first()
    return {"in_reply_to": ids[-1], "references": " ".join(ids), "subject": first.subject if first else "",
            "last_sent_at": rows[-1].sent_at or rows[-1].updated_at}


def _followup_block(session, campaign, rcpt, lead) -> str:
    """Why this recipient may get NO further email (the recipient-level part of can_send), or ''."""
    if rcpt.suppressed or SUP.is_suppressed(session, rcpt.to_email, tenant_id=campaign.tenant_id):
        return "suppressed"
    if rcpt.reply_outcome or (lead is not None and lead.buyer_replied_at is not None):
        return "replied"
    if rcpt.soft_bounce_count > 0:
        return "soft-bounced"
    if lead is None:
        return "no buyer record"
    if not (lead.email or "").strip():
        return "no active contact email"
    return ""


def build_step(campaign, version, index, s) -> CampaignStep:
    """A CampaignStep (not added to the session) from a step dict, normalized exactly as set_sequence stores one."""
    raw_html = (s.get("body_html") or "")[:100_000]
    body_html = CR.sanitize_html(raw_html) if raw_html.strip() else ""
    body = (s.get("body") or "")[:8000] or (CR.html_to_text(body_html)[:8000] if body_html else "")
    return CampaignStep(campaign_id=campaign.id, version=version, step_index=index,
                        subject=SG.sanitize_header((s.get("subject") or "")[:200]), body=body, body_html=body_html,
                        template_id=s.get("template_id"), attachment_path=(s.get("attachment_path") or "")[:300],
                        plain_text_only=bool(s.get("plain_text_only")),
                        list_unsubscribe=bool(s.get("list_unsubscribe", True)),
                        delay_days=max(0, int(s.get("delay_days") or 0)), manual_review=bool(s.get("manual_review")))


def same_email(steps, row):
    """The step among `steps` with exactly row's subject + text + HTML (a re-run adding the same email), or None."""
    return next((st for st in steps if (st.subject, st.body, st.body_html) == (row.subject, row.body, row.body_html)),
                None)


def _step_tried(session, campaign, version, index) -> int:
    """How many sends of this step exist at all (sent, failed or in flight) — 0 = it never went to anyone."""
    return session.exec(select(func.count()).where(CampaignSend.campaign_id == campaign.id,
                                                   CampaignSend.sequence_version == version,
                                                   CampaignSend.step_index == index)).one()


def append_step(session, campaign, step: dict, actor=None, apply=True) -> dict:
    """Add ONE email to the END of the campaign's CURRENT sequence version, in place. The emails already there are never
    touched and recipients stay on their version, so the per-version duplicate guard keeps covering what was sent.
    Refused while the campaign is running (pause it first), for a cancelled/archived campaign, one without a first
    email, when the next index is taken, or behind a held email that never went out (replace that draft instead);
    validated like the send path (merge fields, internal brand, seller names, the PDF, the mailbox).
    step['manual_review']=True holds it until approve_step. Idempotent: the same email (subject + text + HTML) already
    in the sequence is not added again. apply=False only checks. Returns {step_index, added, error}."""
    out = {"step_index": None, "added": False, "error": ""}
    if apply and campaign.status == "running":
        out["error"] = "pause the campaign first — a running campaign's sequence is never edited in place"
        return out
    if campaign.status in FINAL_CAMPAIGN:
        out["error"] = f"the campaign is {campaign.status}"
        return out
    v = campaign.sequence_version
    existing = steps_for(session, campaign, v)
    if not existing:
        out["error"] = "the campaign has no first email yet — set its sequence first"
        return out
    row = build_step(campaign, v, len(existing), step)
    same = same_email(existing, row)
    if same is not None:                                  # a re-run with the same email: nothing to add
        out["step_index"] = same.step_index
        return out
    if any(st.step_index >= row.step_index for st in existing):
        out["error"] = f"email {row.step_index + 1} already exists in sequence v{v}"
        return out
    last = existing[-1]
    if last.manual_review and not _step_tried(session, campaign, v, last.step_index):
        # a revised draft must REPLACE the held one — never queue behind it (approving it would send the old text)
        out["error"] = (f"email {last.step_index + 1} is still held and never sent — approve it first, or change its "
                        "text with replace_held_step (campaign_followup.py add … --replace)")
        return out
    from .models import MailAccount
    mb = session.get(MailAccount, campaign.mailbox_id) if campaign.mailbox_id else None
    errs = CR.template_problems(session, campaign, row, mb)
    if errs:
        out["error"] = "; ".join(errs)
        return out
    out["step_index"] = row.step_index
    if not apply:
        return out
    session.add(row)
    try:
        pipeline.audit(session, actor, "campaign", campaign.id, "append_step",
                       {"version": v, "step_index": row.step_index, "delay_days": row.delay_days,
                        "manual_review": row.manual_review}, tenant_id=campaign.tenant_id)
    except Exception:  # noqa: BLE001
        pass
    session.commit()
    out["added"] = True
    return out


def replace_held_step(session, campaign, step: dict, actor=None, apply=True) -> dict:
    """Change the text of the LAST email while it is still HELD and has never gone to anyone (a draft revised before
    its approval). It stays held — new text, new approval — and keeps its delay, which every waiting buyer's due date
    already counts. Not while the campaign is running; validated like append_step. apply=False only checks.
    Returns {step_index, replaced, error}."""
    out = {"step_index": None, "replaced": False, "error": ""}
    v = campaign.sequence_version
    steps = steps_for(session, campaign, v)
    last = steps[-1] if len(steps) > 1 else None
    if apply and campaign.status == "running":
        out["error"] = "pause the campaign first — a running campaign's sequence is never edited in place"
    elif campaign.status in FINAL_CAMPAIGN:
        out["error"] = f"the campaign is {campaign.status}"
    elif last is None:
        out["error"] = "there is no follow-up to replace"
    elif not last.manual_review:
        out["error"] = f"email {last.step_index + 1} is approved — its text no longer changes"
    elif _step_tried(session, campaign, v, last.step_index):
        out["error"] = f"email {last.step_index + 1} already went out — its text no longer changes"
    if out["error"]:
        return out
    row = build_step(campaign, v, last.step_index, dict(step, manual_review=True))
    if row.delay_days != last.delay_days:
        out["error"] = (f"email {last.step_index + 1} keeps its {last.delay_days}-day delay — every waiting buyer's "
                        "due date already counts it")
        return out
    from .models import MailAccount
    mb = session.get(MailAccount, campaign.mailbox_id) if campaign.mailbox_id else None
    errs = CR.template_problems(session, campaign, row, mb)
    if errs:
        out["error"] = "; ".join(errs)
        return out
    out["step_index"] = last.step_index
    if not apply:
        return out
    for k in ("subject", "body", "body_html", "attachment_path", "plain_text_only", "list_unsubscribe"):
        setattr(last, k, getattr(row, k))
    session.add(last)
    try:
        pipeline.audit(session, actor, "campaign", campaign.id, "replace_held_step",
                       {"version": v, "step_index": last.step_index}, tenant_id=campaign.tenant_id)
    except Exception:  # noqa: BLE001
        pass
    session.commit()
    out["replaced"] = True
    return out


def reopen_completed(session, campaign, apply=False, actor=None, steps=None) -> dict:
    """Give an appended follow-up to the buyers who had already FINISHED the sequence: each 'completed' recipient with
    a next email in its version becomes 'sent' again, due at the time of the last email it really got + that email's
    delay_days (the same per-buyer timing as a normal step). Never a buyer that replied, bounced (hard or soft),
    unsubscribed or is suppressed, and never one without a sent email to reply to. Dry run unless apply=True;
    idempotent (a re-opened recipient is no longer 'completed'). `steps` previews the current version with an email
    that is not added yet (dry run only).
    Returns {eligible, reopened, due: [(recipient_id, due_at)], left: {why: count}}."""
    if apply and steps is not None:
        raise ValueError("apply re-opens against the stored sequence only")
    out = {"eligible": 0, "reopened": 0, "due": [], "left": {}}
    if campaign.status in FINAL_CAMPAIGN:
        out["error"] = f"the campaign is {campaign.status}"
        return out
    by_version = {campaign.sequence_version: list(steps)} if steps is not None else {}
    due, left = [], {}
    for r in session.exec(select(CampaignRecipient).where(CampaignRecipient.campaign_id == campaign.id,
                          CampaignRecipient.status == "completed").order_by(CampaignRecipient.id)).all():
        if r.sequence_version not in by_version:
            by_version[r.sequence_version] = steps_for(session, campaign, r.sequence_version)
        seq = by_version[r.sequence_version]
        why = "no next email" if r.current_step >= len(seq) else \
            _followup_block(session, campaign, r, session.get(Lead, r.lead_id) if r.lead_id else None)
        anchor = {} if why else thread_anchor(session, campaign, r)
        why = why or ("" if anchor else "no sent email to reply to")
        if why:
            left[why] = left.get(why, 0) + 1
            continue
        due.append((r, anchor["last_sent_at"] + timedelta(days=max(0, seq[r.current_step].delay_days))))
    out.update(eligible=len(due), due=[(r.id, at) for r, at in due], left=left)
    if apply and due:
        now = datetime.utcnow()
        for r, at in due:
            r.status, r.next_action_at, r.updated_at = "sent", at, now
            session.add(r)
        try:
            pipeline.audit(session, actor, "campaign", campaign.id, "reopen_completed",
                           {"reopened": len(due)}, tenant_id=campaign.tenant_id)
        except Exception:  # noqa: BLE001
            pass
        session.commit()
        out["reopened"] = len(due)
    return out


def approve_step(session, campaign, step_index, actor=None, version=None) -> tuple:
    """Release a HELD email (manual_review): the worker sends it from its next cycle, to each buyer when due.
    Idempotent (an approved email stays approved, audited once). Refuses an email that does not exist, one that would
    not render safely, and a cancelled/archived campaign. Returns (ok, message)."""
    if campaign.status in FINAL_CAMPAIGN:
        return False, f"the campaign is {campaign.status}"
    v = campaign.sequence_version if version is None else version
    st = next((x for x in steps_for(session, campaign, v) if x.step_index == step_index), None)
    if st is None:
        return False, f"there is no email {step_index + 1} in sequence v{v}"
    if not st.manual_review:
        return True, f"email {step_index + 1} is already approved"
    from .models import MailAccount
    mb = session.get(MailAccount, campaign.mailbox_id) if campaign.mailbox_id else None
    errs = CR.template_problems(session, campaign, st, mb)
    if errs:
        return False, "; ".join(errs)
    st.manual_review = False
    session.add(st)
    try:
        pipeline.audit(session, actor, "campaign", campaign.id, "approve_step",
                       {"version": v, "step_index": step_index}, tenant_id=campaign.tenant_id)
    except Exception:  # noqa: BLE001
        pass
    session.commit()
    return True, f"email {step_index + 1} approved"


# --------------------------------------------------------------------- send safety + idempotent send
def _recipient_problem(session, campaign, rcpt) -> str:
    """The recipient-level rules (not timing, not quotas): '' when this recipient may get its current email."""
    if rcpt.suppressed or rcpt.status in TERMINAL_RECIPIENT:
        return f"recipient {rcpt.status}"
    if SUP.is_suppressed(session, rcpt.to_email, tenant_id=campaign.tenant_id):
        return "suppressed"
    ld = session.get(Lead, rcpt.lead_id) if rcpt.lead_id else None
    if ld is not None and ld.buyer_replied_at is not None:
        return "already replied"
    if ld is not None and not (ld.email or "").strip():
        return "no active contact email"
    steps = steps_for(session, campaign, rcpt.sequence_version)
    if rcpt.current_step >= len(steps):
        return "sequence complete"
    if rcpt.current_step > 0 and rcpt.soft_bounce_count > 0:
        return "soft-bounced"               # an email to this address already bounced — no follow-up chases it
    if steps[rcpt.current_step].manual_review:
        return "manual-review step"
    return ""


def _remaining_today(session, campaign, mailbox, now) -> int:
    """How many more emails this campaign may send today (UTC day): its own limit minus its own sends today, never
    more than the mailbox has left. The MAILBOX limit counts everything the mailbox sent today; the CAMPAIGN limit
    counts only this campaign's sends (so tests or another campaign never eat a campaign's warm-up quota)."""
    today = now.strftime("%Y-%m-%d")
    used = mailbox.sent_today if mailbox.sent_today_date == today else 0
    day_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    mine = session.exec(select(func.count()).where(
        CampaignSend.campaign_id == campaign.id, CampaignSend.status == "sent",
        CampaignSend.sent_at >= day_start)).one()
    return min(max(0, mailbox.daily_limit) - used, max(0, campaign.daily_limit) - mine)


def can_send(session, campaign, rcpt, mailbox, now=None, inbox_fresh=None, local_ok=None) -> tuple:
    """The full pre-send safety chain. Returns (ok, reason). Verified for EVERY send. `inbox_fresh` = the cycle's
    reply_reading_fresh() (the worker works it out once per campaign); None = check it here. For a campaign with
    buyer-local hours, `local_ok` = the recipient is in this cycle's local_plan as 'now' (None = work it out here)."""
    now = now or datetime.utcnow()
    if SG.outreach_paused(session):
        return False, "outreach paused"
    if campaign.status != "running":
        return False, f"campaign not running ({campaign.status})"
    ok, why = SG.mailbox_ok(mailbox)
    if not ok:
        return False, why
    why = _recipient_problem(session, campaign, rcpt)
    if why:
        return False, why
    local = bool(LT.parse_hours(getattr(campaign, "local_hours", "")))
    if not local and not SG.within_window(campaign, now):
        return False, "outside sending window"
    # daily-limit precheck (the mailbox slot is actually consumed in send_step)
    if _remaining_today(session, campaign, mailbox, now) <= 0:
        return False, "daily limit reached"
    if local:
        # each buyer in THEIR business hours, in ranked order (not campaign-level: other buyers' hours may be open)
        if not (local_plan(session, campaign, mailbox, now, inbox_fresh).get(rcpt.id) == "now"
                if local_ok is None else local_ok):
            return False, "outside the buyer's local hours"
    if rcpt.current_step > 0:
        # a follow-up only goes while reply/bounce reading is working: with IMAP down a buyer's reply or bounce isn't
        # seen, and the follow-up would chase someone who already answered. Checked last, so the window/limit reasons
        # still stop the whole cycle first; not campaign-level, so first emails go on.
        if not (reply_reading_fresh(session, now) if inbox_fresh is None else inbox_fresh):
            return False, "reply reading stale"
    return True, ""


def local_plan(session, campaign, mailbox, now=None, inbox_fresh=None, settle=False) -> dict:
    """{recipient id: 'now' | 'later'} — see local_slots."""
    return {rid: state for rid, (state, _end) in local_slots(session, campaign, mailbox, now, inbox_fresh,
                                                             settle).items()}


def local_slots(session, campaign, mailbox, now=None, inbox_fresh=None, settle=False) -> dict:
    """For a campaign with buyer-local hours: {recipient id: ('now' | 'later', window end)} — the rest of today's (UTC
    day) quota, reserved for the best-ranked recipients (lowest id first) that may get their next email and whose
    local hours still come today (or are open now); 'now' = inside one of its windows and due. So a top-ranked buyer in
    Auckland keeps its place even though Europe's mornings come first in the UTC day. A recipient whose current email
    can't go out now (already sent, waiting for an admin's review after a crash, being sent by another cycle, or in a
    retry back-off past its window) never takes a slot. With `settle` (the worker) a recipient that can never be sent
    again is settled to its terminal status, so the campaign can finish. Read-only otherwise."""
    now = now or datetime.utcnow()
    hours = LT.parse_hours(getattr(campaign, "local_hours", ""))
    if not hours or mailbox is None:
        return {}
    remaining = _remaining_today(session, campaign, mailbox, now)
    if remaining <= 0:
        return {}
    day_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    sends = {(cs.recipient_id, cs.sequence_version, cs.step_index): cs for cs in session.exec(
        select(CampaignSend).where(CampaignSend.campaign_id == campaign.id)).all()}
    fresh, plan = inbox_fresh, {}
    rows = session.exec(select(CampaignRecipient, Lead.dest_country, Lead.dest_city)
                        .join(Lead, Lead.id == CampaignRecipient.lead_id, isouter=True)
                        .where(CampaignRecipient.campaign_id == campaign.id,
                               CampaignRecipient.status.not_in(TERMINAL_RECIPIENT))
                        .order_by(CampaignRecipient.id)).all()
    for rcpt, country, city in rows:
        cs = sends.get((rcpt.id, rcpt.sequence_version, rcpt.current_step))
        due = rcpt.next_action_at
        if cs is not None:
            if cs.status in SEND_TERMINAL:
                continue                     # done, or held for an admin's review after a crash — never a slot
            if cs.status in ("claimed", "sending") and cs.lease_expires_at and cs.lease_expires_at > now:
                continue                     # another cycle is sending it right now
            if cs.status in ("pending", "retryable") and cs.next_attempt_at and cs.next_attempt_at > now:
                due = max(due, cs.next_attempt_at) if due else cs.next_attempt_at   # in retry back-off
        tz, _zone, weekend = LT.buyer_zone(country, city)
        slot = next(((s, e) for d in (day_start - timedelta(days=1), day_start)
                     for s, e in LT.windows_on_utc_day(tz, weekend, hours, d)
                     if e > now and (due is None or due < e)), None)
        if slot is None:
            continue
        why = _recipient_problem(session, campaign, rcpt)
        if why:
            if settle:
                _settle_skip(session, rcpt, why, now)
            continue
        if rcpt.current_step > 0:
            fresh = reply_reading_fresh(session, now) if fresh is None else fresh
            if not fresh:
                continue
        plan[rcpt.id] = ("now" if slot[0] <= now and (due is None or due <= now) else "later", slot[1])
        if len(plan) >= remaining:
            break
    return plan


def classify_send_error(err) -> str:
    """Map an SMTP failure to 'retryable' or 'permanent'. Transient/4xx/timeout/connection → retryable;
    a hard 5xx bad-recipient → permanent. Unknown errors are retryable (bounded by RETRY_MAX) — a genuine
    hard bounce later suppresses the address through the bounce path, so no message chases a dead box forever."""
    e = (err or "").lower()
    permanent = ("no such user", "does not exist", "user unknown", "invalid recipient", "recipient rejected",
                 "mailbox unavailable", "address rejected", "550", "551", "553", "5.1.1", "5.1.0", "5.1.3")
    if any(k in e for k in permanent):
        return "permanent"
    return "retryable"


# Phase 11: failures of the MAILBOX, not the recipient — checked before classify_send_error. Gmail's
# "550 5.4.5 daily sending quota exceeded" contains "550" and would otherwise permanently fail every buyer it hits.
_MAILBOX_ERRORS = (
    ("auth", re.compile(r"\(53[045],|\b5\.7\.(?:8|9|14)\b|username and password not accepted|"
                        r"application-specific password|authentication (?:failed|required|unsuccessful)", re.I)),
    ("quota", re.compile(r"\b5\.4\.5\b|\b4\.7\.28\b|sending (?:quota|limit)|unusual rate|too many messages|"
                         r"too many login attempts|\(454,", re.I)),
    ("config", re.compile(r"mailbox not connected|no stored password|credential encryption|sender error|"
                          r"\b5\.7\.26\b|^login error:", re.I)),
    # never reached the handover (DNS, refused, timeout, STARTTLS, 421 on connect): not the buyer's fault and not a
    # reason to stop the mailbox — retry later, charge nothing
    ("transient", re.compile(r"^connect error:", re.I)),
)


def classify_mailbox_error(err) -> str:
    """'auth' | 'quota' | 'config' | 'transient' when the failure is the sending mailbox's (every later send would
    fail the same way right now), else '' (a buyer-level failure)."""
    for kind, pat in _MAILBOX_ERRORS:
        if pat.search(err or ""):
            return kind
    return ""


# can_send reasons that hold for the whole campaign/mailbox this cycle — the worker stops the campaign's loop
CAMPAIGN_LEVEL_SKIPS = ("outreach paused", "campaign not running", "no mailbox", "not a Go4it admin mailbox",
                        "mailbox paused or disabled", "outside sending window", "daily limit reached")
# can_send reasons that are permanent for the recipient — settled to a terminal status so the campaign can finish
_TERMINAL_SKIP = {"suppressed": "suppressed", "already replied": "replied", "no active contact email": "skipped",
                  "sequence complete": "completed", "soft-bounced": "skipped"}


def is_campaign_level_skip(reason) -> bool:
    return (reason or "").startswith(CAMPAIGN_LEVEL_SKIPS)


def _settle_skip(session, rcpt, reason, now):
    status = _TERMINAL_SKIP.get(reason)
    if status and rcpt.status != status:
        rcpt.status = status
        rcpt.suppressed = rcpt.suppressed or status == "suppressed"
        rcpt.next_action_at = None
        rcpt.updated_at = now
        session.add(rcpt); session.commit()


BOUNCE_BREAKER_MIN_SENT = int(os.getenv("CAMPAIGN_BOUNCE_BREAKER_MIN_SENT", "20"))
BOUNCE_BREAKER_RATE = float(os.getenv("CAMPAIGN_BOUNCE_BREAKER_RATE", "0.08"))
_COUNTED = ("sent", "delivered", "hard_bounced", "soft_bounced", "replied", "positive_reply", "negative_reply",
            "completed", "unsubscribed")


def _bounce_counts(session, campaign):
    rows = session.exec(select(CampaignRecipient.status).where(CampaignRecipient.campaign_id == campaign.id)).all()
    return sum(1 for st in rows if st == "hard_bounced"), sum(1 for st in rows if st in _COUNTED)


def bounce_stats(session, campaign) -> tuple:
    """(hard bounces, sent) since the campaign was last (re)started (bounce_baseline) — exactly what the bounce
    breaker judges; the warm-up ramp and the daily summary use the same numbers."""
    hard, sent = _bounce_counts(session, campaign)
    try:
        h0, s0 = (int(x) for x in (campaign.bounce_baseline or "0:0").split(":"))
    except ValueError:
        h0, s0 = 0, 0
    return max(0, hard - h0), max(0, sent - s0)


def bounce_breaker(session, campaign) -> bool:
    """Pause a running campaign whose hard-bounce rate is too high (a scraped list gone bad burns the sending
    domain). Judged on what was sent SINCE the campaign was last (re)started (bounce_baseline), so an admin who
    resumes after cleaning the list isn't re-paused by the old bounces. Returns True if it paused the campaign."""
    if campaign.status != "running":
        return False
    hard, sent = bounce_stats(session, campaign)
    if sent < BOUNCE_BREAKER_MIN_SENT or hard / sent < BOUNCE_BREAKER_RATE:
        return False
    _pause_with_item(session, campaign, f"bounce breaker: {hard} hard bounces in {sent} sent — check the list")
    return True


# --- Phase 12: automatic warm-up ramp ---------------------------------------------------------------------------
# A campaign with a warm-up plan ("10,20,35,50") moves its OWN daily limit one step up, once per UTC day before that
# day's first send, only after a full sending day with a low hard-bounce rate while reply/bounce reading works. It
# never lowers a limit, never goes above the mailbox's limit and never changes MailAccount.daily_limit.
WARMUP_MAX_BOUNCE_RATE = float(os.getenv("CAMPAIGN_WARMUP_MAX_BOUNCE_RATE", "0.05"))
WARMUP_IMAP_FRESH_SEC = int(os.getenv("CAMPAIGN_WARMUP_IMAP_FRESH_SEC", "7200"))
WARMUP_LOOKBACK_DAYS = 3            # how many recent SENDING days the ramp checks for a full one at the current limit


def parse_warmup_plan(text) -> list:
    """'10, 20,35;50' → [10, 20, 35, 50]: positive whole numbers (at most 500, the controls' cap), sorted, unique.
    Anything else is ignored."""
    return sorted({min(500, int(p)) for p in re.split(r"[\s,;]+", text or "")
                   if re.fullmatch(r"[0-9]{1,6}", p) and int(p) > 0})


def _sent_between(session, campaign, start, end) -> int:
    return session.exec(select(func.count()).where(
        CampaignSend.campaign_id == campaign.id, CampaignSend.status == "sent",
        CampaignSend.sent_at >= start, CampaignSend.sent_at < end)).one()


def last_inbox_success(session, complete_only=False):
    """When reply/bounce reading (the IMAP poll) last finished without failing, or None. complete_only: only a poll
    that read its whole window ('ok' — not 'partial', which may have stopped on a dropped connection). Newest row
    first by primary key (one poller, so id order is finish order) — no sort of the whole, never-pruned table."""
    return session.exec(select(IngestionRun.finished_at).where(
        IngestionRun.source == "email-inbound",
        IngestionRun.status.in_(("ok",) if complete_only else ("ok", "partial")),
        IngestionRun.finished_at.is_not(None)).order_by(IngestionRun.id.desc()).limit(1)).first()


def reply_reading_fresh(session, now=None) -> bool:
    """True while a COMPLETE read of the inbox finished within WARMUP_IMAP_FRESH_SEC — the condition for any
    follow-up (an interrupted read may have missed the very reply that should stop it)."""
    now = now or datetime.utcnow()
    ok_at = last_inbox_success(session, complete_only=True)
    return ok_at is not None and (now - ok_at).total_seconds() <= WARMUP_IMAP_FRESH_SEC


def warmup_decision(session, campaign, mailbox, day_start, now=None) -> dict:
    """Read-only: what the ramp does for the sending day starting at `day_start` (UTC midnight). Returns
    {action, from, to, why, code, alert}: 'none' (no plan / plan done / limit 0), 'wait' (no full day yet — normal),
    'hold' (a problem blocks the next step → alert) or 'advance' (to the next plan step, capped by the mailbox)."""
    now = now or datetime.utcnow()
    cur = max(0, campaign.daily_limit or 0)
    plan = parse_warmup_plan(campaign.warmup_plan)

    def out(action, why, code="", to=cur):
        return {"action": action, "from": cur, "to": to, "why": why, "code": code, "alert": action == "hold"}
    nxt = next((p for p in plan if p > cur), None)
    if not plan or nxt is None or cur == 0:     # a limit of 0 is a deliberate stop; above the plan is a manual choice
        return out("none", "no warm-up plan" if not plan else "daily limit is 0" if cur == 0 else "plan complete")
    cap = max(0, mailbox.daily_limit) if mailbox is not None else nxt
    target = min(nxt, cap)
    if target <= cur:
        return out("hold", f"the mailbox's own daily limit ({cap}) caps the ramp — raise it on /mail to go on to "
                           f"{nxt}/day", "mailbox_cap")
    last = session.exec(select(func.max(CampaignSend.sent_at)).where(
        CampaignSend.campaign_id == campaign.id, CampaignSend.status == "sent",
        CampaignSend.sent_at < day_start)).one()
    if last is None:
        return out("wait", "no sending day yet")
    eff = min(cur, cap)
    # a FULL day at the current limit among the last WARMUP_LOOKBACK_DAYS sending days — not just the last one: with
    # buyer-local hours a weekend UTC day can be short (only Gulf / Monday-morning Asia-Pacific buyers) without anything
    # being wrong
    full, sending_days = None, 0
    for back in range(1, 15):
        d0 = day_start - timedelta(days=back)
        n = _sent_between(session, campaign, d0, d0 + timedelta(days=1))
        if not n:
            continue
        sending_days += 1
        if n >= eff:
            full = (d0, n)
            break
        if sending_days >= WARMUP_LOOKBACK_DAYS:
            break
    if full is None:
        last_day = last.replace(hour=0, minute=0, second=0, microsecond=0)
        sent_last = _sent_between(session, campaign, last_day, last_day + timedelta(days=1))
        return out("wait", f"the last sending day ({last_day:%a %d %b}) sent {sent_last} of {eff}")
    last_day, sent_last = full
    hard, sent = bounce_stats(session, campaign)
    if sent < eff:
        return out("wait", f"only {sent} sent since the campaign was last (re)started")
    if hard / sent >= WARMUP_MAX_BOUNCE_RATE:
        return out("hold", f"{hard} hard bounce(s) in {sent} sent since the last (re)start "
                           f"({hard / sent:.0%}, warm-up limit {WARMUP_MAX_BOUNCE_RATE:.0%})", "bounce_rate")
    ok_at = last_inbox_success(session)
    if ok_at is None or (now - ok_at).total_seconds() > WARMUP_IMAP_FRESH_SEC:
        return out("hold", f"reply/bounce reading (IMAP) has not succeeded in the last "
                           f"{WARMUP_IMAP_FRESH_SEC // 60} min — bounces can't be counted", "imap_stale")
    return out("advance", f"{sent_last} sent on {last_day:%a %d %b}, {hard} hard bounce(s) in {sent} since the last "
                          f"(re)start, reply reading OK", to=target)


def apply_warmup(session, campaign, mailbox, now=None) -> dict:
    """The worker's once-per-UTC-day ramp step (called inside the sending window, before the day's first send): apply
    warmup_decision, record the day, audit an advance, and raise/refresh one 'campaign_warmup_held' task on a hold."""
    now = now or datetime.utcnow()
    today = now.strftime("%Y-%m-%d")
    if campaign.status != "running" or not (campaign.warmup_plan or "").strip() or campaign.warmup_checked_on == today:
        return {"action": "skip"}
    d = warmup_decision(session, campaign, mailbox, now.replace(hour=0, minute=0, second=0, microsecond=0), now)
    from . import work_queue as WQ
    key = f"campaign_warmup_held:{campaign.id}"
    campaign.warmup_checked_on = today
    if d["action"] == "advance" and d["to"] > (campaign.daily_limit or 0):      # never lowers
        campaign.daily_limit, campaign.updated_at = d["to"], now
        WQ.resolve_by_key(session, key, note=f"warm-up advanced to {d['to']}/day")
        pipeline.audit(session, None, "campaign", campaign.id, "warmup_advance",
                       {"from": d["from"], "to": d["to"], "why": d["why"][:200]}, tenant_id=campaign.tenant_id)
    session.add(campaign)
    session.commit()
    if d["alert"]:
        _warmup_held_item(session, campaign, d, key, now)
    return d


def _warmup_held_item(session, campaign, d, key, now):
    """One open task per campaign; a new reason/limit refreshes it, a dismissed one stays dismissed. Best-effort."""
    from . import work_queue as WQ
    version = f"{d['from']}:{d['code']}"
    title, desc = f"Warm-up held at {d['from']}/day: {campaign.name[:60]}", d["why"][:300]
    try:
        items = WQ.open_items_for_key(session, key)
        if items:
            wi = items[0]
            if wi.condition_version != version:
                wi.title, wi.description, wi.condition_version, wi.updated_at = title, desc, version, now
                session.add(wi)
        elif not WQ.already_handled(session, key, version):
            WQ.create_work_item_safe(session, type="campaign_warmup_held", priority="high", title=title,
                                     description=desc, tenant_id=campaign.tenant_id, idempotency_key=key,
                                     condition_version=version)
        session.commit()
    except Exception:  # noqa: BLE001 — the hold itself is what matters; the task is best-effort
        session.rollback()


def _pause_with_item(session, campaign, reason):
    """Pause the campaign and surface the same work item the paused-campaign sweep would (same key/version)."""
    transition(session, campaign, "paused", reason=reason)
    session.commit()
    try:
        from . import work_queue as WQ
        WQ.create_work_item_safe(
            session, type="campaign_paused", priority="high", title=f"Campaign paused: {campaign.name[:60]}",
            description=campaign.pause_reason[:300], tenant_id=campaign.tenant_id,
            idempotency_key=f"campaign_paused:{campaign.id}", condition_version=campaign.pause_reason[:60])
        session.commit()
    except Exception:  # noqa: BLE001 — the pause is what matters; the task is best-effort
        session.rollback()


def _send_row(session, campaign, rcpt, step_index):
    return session.exec(select(CampaignSend).where(
        CampaignSend.campaign_id == campaign.id, CampaignSend.recipient_id == rcpt.id,
        CampaignSend.sequence_version == rcpt.sequence_version,
        CampaignSend.step_index == step_index)).first()


def claim_send(session, campaign, rcpt, step_index, now=None, lease_sec=None):
    """Atomically CLAIM the (campaign, recipient, version, step) send with a time-limited lease. Returns the
    claimed CampaignSend or None if it's already done or another worker holds a live lease. Two workers racing
    the first claim: the unique index lets exactly one INSERT win (the other gets IntegrityError → re-reads).
    Re-claiming a stale/retryable row uses a compare-and-swap on claim_token, so only one worker takes it."""
    now = now or datetime.utcnow()
    lease_sec = lease_sec or LEASE_SECONDS
    token = secrets.token_hex(8)
    lease = now + timedelta(seconds=lease_sec)
    cs = _send_row(session, campaign, rcpt, step_index)
    if cs is None:
        try:
            with session.begin_nested():
                cs = CampaignSend(campaign_id=campaign.id, recipient_id=rcpt.id,
                                  sequence_version=rcpt.sequence_version, step_index=step_index,
                                  status="claimed", claim_token=token, claimed_at=now, lease_expires_at=lease)
                session.add(cs); session.flush()
            session.commit()
            return cs
        except IntegrityError:            # a concurrent worker inserted first — fall through to re-read
            session.rollback()
            cs = _send_row(session, campaign, rcpt, step_index)
            if cs is None:
                return None
    # existing row — decide claimability
    if cs.status in SEND_TERMINAL:
        return None
    if cs.status in ("claimed", "sending") and cs.lease_expires_at and cs.lease_expires_at > now:
        return None                        # another worker holds a valid lease
    if cs.status in ("pending", "retryable") and cs.next_attempt_at and cs.next_attempt_at > now:
        return None                        # backoff not elapsed yet
    old_token = cs.claim_token
    res = session.execute(_sa_update(CampaignSend).where(
        CampaignSend.id == cs.id, CampaignSend.claim_token == old_token).values(
        status="claimed", claim_token=token, claimed_at=now, lease_expires_at=lease, updated_at=now))
    session.commit()
    if res.rowcount == 1:                   # CAS won
        session.refresh(cs)
        return cs
    return None                             # another worker re-claimed between our read and write


def send_step(session, campaign, rcpt, mailbox, now=None, sender=None, inbox_fresh=None, local_ok=None) -> dict:
    """Send the recipient's CURRENT sequence step, crash-safely and at most once automatically.

    Lifecycle (durable in CampaignSend): claim → sending → (sent | retryable | permanently_failed). The row is
    CLAIMED with a time-limited lease before SMTP is touched; the Outreach event + 'sent' state are written
    ONLY after the provider accepts. A crash BEFORE acceptance leaves a reclaimable 'sending' row that recovery
    treats as ambiguous (never auto-resent — see recover_stale_sends), so there is no silent duplicate.

    Honest limitation: SMTP offers no true exactly-once. We guarantee no AUTOMATIC duplicate — the residual
    crash-during-provider-accept window is surfaced to an admin ('unknown_needs_review'), not blindly resent.

    Phase 11: the message is RENDERED (campaign_render — merge fields, footer, List-Unsubscribe, seller + internal-
    brand guards) BEFORE anything is claimed: a message that can't be rendered safely never takes a claim or a daily
    slot. A mailbox-level failure (auth/quota/config) refunds the slot, pauses the mailbox and never counts against
    the recipient. `sender` is injectable for tests.
    """
    now = now or datetime.utcnow()
    ok, reason = can_send(session, campaign, rcpt, mailbox, now, inbox_fresh, local_ok)
    if not ok:
        _settle_skip(session, rcpt, reason, now)
        return {"status": "skipped", "reason": reason}
    step_index = rcpt.current_step
    step = steps_for(session, campaign, rcpt.sequence_version)[step_index]
    ld = session.get(Lead, rcpt.lead_id) if rcpt.lead_id else None
    # Phase 12: a follow-up is ALWAYS a reply in the buyer's own thread — never a "Re:" to an email they never got
    thread = thread_anchor(session, campaign, rcpt) if step_index > 0 else {}
    if step_index > 0 and not thread:
        return _render_failed(session, campaign, rcpt, {"scope": "recipient",
                                                        "error": "follow-up has no sent email to reply to"}, now)
    msg = CR.render_campaign_message(session, campaign, step, ld, mailbox, thread_subject=thread.get("subject", ""))
    if not msg["ok"]:
        return _render_failed(session, campaign, rcpt, msg, now)
    cs = claim_send(session, campaign, rcpt, step_index, now)
    if cs is None:
        return {"status": "already_sent", "reason": "claimed/sent by another worker or terminal"}
    # Durable RFC Message-ID: generate + persist ONCE, BEFORE the 'sending' transition. Reused verbatim on
    # every safe retry of this logical send; never regenerated for the same (campaign,recipient,version,step).
    if not cs.rfc_message_id:
        cs.rfc_message_id = _make_message_id(mailbox)
    # committed 'sending' (with the Message-ID) BEFORE SMTP → a crash here is recoverable as an ambiguous
    # (never auto-resent) row, and the Message-ID survives a worker restart for reply correlation.
    cs.status = "sending"; cs.updated_at = now; session.add(cs); session.commit()
    if not SG.mailbox_take_slot(mailbox, now):
        cs.status = "retryable"; cs.last_error = "daily limit reached"
        cs.next_attempt_at = now + timedelta(days=1); cs.updated_at = now
        session.add(cs); session.add(mailbox); session.commit()
        return {"status": "limited", "reason": "daily limit reached"}
    subject, body = msg["subject"], msg["text"]
    send = sender or _default_sender
    # our durable RFC Message-ID becomes the actual Message-ID header of the sent mail (reply-correlation key); a
    # follow-up carries In-Reply-To/References to the emails this buyer already got, so it lands in the same thread
    try:
        okk, err, provider_id = send(mailbox, rcpt.to_email, subject, body, html=msg["html"] or None,
                                     reply_to=mailbox.email, message_id=cs.rfc_message_id, headers=msg["headers"],
                                     attachments=msg.get("attachments") or None,
                                     in_reply_to=thread.get("in_reply_to", ""), references=thread.get("references", ""))
    except Exception as e:  # noqa: BLE001 — a sender that raises must never leave this row stuck in 'sending'
        okk, err, provider_id = False, f"sender error: {e}"[:300], ""
    kind = "" if okk else classify_mailbox_error(err)
    if kind:
        return _mailbox_failed(session, cs, mailbox, kind, err, now)
    cs.attempt_count += 1
    cs.updated_at = now
    prov = provider_id if (provider_id and provider_id != cs.rfc_message_id) else ""   # store provider id separately
    if okk:
        # provider ACCEPTED → NOW write the Outreach event (the unique index is the last-line dup guard) + mark sent
        row = Outreach(lead_id=rcpt.lead_id or 0, direction="out", channel="email",
                       recipient=rcpt.to_email[:200], from_addr=mailbox.email[:200],
                       subject=subject[:200], body=body[:4000], status="sent",
                       campaign_id=campaign.id, campaign_recipient_id=rcpt.id,
                       campaign_version=rcpt.sequence_version, campaign_step=step_index,
                       message_id=cs.rfc_message_id or prov, user_id=campaign.owner_id)  # our id = the sent header
        try:
            with session.begin_nested():
                session.add(row); session.flush()
        except IntegrityError:
            # an Outreach for this exact step already exists → a prior attempt actually delivered. Mark sent,
            # do NOT create a second event and do NOT re-advance the recipient beyond this step.
            session.rollback()
            cs.status = "sent"; cs.sent_at = now; cs.provider_message_id = prov
            session.add(cs); session.commit()
            return {"status": "sent", "reason": "already recorded", "provider_message_id": prov}
        cs.status = "sent"; cs.sent_at = now; cs.provider_message_id = prov; cs.outreach_id = row.id
        rcpt.last_sent_at = now; rcpt.status = "sent"; rcpt.current_step = step_index + 1
        rcpt.next_action_at = now + timedelta(days=max(0, _next_delay(session, campaign, rcpt)))
        if rcpt.current_step >= len(steps_for(session, campaign, rcpt.sequence_version)):
            rcpt.status = "completed"; rcpt.next_action_at = None
        rcpt.updated_at = now; session.add(rcpt)
        if ld is not None and ld.first_response_at is None:
            ld.first_response_at = now; session.add(ld)
        if (mailbox.last_send_error or "").startswith("transient"):
            mailbox.last_send_error = ""          # the network hiccup is over
        session.add(cs); session.add(mailbox); session.commit()
        # managed buyer's pipeline follows the real send — in its own transaction, so a sync failure can never
        # undo the recorded send
        try:
            if pipeline.advance_stage(session, ld, "contacted", note=f"campaign {campaign.id} email {step_index + 1}"):
                session.commit()
        except Exception:  # noqa: BLE001
            session.rollback()
        return {"status": "sent", "reason": "", "rfc_message_id": cs.rfc_message_id,
                "provider_message_id": cs.provider_message_id}
    # provider REJECTED → retry policy
    cs.last_error = (err or "")[:400]
    if classify_send_error(err) == "permanent" or cs.attempt_count >= RETRY_MAX:
        cs.status = "permanently_failed"
        rcpt.status = "skipped"; rcpt.next_action_at = None; rcpt.updated_at = now   # terminal: campaign can finish
        session.add(rcpt)
    else:
        cs.status = "retryable"
        cs.next_attempt_at = now + timedelta(seconds=RETRY_BACKOFF_SEC * cs.attempt_count)
    session.add(cs); session.add(mailbox); session.commit()
    return {"status": cs.status, "reason": err}


def _render_failed(session, campaign, rcpt, msg, now):
    """A template-level problem pauses the whole campaign once (fix it, then resume); a recipient-level one skips
    only that buyer. Nothing is claimed and no daily slot is used either way."""
    if msg["scope"] == "template":
        _pause_with_item(session, campaign, f"render: {msg['error']}"[:300])
        return {"status": "render_failed", "scope": "template", "reason": msg["error"]}
    rcpt.status = "skipped"; rcpt.next_action_at = None; rcpt.updated_at = now
    session.add(rcpt); session.commit()
    try:
        from . import work_queue as WQ
        WQ.create_work_item_safe(
            session, type="failed_system_job", priority="normal",
            title=f"Buyers skipped in campaign: {campaign.name[:50]}",
            description=f"At least one buyer could not be emailed safely and was skipped: {msg['error']}"[:300],
            tenant_id=campaign.tenant_id, idempotency_key=f"campaign_render_skip:{campaign.id}")
        session.commit()
    except Exception:  # noqa: BLE001
        session.rollback()
    return {"status": "render_failed", "scope": "recipient", "reason": msg["error"]}


def _mailbox_failed(session, cs, mailbox, kind, err, now):
    """The MAILBOX failed (auth / quota / config): every later send would fail the same way. Refund the slot, put
    the send back in the queue untouched (no attempt counted — not the buyer's fault), pause the mailbox and raise
    an urgent task. Resuming the mailbox re-sends with the same Message-ID."""
    SG.mailbox_refund_slot(mailbox, now)
    cs.status = "retryable"; cs.claim_token = ""
    cs.last_error = (err or "")[:400]; cs.updated_at = now
    mailbox.last_send_error = f"{kind}: {err}"[:200]
    if kind == "transient":            # network hiccup: retry this send after the backoff; the mailbox keeps going
        cs.next_attempt_at = now + timedelta(seconds=RETRY_BACKOFF_SEC)
        session.add(cs); session.add(mailbox); session.commit()
        return {"status": "mailbox_failed", "kind": kind, "reason": err}
    cs.next_attempt_at = now
    mailbox.paused = True
    session.add(cs); session.add(mailbox); session.commit()
    try:
        from . import work_queue as WQ
        if kind == "auth":        # same key/version as work_queue.sync_auth_failed_mailboxes
            WQ.create_work_item_safe(
                session, type="mailbox_auth_failure", priority="urgent",
                title=f"Mailbox authentication failure: {mailbox.email}",
                description="A Go4it mailbox failed authentication and was paused — update credentials.",
                idempotency_key=f"mailbox_auth_failure:{mailbox.id}",
                condition_version=mailbox.last_send_error.lower()[:60])
        else:
            WQ.create_work_item_safe(
                session, type="failed_system_job", priority="urgent",
                title=f"Mailbox paused ({kind}): {mailbox.email}",
                description=f"Sending stopped: {err}"[:300], idempotency_key=f"mailbox_paused:{mailbox.id}")
        session.commit()
    except Exception:  # noqa: BLE001
        session.rollback()
    return {"status": "mailbox_failed", "kind": kind, "reason": err}


def recover_stale_sends(session, now=None) -> dict:
    """Reclaim abandoned sends whose lease expired (worker crash / lost process). A 'claimed' row never reached
    SMTP → safe to retry. A 'sending' row was mid-provider-handoff at the crash and MIGHT have been delivered →
    it is flagged 'unknown_needs_review' (an admin work item), never auto-resent, so no silent duplicate."""
    now = now or datetime.utcnow()
    out = {"reclaimed": 0, "needs_review": 0}
    stale = session.exec(select(CampaignSend).where(
        CampaignSend.status.in_(("claimed", "sending")),
        CampaignSend.lease_expires_at.is_not(None),
        CampaignSend.lease_expires_at < now)).all()
    for cs in stale:
        if cs.status == "claimed":
            cs.status = "retryable"; cs.next_attempt_at = now; cs.claim_token = ""
            out["reclaimed"] += 1
        else:
            cs.status = "unknown_needs_review"; cs.claim_token = ""
            out["needs_review"] += 1
            _needs_review_workitem(session, cs)
        cs.updated_at = now; session.add(cs)
    session.commit()
    return out


def _needs_review_workitem(session, cs):
    """Non-blocking: surface an ambiguous crash-during-send for an admin to reconcile. Never raises."""
    try:
        from . import work_queue as WQ
        WQ.create_work_item_safe(
            session, tenant_id=None, type="failed_system_job", source="automatic",
            title="Send needs review (crash during delivery)",
            description=(f"CampaignSend #{cs.id} (campaign {cs.campaign_id}, recipient {cs.recipient_id}, "
                         f"step {cs.step_index}) crashed mid-delivery — may or may not have been delivered. "
                         "Verify with the mailbox before any resend."),
            idempotency_key=f"send_review:cs:{cs.id}")
    except Exception:
        pass


def _next_delay(session, campaign, rcpt):
    steps = steps_for(session, campaign, rcpt.sequence_version)
    return steps[rcpt.current_step].delay_days if rcpt.current_step < len(steps) else 0


def _make_message_id(mailbox):
    """A globally-unique, RFC-5322-compliant Message-ID (<uniq@domain>) using the mailbox's own domain."""
    from email.utils import make_msgid
    email = getattr(mailbox, "email", "") or ""
    domain = email.split("@")[-1] if "@" in email else "go4it.vip"
    return make_msgid(domain=domain)


def _default_sender(mailbox, to_addr, subject, text, html=None, reply_to="", message_id="",
                    in_reply_to="", references="", headers=None, attachments=None):
    from .outreach import send_via_account
    return send_via_account(mailbox, to_addr, subject, text, html=html, reply_to=reply_to,
                            message_id=message_id, in_reply_to=in_reply_to, references=references,
                            headers=headers, attachments=attachments)


# --------------------------------------------------------------------- lifecycle + reply/bounce stop
def stop_recipient(session, lead_id, outcome, actor=None):
    """A reply/bounce/unsubscribe stops the recipient's remaining steps across campaigns (idempotent).
    outcome ∈ replied|positive_reply|negative_reply|hard_bounced|unsubscribed|soft_bounced|follow_up_later."""
    n = 0
    for rcpt in session.exec(select(CampaignRecipient).where(CampaignRecipient.lead_id == lead_id)).all():
        if outcome == "soft_bounced":
            rcpt.soft_bounce_count += 1
            rcpt.status = "soft_bounced"
        else:
            rcpt.status = outcome if outcome in RECIPIENT_STATUSES else rcpt.status
            rcpt.reply_outcome = outcome
            if outcome in TERMINAL_RECIPIENT:
                rcpt.next_action_at = None
        rcpt.updated_at = datetime.utcnow()
        session.add(rcpt)
        n += 1
    return n


def transition(session, campaign, to, actor=None, reason=""):
    """Move a campaign through its lifecycle. Records timestamps; never hard-deletes."""
    if to not in CAMPAIGN_STATUSES:
        return False, "unknown status"
    now = datetime.utcnow()
    if to == "running" and campaign.status != "running":      # (re)start: the bounce breaker judges from here on
        hard, sent = _bounce_counts(session, campaign)
        campaign.bounce_baseline = f"{hard}:{sent}"
    campaign.status = to
    if to == "scheduled":
        campaign.scheduled_at = now
    elif to == "running" and not campaign.started_at:
        campaign.started_at = now
    elif to == "paused":
        campaign.paused_at = now
        campaign.pause_reason = (reason or "")[:300]
    elif to == "completed":
        campaign.completed_at = now
    campaign.updated_at = now
    session.add(campaign)
    try:
        pipeline.audit(session, actor, "campaign", campaign.id, f"status_{to}",
                       {"reason": reason[:120]}, tenant_id=campaign.tenant_id)
    except Exception:  # noqa: BLE001
        pass
    return True, ""
