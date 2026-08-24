"""Campaign engine (Phase 4) — audience building, idempotent enrolment, sequence versioning and the
send-safety chain. A Campaign is NEVER an independent contact database: recipients come only from the Trade
Network (managed buyer Leads + their canonical Company/Contact). Two-way confidentiality and suppression are
enforced on every send here (buyers never learn the seller; suppressed/replied addresses are never sent).
"""
import os
import secrets
from datetime import datetime, timedelta

from sqlalchemy import update as _sa_update
from sqlalchemy.exc import IntegrityError
from sqlmodel import func, select

from . import pipeline
from . import send_guard as SG
from . import suppression as SUP
from .models import Campaign, CampaignRecipient, CampaignSend, CampaignStep, Lead, Outreach

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
    rows = session.exec(stmt).all()
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


def audience_preview(session, campaign, f: dict) -> dict:
    """The pre-launch breakdown an admin confirms before recipient records are created."""
    leads = audience_leads(session, campaign.tenant_id, f)
    existing = {r.lead_id for r in session.exec(select(CampaignRecipient)
                .where(CampaignRecipient.campaign_id == campaign.id)).all()}
    seen_email, dupes, valid, missing, suppressed, prev_contacted, already = set(), 0, 0, 0, 0, 0, 0
    eligible = []
    for ld in leads:
        em = SUP.normalize_email(ld.email)
        if not em:
            missing += 1
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
            "suppressed": suppressed, "previously_contacted": prev_contacted,
            "already_in_campaign": already, "duplicates": dupes, "final_eligible": len(eligible),
            "eligible_lead_ids": [ld.id for ld in eligible]}


def enroll(session, campaign, actor, f: dict) -> dict:
    """Create durable recipient records for the eligible audience — idempotent (unique campaign_id+contact/
    lead), never enrolling a suppressed or duplicate address. Requires the admin to have confirmed the
    preview. Returns {created, skipped_suppressed, skipped_existing}."""
    prev = audience_preview(session, campaign, f)
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
    for i, s in enumerate(steps):
        session.add(CampaignStep(campaign_id=campaign.id, version=version, step_index=i,
                                 subject=SG.sanitize_header((s.get("subject") or "")[:200]),
                                 body=(s.get("body") or "")[:8000], template_id=s.get("template_id"),
                                 delay_days=int(s.get("delay_days") or 0),
                                 manual_review=bool(s.get("manual_review"))))
    session.commit()
    try:
        pipeline.audit(session, actor, "campaign", campaign.id, "set_sequence",
                       {"version": version, "running_edit": running}, tenant_id=campaign.tenant_id)
    except Exception:  # noqa: BLE001
        pass
    return version


# --------------------------------------------------------------------- send safety + idempotent send
def can_send(session, campaign, rcpt, mailbox, now=None) -> tuple:
    """The full pre-send safety chain. Returns (ok, reason). Verified for EVERY send."""
    now = now or datetime.utcnow()
    if SG.outreach_paused(session):
        return False, "outreach paused"
    if campaign.status != "running":
        return False, f"campaign not running ({campaign.status})"
    ok, why = SG.mailbox_ok(mailbox)
    if not ok:
        return False, why
    if rcpt.suppressed or rcpt.status in TERMINAL_RECIPIENT:
        return False, f"recipient {rcpt.status}"
    if SUP.is_suppressed(session, rcpt.to_email, tenant_id=campaign.tenant_id):
        return False, "suppressed"
    ld = session.get(Lead, rcpt.lead_id) if rcpt.lead_id else None
    if ld is not None and ld.buyer_replied_at is not None:
        return False, "already replied"
    if ld is not None and not (ld.email or "").strip():
        return False, "no active contact email"
    steps = steps_for(session, campaign, rcpt.sequence_version)
    if rcpt.current_step >= len(steps):
        return False, "sequence complete"
    step = steps[rcpt.current_step]
    if step.manual_review:
        return False, "manual-review step"
    if not SG.within_window(campaign, now):
        return False, "outside sending window"
    # daily-limit precheck (the slot is actually consumed in send_step)
    today = now.strftime("%Y-%m-%d")
    used = mailbox.sent_today if mailbox.sent_today_date == today else 0
    if used >= max(0, min(mailbox.daily_limit, campaign.daily_limit)):
        return False, "daily limit reached"
    return True, ""


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


