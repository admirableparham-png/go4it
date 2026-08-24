"""Requests + Work Queue (Phase 3) — the WorkItem service: creation (manual + non-blocking automatic),
transitions, tenant validation, queue queries/counts, and the idempotent synchronization scanners.

Admin-only. A WorkItem is an INTERNAL admin task; a requester-visible action is published ONLY through the
existing sanitized SellerUpdate path and linked here (never merged). Automatic creation is non-blocking: it
runs after the operational event has committed and can NEVER make that event fail (mirrors
company_service.link_lead_company_safe). Re-running synchronization never duplicates an OPEN task
(idempotency_key + the partial-unique OPEN index in db._ensure_workitem_indexes).
"""
import logging
from datetime import datetime, timedelta

from sqlalchemy.exc import IntegrityError
from sqlmodel import select, func

from .models import (Campaign, CampaignRecipient, CommandJob, DuplicateCandidate, IngestionRun, Lead,
                     MailAccount, Outreach, Quote, ServiceRequest, SellerUpdate, WorkItem)
from .pipeline import audit

logger = logging.getLogger("go4it.work_queue")

# --- vocabularies ---------------------------------------------------------------------------------
TYPES = ["review_new_request", "follow_up_buyer", "follow_up_seller", "follow_up_supplier", "review_reply",
         "requester_action_required", "admin_action_required", "data_enrichment", "replace_invalid_contact",
         "review_potential_duplicate", "prepare_quote", "approve_quote", "missing_document", "deliver_result",
         "failed_system_job", "overdue_request",
         # Phase 4 (Outreach) work-item types
         "review_inbound_reply", "unmatched_inbound", "mailbox_auth_failure", "campaign_paused",
         "spam_complaint", "high_bounce_rate", "other"]
TYPE_LABELS = {
    "review_new_request": "Review new request", "follow_up_buyer": "Follow up with buyer",
    "follow_up_seller": "Follow up with seller", "follow_up_supplier": "Follow up with supplier",
    "review_reply": "Review reply", "requester_action_required": "Requester action required",
    "admin_action_required": "Admin action required", "data_enrichment": "Data enrichment",
    "replace_invalid_contact": "Replace invalid contact", "review_potential_duplicate": "Review potential duplicate",
    "prepare_quote": "Prepare quote", "approve_quote": "Approve quote", "missing_document": "Missing document",
    "deliver_result": "Deliver result", "failed_system_job": "Failed system job",
    "overdue_request": "Overdue request",
    "review_inbound_reply": "Review inbound reply", "unmatched_inbound": "Unmatched inbound message",
    "mailbox_auth_failure": "Mailbox authentication failure", "campaign_paused": "Campaign paused by failure",
    "spam_complaint": "Spam complaint", "high_bounce_rate": "High bounce-rate alert", "other": "Other",
}
STATUSES = ["open", "in_progress", "waiting", "completed", "dismissed"]
NONTERMINAL = ("open", "in_progress", "waiting")
PRIORITIES = ["low", "normal", "high", "urgent"]
WAITING_PARTIES = ["buyer", "seller", "supplier", "requester", "internal", "service_provider", "system"]
# related-party category (for follow-up organization) derived from type/waiting_on
PARTY_OF_TYPE = {"follow_up_buyer": "buyer", "follow_up_seller": "seller", "follow_up_supplier": "supplier",
                 "requester_action_required": "requester", "review_reply": "requester",
                 "review_inbound_reply": "buyer", "replace_invalid_contact": "buyer",
                 "failed_system_job": "system", "unmatched_inbound": "system",
                 "mailbox_auth_failure": "system", "campaign_paused": "system",
                 "spam_complaint": "system", "high_bounce_rate": "system"}
PRIORITY_BADGE = {"low": "slate", "normal": "sky", "high": "amber", "urgent": "rose"}
STATUS_BADGE = {"open": "queued", "in_progress": "running", "waiting": "amber",
                "completed": "won", "dismissed": "slate"}


def party_category(wi: WorkItem) -> str:
    """The related-party category a follow-up/task concerns (buyer|seller|supplier|requester|internal|...)."""
    if wi.waiting_on:
        return wi.waiting_on
    return PARTY_OF_TYPE.get(wi.type, "internal")


# --- creation -------------------------------------------------------------------------------------
def create_work_item(session, *, type, title, description="", tenant_id=None, priority="normal", status=None,
                     assigned_admin_id=None, created_by=None, source="manual", visibility="internal",
                     waiting_on="", related_request_id=None, related_company_id=None, related_lead_id=None,
                     related_outreach_id=None, related_quote_id=None, related_deal_id=None,
                     related_seller_update_id=None, parent_id=None, idempotency_key="", condition_version="",
                     inferred=False, due_at=None) -> WorkItem:
    """Create a WorkItem (does not commit). status defaults to 'waiting' when waiting_on is set, else 'open'.
    Raises IntegrityError if an OPEN item with the same idempotency_key already exists (partial-unique index)."""
    if status is None:
        status = "waiting" if waiting_on else "open"
    wi = WorkItem(type=type, title=(title or TYPE_LABELS.get(type, "Task"))[:200], description=description[:4000],
                  tenant_id=tenant_id, priority=priority if priority in PRIORITIES else "normal", status=status,
                  assigned_admin_id=assigned_admin_id, created_by=created_by, source=source, visibility=visibility,
                  waiting_on=waiting_on if waiting_on in WAITING_PARTIES else "",
                  related_request_id=related_request_id, related_company_id=related_company_id,
                  related_lead_id=related_lead_id, related_outreach_id=related_outreach_id,
                  related_quote_id=related_quote_id, related_deal_id=related_deal_id,
                  related_seller_update_id=related_seller_update_id, parent_id=parent_id,
                  idempotency_key=idempotency_key, condition_version=condition_version, inferred=inferred,
                  due_at=due_at)
    session.add(wi)
    session.flush()  # surface the partial-unique IntegrityError to the caller now
    return wi


def already_handled(session, key, version) -> bool:
    """True if a work item for this EXACT condition instance+version already exists — in ANY status,
    including completed/dismissed. This is the durable-disposition guard: once an admin has completed or
    dismissed the task for a condition, sync must NOT recreate it while the condition version is unchanged.
    A materially changed condition produces a new `version`, which is not 'handled' → a fresh task is made."""
    if not key:
        return False
    return session.exec(select(WorkItem.id).where(WorkItem.idempotency_key == key,
                                                  WorkItem.condition_version == version)).first() is not None


def create_work_item_safe(session, *, actor=None, **spec):
    """Non-blocking automatic creation used by operational hooks: the primary row is ALREADY committed, so a
    failure here must never propagate. A duplicate OPEN idempotency_key (IntegrityError) is a silent no-op;
    any other error is swallowed and logged to the dq_ audit queue. Returns the WorkItem or None."""
    spec.setdefault("source", "automatic")
    key = spec.get("idempotency_key", "")
    try:
        with session.begin_nested():
            wi = create_work_item(session, **spec)
        return wi
    except IntegrityError:
        return None  # an OPEN item with this key already exists — exactly the idempotent outcome we want
    except Exception:  # noqa: BLE001 — belt-and-suspenders; automatic creation is best-effort only
        logger.warning("work item creation failed (key=%s)", key, exc_info=True)
        try:
            audit(session, actor, "work_item", None, "workitem_link_failed",
                  {"key": key, "type": spec.get("type")})
        except Exception:  # noqa: BLE001
            pass
        return None


def open_items_for_key(session, key):
    """All non-terminal work items carrying this idempotency_key."""
    if not key:
        return []
    return session.exec(select(WorkItem).where(WorkItem.idempotency_key == key,
                                               WorkItem.status.in_(NONTERMINAL))).all()