def send_step(session, campaign, rcpt, mailbox, now=None, sender=None) -> dict:
    """Send the recipient's CURRENT sequence step, crash-safely and at most once automatically.

    Lifecycle (durable in CampaignSend): claim → sending → (sent | retryable | permanently_failed). The row is
    CLAIMED with a time-limited lease before SMTP is touched; the Outreach event + 'sent' state are written
    ONLY after the provider accepts. A crash BEFORE acceptance leaves a reclaimable 'sending' row that recovery
    treats as ambiguous (never auto-resent — see recover_stale_sends), so there is no silent duplicate.

    Honest limitation: SMTP offers no true exactly-once. We guarantee no AUTOMATIC duplicate — the residual
    crash-during-provider-accept window is surfaced to an admin ('unknown_needs_review'), not blindly resent.

    Confidentiality: seller identity stripped + headers sanitized before send. `sender` is injectable for tests.
    """
    now = now or datetime.utcnow()
    ok, reason = can_send(session, campaign, rcpt, mailbox, now)
    if not ok:
        return {"status": "skipped", "reason": reason}
    step_index = rcpt.current_step
    cs = claim_send(session, campaign, rcpt, step_index, now)
    if cs is None:
        return {"status": "already_sent", "reason": "claimed/sent by another worker or terminal"}
    # committed 'sending' BEFORE SMTP → a crash here is recoverable as an ambiguous (never auto-resent) row
    cs.status = "sending"; cs.updated_at = now; session.add(cs); session.commit()
    if not SG.mailbox_take_slot(mailbox, now):
        cs.status = "retryable"; cs.last_error = "daily limit reached"
        cs.next_attempt_at = now + timedelta(days=1); cs.updated_at = now
        session.add(cs); session.add(mailbox); session.commit()
        return {"status": "limited", "reason": "daily limit reached"}
    step = steps_for(session, campaign, rcpt.sequence_version)[step_index]
    ld = session.get(Lead, rcpt.lead_id) if rcpt.lead_id else None
    subject = SG.sanitize_header(step.subject or f"Re: {getattr(ld, 'product', '')}")
    body = SG.guard_buyer_text(session, step.body or "", campaign.tenant_id)   # buyers never learn the seller
    from_text, from_html = _parts(body)
    send = sender or _default_sender
    okk, err, mid = send(mailbox, rcpt.to_email, subject, from_text, html=from_html, reply_to=mailbox.email)
    cs.attempt_count += 1
    cs.updated_at = now
    if okk:
        # provider ACCEPTED → NOW write the Outreach event (the unique index is the last-line dup guard) + mark sent
        row = Outreach(lead_id=rcpt.lead_id or 0, direction="out", channel="email",
                       recipient=rcpt.to_email[:200], from_addr=mailbox.email[:200],
                       subject=subject[:200], body=body[:4000], status="sent",
                       campaign_id=campaign.id, campaign_recipient_id=rcpt.id,
                       campaign_version=rcpt.sequence_version, campaign_step=step_index,
                       message_id=mid or "", user_id=campaign.owner_id)
        try:
            with session.begin_nested():
                session.add(row); session.flush()
        except IntegrityError:
            # an Outreach for this exact step already exists → a prior attempt actually delivered. Mark sent,
            # do NOT create a second event and do NOT re-advance the recipient beyond this step.
            session.rollback()
            cs.status = "sent"; cs.sent_at = now; cs.provider_message_id = mid or ""
            session.add(cs); session.commit()
            return {"status": "sent", "reason": "already recorded", "provider_message_id": mid or ""}
        cs.status = "sent"; cs.sent_at = now; cs.provider_message_id = mid or ""; cs.outreach_id = row.id
        rcpt.last_sent_at = now; rcpt.status = "sent"; rcpt.current_step = step_index + 1
        rcpt.next_action_at = now + timedelta(days=max(0, _next_delay(session, campaign, rcpt)))
        if rcpt.current_step >= len(steps_for(session, campaign, rcpt.sequence_version)):
            rcpt.status = "completed"; rcpt.next_action_at = None
        rcpt.updated_at = now; session.add(rcpt)
        if ld is not None and ld.first_response_at is None:
            ld.first_response_at = now; session.add(ld)
        session.add(cs); session.add(mailbox); session.commit()
        return {"status": "sent", "reason": "", "provider_message_id": cs.provider_message_id}
    # provider REJECTED → retry policy
    cs.last_error = (err or "")[:400]
    if classify_send_error(err) == "permanent" or cs.attempt_count >= RETRY_MAX:
        cs.status = "permanently_failed"
    else:
        cs.status = "retryable"
        cs.next_attempt_at = now + timedelta(seconds=RETRY_BACKOFF_SEC * cs.attempt_count)
    session.add(cs); session.add(mailbox); session.commit()
    return {"status": cs.status, "reason": err}


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


def _parts(body):
    from .outreach import plain_parts
    return plain_parts(body)


def _default_sender(mailbox, to_addr, subject, text, html=None, reply_to=""):
    from .outreach import send_via_account
    return send_via_account(mailbox, to_addr, subject, text, html=html, reply_to=reply_to)


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