def resolve_by_key(session, key, actor=None, note="") -> int:
    """Complete every OPEN work item with this idempotency_key (the triggering condition is resolved).
    Returns how many were closed. Never raises fatally."""
    n = 0
    for wi in open_items_for_key(session, key):
        wi.status = "completed"
        wi.completed_at = datetime.utcnow()
        wi.resolved_by = getattr(actor, "id", None)
        wi.resolution_note = (note or "auto-resolved")[:500]
        wi.updated_at = datetime.utcnow()
        session.add(wi)
        audit(session, actor, "work_item", wi.id, "work_item_completed", {"auto": True}, tenant_id=wi.tenant_id)
        n += 1
    return n


# --- transitions (used by Work Queue routes; each audits) -----------------------------------------
def _touch(wi):
    wi.updated_at = datetime.utcnow()


def assign_item(session, wi, admin_id, actor=None):
    wi.assigned_admin_id = admin_id
    _touch(wi); session.add(wi)
    audit(session, actor, "work_item", wi.id, "work_item_assigned", {"to": admin_id}, tenant_id=wi.tenant_id)


def start_item(session, wi, actor=None):
    wi.status = "in_progress"
    if not wi.started_at:
        wi.started_at = datetime.utcnow()
    _touch(wi); session.add(wi)
    audit(session, actor, "work_item", wi.id, "work_item_status", {"to": "in_progress"}, tenant_id=wi.tenant_id)


def complete_item(session, wi, actor=None, note=""):
    wi.status = "completed"
    wi.completed_at = datetime.utcnow()
    wi.resolved_by = getattr(actor, "id", None)
    if note:
        wi.resolution_note = note[:500]
    _touch(wi); session.add(wi)
    audit(session, actor, "work_item", wi.id, "work_item_completed", {}, tenant_id=wi.tenant_id)


def mark_waiting(session, wi, party, actor=None):
    wi.status = "waiting"
    wi.waiting_on = party if party in WAITING_PARTIES else "internal"
    _touch(wi); session.add(wi)
    audit(session, actor, "work_item", wi.id, "work_item_status", {"to": "waiting", "on": wi.waiting_on},
          tenant_id=wi.tenant_id)


def set_priority(session, wi, priority, actor=None):
    wi.priority = priority if priority in PRIORITIES else wi.priority
    _touch(wi); session.add(wi)
    audit(session, actor, "work_item", wi.id, "work_item_priority", {"to": wi.priority}, tenant_id=wi.tenant_id)


def set_due(session, wi, due_at, actor=None):
    wi.due_at = due_at
    _touch(wi); session.add(wi)
    audit(session, actor, "work_item", wi.id, "work_item_due", {"to": due_at.isoformat() if due_at else None},
          tenant_id=wi.tenant_id)


def dismiss_item(session, wi, reason, actor=None):
    """Dismiss requires a reason (enforced by the caller). Retained for history — never deleted."""
    wi.status = "dismissed"
    wi.dismissed_reason = (reason or "")[:500]
    wi.resolved_by = getattr(actor, "id", None)
    wi.completed_at = datetime.utcnow()
    _touch(wi); session.add(wi)
    audit(session, actor, "work_item", wi.id, "work_item_dismissed", {"reason": wi.dismissed_reason[:120]},
          tenant_id=wi.tenant_id)


# --- tenant validation (cross-tenant linking prevention) ------------------------------------------
def _lead_tenant(lead):
    return lead.seller_id if lead.managed else lead.owner_id


def related_tenant(session, *, related_request_id=None, related_lead_id=None, related_quote_id=None,
                   related_company_id=None):
    """Derive the tenant a related record belongs to (so a manual work item is stamped with the RECORD's
    tenant, never a user-supplied one — cross-tenant linking is structurally impossible). Returns
    (tenant_id, error). error is set only when a referenced record does not exist."""
    if related_request_id is not None:
        sr = session.get(ServiceRequest, related_request_id)
        if not sr:
            return None, "request not found"
        return sr.owner_id, ""
    if related_lead_id is not None:
        ld = session.get(Lead, related_lead_id)
        if not ld:
            return None, "lead not found"
        return _lead_tenant(ld), ""
    if related_quote_id is not None:
        q = session.get(Quote, related_quote_id)
        if not q:
            return None, "quote not found"
        return q.owner_id, ""
    if related_company_id is not None:
        from .models import Company
        co = session.get(Company, related_company_id)
        if not co:
            return None, "company not found"
        return co.tenant_id, ""
    return None, ""


# --- counts + queue queries -----------------------------------------------------------------------
def _today_bounds():
    now = datetime.utcnow()
    start = datetime(now.year, now.month, now.day)
    return start, start + timedelta(days=1), now


def queue_counts(session, user=None) -> dict:
    """Summary counts for the Work Queue header + nav badge."""
    start, end, now = _today_bounds()
    def c(stmt):
        return session.exec(select(func.count()).select_from(stmt.subquery())).one()
    base = select(WorkItem.id).where(WorkItem.status.in_(NONTERMINAL))
    counts = {
        "open": c(select(WorkItem.id).where(WorkItem.status.in_(("open", "in_progress")))),
        "waiting": c(select(WorkItem.id).where(WorkItem.status == "waiting")),
        "due_today": c(base.where(WorkItem.due_at >= start, WorkItem.due_at < end)),
        "overdue": c(base.where(WorkItem.due_at.is_not(None), WorkItem.due_at < now)),
        "unassigned": c(base.where(WorkItem.assigned_admin_id.is_(None))),
        "failed": c(base.where(WorkItem.type == "failed_system_job")),
        "actionable": c(base),
    }
    if user is not None:
        counts["mine"] = c(base.where(WorkItem.assigned_admin_id == getattr(user, "id", None)))
    return counts


def nav_open_count(session) -> int:
    """The number shown on the Work Queue nav badge = actionable (open + in_progress) items."""
    return session.exec(select(func.count()).select_from(
        select(WorkItem.id).where(WorkItem.status.in_(("open", "in_progress"))).subquery())).one()


VIEWS = ["mine", "all_open", "due_today", "upcoming", "overdue", "waiting", "unassigned", "failures", "completed"]
VIEW_LABELS = {"mine": "My work", "all_open": "All open", "due_today": "Due today", "upcoming": "Upcoming",
               "overdue": "Overdue", "waiting": "Waiting", "unassigned": "Unassigned",
               "failures": "System failures", "completed": "Completed"}


def apply_view(stmt, view, user):
    """Narrow a select(WorkItem) by saved view."""
    start, end, now = _today_bounds()
    if view == "mine":
        return stmt.where(WorkItem.assigned_admin_id == getattr(user, "id", None),
                          WorkItem.status.in_(NONTERMINAL))
    if view == "all_open":
        return stmt.where(WorkItem.status.in_(("open", "in_progress")))
    if view == "due_today":
        return stmt.where(WorkItem.status.in_(NONTERMINAL), WorkItem.due_at >= start, WorkItem.due_at < end)
    if view == "upcoming":
        return stmt.where(WorkItem.status.in_(NONTERMINAL), WorkItem.due_at >= end)
    if view == "overdue":
        return stmt.where(WorkItem.status.in_(NONTERMINAL), WorkItem.due_at.is_not(None), WorkItem.due_at < now)
    if view == "waiting":
        return stmt.where(WorkItem.status == "waiting")
    if view == "unassigned":
        return stmt.where(WorkItem.status.in_(NONTERMINAL), WorkItem.assigned_admin_id.is_(None))
    if view == "failures":
        return stmt.where(WorkItem.status.in_(NONTERMINAL), WorkItem.type == "failed_system_job")
    if view == "completed":
        return stmt.where(WorkItem.status.in_(("completed", "dismissed")))
    return stmt.where(WorkItem.status.in_(NONTERMINAL))  # default = actionable


# --- synchronization scanners (idempotent + durable-disposition; used by the CLI + worker) ---------
# Each scanner computes a `condition_version` identifying the current INSTANCE of the underlying condition.
# already_handled() then skips creation when a task for that (key, version) already exists in ANY status —
# so a completed/dismissed task is NOT recreated while the condition is unchanged. `budget` caps how many
# NEW items one run may create (the "slow sync has a limit" guard); None = unbounded.
def _capped(budget, n):
    return budget is not None and n >= budget


def sync_unreviewed_requests(session, actor=None, inferred=False, budget=None) -> int:
    n = 0
    for sr in session.exec(select(ServiceRequest).where(ServiceRequest.status == "submitted")).all():
        if _capped(budget, n):
            break
        key = f"review_new_request:req:{sr.id}"
        if already_handled(session, key, "submitted"):
            continue
        if create_work_item_safe(session, actor=actor, type="review_new_request",
                                 title=f"Review new request {sr.tracking_code or sr.id}",
                                 description="A new concierge request is awaiting review.",
                                 tenant_id=sr.owner_id, related_request_id=sr.id, inferred=inferred,
                                 idempotency_key=key, condition_version="submitted", source="automatic"):
            n += 1
    return n


def sync_open_seller_questions(session, actor=None, inferred=False, budget=None) -> int:
    """One requester-visible action per OPEN SellerUpdate question; close it when the SellerUpdate resolves."""
    n = 0
    for su in session.exec(select(SellerUpdate).where(SellerUpdate.seller_question != "")).all():
        key = f"requester_action:su:{su.id}"
        if su.status == "open":
            if _capped(budget, n) or already_handled(session, key, "open"):
                continue
            if create_work_item_safe(session, actor=actor, type="requester_action_required",
                                     title="Requester action required",
                                     description="A published question is awaiting the requester's reply.",
                                     tenant_id=su.seller_id, related_request_id=su.request_id,
                                     related_seller_update_id=su.id, visibility="requester_visible",
                                     waiting_on="requester", idempotency_key=key, condition_version="open",
                                     source="automatic", inferred=inferred):
                n += 1
        else:
            resolve_by_key(session, key, actor, note="seller update resolved")
    return n


def sync_open_duplicates(session, actor=None, inferred=False, budget=None) -> int:
    n = 0
    for dc in session.exec(select(DuplicateCandidate).where(DuplicateCandidate.status == "open")).all():
        if _capped(budget, n):
            break
        key = f"review_dup:cand:{dc.id}"
        if already_handled(session, key, dc.status):
            continue
        if create_work_item_safe(session, actor=actor, type="review_potential_duplicate",
                                 title="Review potential duplicate",
                                 description=f"Duplicate candidate #{dc.id} ({dc.match_type}) awaiting review.",
                                 tenant_id=dc.tenant_id, related_company_id=dc.left_id, inferred=inferred,
                                 idempotency_key=key, condition_version=dc.status, source="automatic"):
            n += 1
    return n


def sync_bounced_contacts(session, actor=None, inferred=False, budget=None) -> int:
    n = 0
    for ld in session.exec(select(Lead).where(Lead.next_action_note == "bounced")).all():
        if _capped(budget, n):
            break
        key = f"replace_contact:lead:{ld.id}"
        version = ld.email or "bounced"       # a new (replacement) address that later bounces = a new version
        if already_handled(session, key, version):
            continue
        if create_work_item_safe(session, actor=actor, type="replace_invalid_contact",
                                 title="Replace invalid contact",
                                 description="A contact bounced and needs a replacement address.",
                                 tenant_id=_lead_tenant(ld), related_lead_id=ld.id, inferred=inferred,
                                 idempotency_key=key, condition_version=version, source="automatic"):
            n += 1
    return n


def sync_failed_jobs(session, actor=None, inferred=False, budget=None) -> int:
    n = 0
    sources = [
        (IngestionRun, "failed_job:ingest", "Failed ingestion run", "Ingestion job failed."),
        (CommandJob, "failed_job:command", "Failed command job", "Command job failed."),
        (Outreach, "failed_job:outreach", "Failed outreach", "Outreach send failed."),
    ]
    for model, prefix, title, default_desc in sources:
        for row in session.exec(select(model).where(model.status == "failed")).all():
            if _capped(budget, n):
                break
            key = f"{prefix}:{row.id}"
            if already_handled(session, key, "failed"):
                continue
            spec = {"related_outreach_id": row.id, "related_lead_id": row.lead_id} if model is Outreach else {}
            if create_work_item_safe(session, actor=actor, type="failed_system_job",
                                     title=f"{title} #{row.id}", inferred=inferred,
                                     description=(getattr(row, "error", "") or default_desc)[:500],
                                     idempotency_key=key, condition_version="failed", source="automatic", **spec):
                n += 1
    return n


def sync_overdue_requests(session, actor=None, inferred=False, budget=None) -> int:
    """Overdue = a request past its due_at that is not yet delivered/completed/rejected/cancelled. Never
    auto-escalates priority (spec: overdue alone must not become Urgent)."""
    from .request_service import effective_workflow
    now = datetime.utcnow()
    done_wf = ("delivered", "completed", "rejected", "cancelled")
    n = 0
    for sr in session.exec(select(ServiceRequest).where(ServiceRequest.due_at.is_not(None),
                                                        ServiceRequest.due_at < now)).all():
        if _capped(budget, n):
            break
        if effective_workflow(sr) in done_wf:
            continue
        key = f"overdue:req:{sr.id}"
        version = sr.due_at.isoformat() if sr.due_at else "overdue"   # a changed due date = a new version
        if already_handled(session, key, version):
            continue
        if create_work_item_safe(session, actor=actor, type="overdue_request",
                                 title=f"Overdue request {sr.tracking_code or sr.id}",
                                 description="This request has passed its due date.",
                                 tenant_id=sr.owner_id, related_request_id=sr.id, inferred=inferred,
                                 idempotency_key=key, condition_version=version, source="automatic"):
            n += 1
    return n


def sync_pending_quotes(session, actor=None, inferred=False, budget=None) -> int:
    """A DRAFT quote awaits admin approval → an Approve-quote task; once it leaves draft the task closes.
    Keeps all quote automation here (idempotent) so the protected quote routes/calculations stay untouched."""
    n = 0
    for q in session.exec(select(Quote)).all():
        key = f"approve_quote:quote:{q.id}"
        if q.status == "draft":
            version = f"draft:v{q.version}"    # a new draft version = a fresh approve task
            if _capped(budget, n) or already_handled(session, key, version):
                continue
            if create_work_item_safe(session, actor=actor, type="approve_quote",
                                     title=f"Approve quote #{q.id}", inferred=inferred,
                                     description="A draft quote is awaiting admin approval.",
                                     tenant_id=q.owner_id, related_quote_id=q.id, related_lead_id=q.lead_id,
                                     idempotency_key=key, condition_version=version, source="automatic"):
                n += 1
        else:
            resolve_by_key(session, key, actor, note="quote left draft")
    return n


def sync_paused_campaigns(session, actor=None, inferred=False, budget=None) -> int:
    """A campaign paused by a failure surfaces a work item; resumed/other campaigns resolve it."""
    n = 0
    for c in session.exec(select(Campaign)).all():
        key = f"campaign_paused:{c.id}"
        if c.status == "paused" and c.pause_reason:
            if _capped(budget, n) or already_handled(session, key, c.pause_reason[:60]):
                continue
            if create_work_item_safe(session, actor=actor, type="campaign_paused", priority="high",
                                     title=f"Campaign paused: {c.name[:60]}",
                                     description=(c.pause_reason or "Campaign paused by failure.")[:300],
                                     tenant_id=c.tenant_id, idempotency_key=key,
                                     condition_version=c.pause_reason[:60], source="automatic",
                                     inferred=inferred):
                n += 1
        elif c.status != "paused":
            resolve_by_key(session, key, actor, note="campaign resumed")
    return n


def sync_high_bounce_rate(session, actor=None, inferred=False, budget=None) -> int:
    """Alert when a campaign's hard-bounce rate exceeds 20% over a meaningful volume. Condition-versioned by
    the current hard-bounce count so a genuinely new bounce spike re-alerts even after dismissal."""
    counted = ("sent", "delivered", "hard_bounced", "soft_bounced", "replied", "positive_reply",
               "negative_reply", "completed")
    n = 0
    for c in session.exec(select(Campaign).where(Campaign.status.in_(("running", "paused")))).all():
        rc = session.exec(select(CampaignRecipient).where(CampaignRecipient.campaign_id == c.id)).all()
        sent = sum(1 for r in rc if r.status in counted)
        hard = sum(1 for r in rc if r.status == "hard_bounced")
        if sent >= 10 and hard / sent >= 0.2:
            key = f"high_bounce_rate:campaign:{c.id}"
            if _capped(budget, n) or already_handled(session, key, f"hb:{hard}"):
                continue
            if create_work_item_safe(session, actor=actor, type="high_bounce_rate", priority="high",
                                     title=f"High bounce rate on {c.name[:50]} ({hard}/{sent})",
                                     description="Hard-bounce rate exceeded 20% — pause and review deliverability.",
                                     tenant_id=c.tenant_id, idempotency_key=key,
                                     condition_version=f"hb:{hard}", source="automatic", inferred=inferred):
                n += 1
    return n


def sync_auth_failed_mailboxes(session, actor=None, inferred=False, budget=None) -> int:
    """A paused Go4it mailbox whose last error looks like an auth failure surfaces an urgent task; a
    recovered (un-paused) mailbox resolves it."""
    n = 0
    for m in session.exec(select(MailAccount).where(MailAccount.admin_owned == True)).all():   # noqa: E712
        err = (m.last_send_error or "").lower()
        key = f"mailbox_auth_failure:{m.id}"
        if m.paused and any(k in err for k in ("auth", "login", "password", "credential", "535", "5.7.8")):
            if _capped(budget, n) or already_handled(session, key, err[:60]):
                continue
            if create_work_item_safe(session, actor=actor, type="mailbox_auth_failure", priority="urgent",
                                     title=f"Mailbox authentication failure: {m.email}",
                                     description="A Go4it mailbox failed authentication and was paused — update credentials.",
                                     idempotency_key=key, condition_version=err[:60], source="automatic",
                                     inferred=inferred):
                n += 1
        elif not m.paused:
            resolve_by_key(session, key, actor, note="mailbox recovered")
    return n


_SCANNERS = [
    ("review_new_request", sync_unreviewed_requests),
    ("requester_action_required", sync_open_seller_questions),
    ("review_potential_duplicate", sync_open_duplicates),
    ("replace_invalid_contact", sync_bounced_contacts),
    ("approve_quote", sync_pending_quotes),
    ("failed_system_job", sync_failed_jobs),
    ("overdue_request", sync_overdue_requests),
    ("campaign_paused", sync_paused_campaigns),
    ("high_bounce_rate", sync_high_bounce_rate),
    ("mailbox_auth_failure", sync_auth_failed_mailboxes),
]


def run_all_sync(session, actor=None, inferred=False, limit=None) -> dict:
    """Idempotent repair pass: get-or-creates every kind of automatic work item. Re-running creates zero
    duplicates and never recreates a dispositioned task (durable via condition_version). `inferred=True` marks
    created items as backfill-seeded. `limit` caps NEW items created this run (bounds a slow sync); the rest
    are picked up next run. Returns a per-kind summary + total + `capped`."""
    summary, total = {}, 0
    for name, fn in _SCANNERS:
        budget = None if limit is None else max(0, limit - total)
        c = fn(session, actor, inferred, budget)
        summary[name] = c
        total += c
    summary["total"] = total
    summary["capped"] = bool(limit is not None and total >= limit)
    return summary
