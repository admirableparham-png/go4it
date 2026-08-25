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
         "spam_complaint", "high_bounce_rate",
         # Phase 5 (Products/Pricing) work-item types
         "product_incomplete", "product_uncategorized", "missing_hs_code", "missing_supplier",
         "missing_origin", "missing_unit", "missing_base_price", "stale_product_verification",
         "expired_price", "expired_rate", "price_needs_approval", "catalog_needs_review",
         "catalog_generation_failed", "ambiguous_import_match", "invalid_document",
         # Phase 6 (Commercial) work-item types
         "quote_needs_review", "quote_missing_pricing", "quote_expired", "quote_change_requested",
         "quote_email_failed", "buyer_reply_needs_review", "contract_needs_review",
         "contract_change_requested", "contract_awaiting_signature", "contract_expired",
         "signed_doc_scan_review", "accepted_quote_needs_deal", "deal_missing_contract",
         "deal_ready_for_handoff", "ambiguous_legacy_commercial",
         # Phase 7 (Operations) work-item types
         "approved_request_needs_case", "operation_missing_data", "freight_request_incomplete",
         "freight_offer_needs_review", "freight_offer_expiring", "booking_confirmation_required",
         "shipment_update_overdue", "tracking_stale", "customs_document_missing", "customs_hold",
         "seller_document_required", "uploaded_document_needs_review", "payment_due", "payment_overdue",
         "payment_confirmation_required", "remittance_compliance_review", "remittance_delayed",
         "delivery_confirmation_required", "cargo_damage_shortage", "settlement_review_required",
         "operational_handoff_required", "external_integration_failure",
         # Phase 8 (Intelligence) work-item types
         "opportunity_needs_review", "opportunity_needs_research", "high_demand_no_supply",
         "supplier_product_verification", "source_stale_failed", "demand_signal_ambiguous",
         "demand_signal_duplicate", "data_quality_anomaly", "report_generation_failed",
         "scheduled_report_review", "seasonal_preparation_due", "other"]
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
    "spam_complaint": "Spam complaint", "high_bounce_rate": "High bounce-rate alert",
    "product_incomplete": "Incomplete product", "product_uncategorized": "Uncategorized product",
    "missing_hs_code": "Missing HS code", "missing_supplier": "Missing supplier", "missing_origin": "Missing origin",
    "missing_unit": "Missing unit", "missing_base_price": "Missing base price",
    "stale_product_verification": "Stale product verification", "expired_price": "Expired price version",
    "expired_rate": "Expired cost rate", "price_needs_approval": "Price version needs approval",
    "catalog_needs_review": "Catalog needs review", "catalog_generation_failed": "Catalog generation failed",
    "ambiguous_import_match": "Ambiguous import match", "invalid_document": "Invalid document",
    "quote_needs_review": "Quote needs review", "quote_missing_pricing": "Quote missing pricing inputs",
    "quote_expired": "Quote expired", "quote_change_requested": "Quote change requested",
    "quote_email_failed": "Quote email delivery failed", "buyer_reply_needs_review": "Buyer reply needs review",
    "contract_needs_review": "Contract needs review", "contract_change_requested": "Contract change requested",
    "contract_awaiting_signature": "Contract awaiting signature", "contract_expired": "Contract expired",
    "signed_doc_scan_review": "Signed document needs scan review",
    "accepted_quote_needs_deal": "Accepted quote needs deal creation",
    "deal_missing_contract": "Deal missing required contract",
    "deal_ready_for_handoff": "Deal ready for operational handoff",
    "ambiguous_legacy_commercial": "Ambiguous legacy commercial relationship",
    "approved_request_needs_case": "Approved request needs operation case",
    "operation_missing_data": "Operation missing required data",
    "freight_request_incomplete": "Freight request incomplete",
    "freight_offer_needs_review": "Freight offer needs review", "freight_offer_expiring": "Freight offer expiring",
    "booking_confirmation_required": "Booking confirmation required",
    "shipment_update_overdue": "Shipment update overdue", "tracking_stale": "Tracking stale",
    "customs_document_missing": "Customs document missing", "customs_hold": "Customs hold",
    "seller_document_required": "Seller document required",
    "uploaded_document_needs_review": "Uploaded document needs review", "payment_due": "Payment due",
    "payment_overdue": "Payment overdue", "payment_confirmation_required": "Payment confirmation required",
    "remittance_compliance_review": "Remittance compliance review", "remittance_delayed": "Remittance delayed",
    "delivery_confirmation_required": "Delivery confirmation required",
    "cargo_damage_shortage": "Cargo damage / shortage", "settlement_review_required": "Settlement review required",
    "operational_handoff_required": "Operational handoff required",
    "external_integration_failure": "External integration failure",
    "opportunity_needs_review": "Opportunity needs review",
    "opportunity_needs_research": "Opportunity needs research",
    "high_demand_no_supply": "High demand without matching supply",
    "supplier_product_verification": "Supplier/product verification required",
    "source_stale_failed": "Data source stale/failed", "demand_signal_ambiguous": "Demand signal ambiguous",
    "demand_signal_duplicate": "Demand signal possible duplicate", "data_quality_anomaly": "Data-quality anomaly",
    "report_generation_failed": "Report generation failed", "scheduled_report_review": "Scheduled report review",
    "seasonal_preparation_due": "Seasonal preparation due", "other": "Other",
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
                 "spam_complaint": "system", "high_bounce_rate": "system",
                 "product_incomplete": "internal", "product_uncategorized": "internal",
                 "missing_hs_code": "internal", "missing_supplier": "supplier", "missing_origin": "internal",
                 "missing_unit": "internal", "missing_base_price": "internal",
                 "stale_product_verification": "internal", "expired_price": "internal", "expired_rate": "internal",
                 "price_needs_approval": "internal", "catalog_needs_review": "internal",
                 "catalog_generation_failed": "system", "ambiguous_import_match": "internal",
                 "invalid_document": "internal",
                 "quote_needs_review": "internal", "quote_missing_pricing": "internal",
                 "quote_expired": "internal", "quote_change_requested": "buyer",
                 "quote_email_failed": "system", "buyer_reply_needs_review": "buyer",
                 "contract_needs_review": "internal", "contract_change_requested": "internal",
                 "contract_awaiting_signature": "internal", "contract_expired": "internal",
                 "signed_doc_scan_review": "internal", "accepted_quote_needs_deal": "internal",
                 "deal_missing_contract": "internal", "deal_ready_for_handoff": "internal",
                 "ambiguous_legacy_commercial": "internal",
                 "approved_request_needs_case": "internal", "operation_missing_data": "internal",
                 "freight_request_incomplete": "internal", "freight_offer_needs_review": "internal",
                 "freight_offer_expiring": "internal", "booking_confirmation_required": "service_provider",
                 "shipment_update_overdue": "service_provider", "tracking_stale": "service_provider",
                 "customs_document_missing": "internal", "customs_hold": "service_provider",
                 "seller_document_required": "seller", "uploaded_document_needs_review": "internal",
                 "payment_due": "internal", "payment_overdue": "buyer",
                 "payment_confirmation_required": "internal", "remittance_compliance_review": "internal",
                 "remittance_delayed": "service_provider", "delivery_confirmation_required": "internal",
                 "cargo_damage_shortage": "service_provider", "settlement_review_required": "internal",
                 "operational_handoff_required": "internal", "external_integration_failure": "system",
                 "opportunity_needs_review": "internal", "opportunity_needs_research": "internal",
                 "high_demand_no_supply": "supplier", "supplier_product_verification": "supplier",
                 "source_stale_failed": "system", "demand_signal_ambiguous": "internal",
                 "demand_signal_duplicate": "internal", "data_quality_anomaly": "internal",
                 "report_generation_failed": "system", "scheduled_report_review": "internal",
                 "seasonal_preparation_due": "internal"}
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
                     related_seller_update_id=None, related_product_id=None, related_contract_id=None,
                     related_operation_case_id=None, related_shipment_id=None, related_payment_id=None,
                     related_exception_id=None, related_opportunity_id=None, related_alert_id=None,
                     parent_id=None, idempotency_key="", condition_version="", inferred=False,
                     due_at=None) -> WorkItem:
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
                  related_seller_update_id=related_seller_update_id, related_product_id=related_product_id,
                  related_contract_id=related_contract_id,
                  related_operation_case_id=related_operation_case_id, related_shipment_id=related_shipment_id,
                  related_payment_id=related_payment_id, related_exception_id=related_exception_id,
                  related_opportunity_id=related_opportunity_id, related_alert_id=related_alert_id,
                  parent_id=parent_id, idempotency_key=idempotency_key, condition_version=condition_version,
                  inferred=inferred, due_at=due_at)
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


# --- Phase 5 product/pricing scanners -------------------------------------------------------------
# The completeness fields whose absence makes a product "incomplete". Each missing field is encoded in the
# condition_version so a task re-opens if a fixed field breaks again, and auto-resolves when all are present.
_PRODUCT_REQUIRED = ("hs_code", "origin", "unit", "base_price", "supplier", "category")


def _missing_fields(session, p):
    from .models import ProductSupplier
    miss = []
    if not (p.hs_code or "").strip():
        miss.append("hs_code")
    if not ((p.origin_country or p.origin_region or "").strip()):
        miss.append("origin")
    if not (p.unit or "").strip():
        miss.append("unit")
    if not (p.exw_price and p.exw_price > 0):
        miss.append("base_price")
    if p.category_id is None and not (p.category or "").strip():
        miss.append("category")
    has_sup = p.supplier_id is not None or session.exec(
        select(ProductSupplier.id).where(ProductSupplier.product_id == p.id)).first() is not None
    if not has_sup:
        miss.append("supplier")
    return miss


def sync_incomplete_products(session, actor=None, inferred=False, budget=None) -> int:
    """One 'incomplete product' task per product missing key fields; version = the sorted missing-field set so a
    re-broken field re-alerts and a fully-completed product auto-resolves. Skips archived products."""
    from .models import Product
    n = 0
    for p in session.exec(select(Product).where(Product.active == True)).all():  # noqa: E712
        miss = _missing_fields(session, p)
        key = f"product_incomplete:product:{p.id}"
        if not miss:
            resolve_by_key(session, key, actor, "product complete")
            continue
        if _capped(budget, n):
            break
        version = ",".join(sorted(miss))
        if already_handled(session, key, version):
            continue
        if create_work_item_safe(session, actor=actor, type="product_incomplete",
                                 title=f"Incomplete product: {p.name[:60]}",
                                 description=f"Missing: {', '.join(miss)}.", related_product_id=p.id,
                                 idempotency_key=key, condition_version=version, inferred=inferred):
            n += 1
    return n


def sync_expired_price_versions(session, actor=None, inferred=False, budget=None) -> int:
    """Flag approved price versions past their rate validity, and price versions awaiting approval."""
    from .models import ProductPriceVersion
    n = 0
    now = datetime.utcnow()
    for pv in session.exec(select(ProductPriceVersion).where(
            ProductPriceVersion.status.in_(("approved", "needs_review", "draft")))).all():
        if _capped(budget, n):
            break
        if pv.status == "needs_review":
            key, ver, typ, title = (f"price_needs_approval:pv:{pv.id}", f"v{pv.version}",
                                    "price_needs_approval", f"Price version needs approval (product {pv.product_id})")
        elif pv.status == "approved" and pv.rate_valid_until and now > pv.rate_valid_until:
            key, ver, typ, title = (f"expired_price:pv:{pv.id}", pv.rate_valid_until.isoformat(),
                                    "expired_price", f"Approved price expired (product {pv.product_id})")
        else:
            continue
        if already_handled(session, key, ver):
            continue
        if create_work_item_safe(session, actor=actor, type=typ, title=title, related_product_id=pv.product_id,
                                 idempotency_key=key, condition_version=ver, inferred=inferred):
            n += 1
    return n


def sync_expired_cost_rates(session, actor=None, inferred=False, budget=None) -> int:
    """Flag active cost rates whose validity window has passed (they must not be used silently)."""
    from .models import CostRate
    n = 0
    now = datetime.utcnow()
    for r in session.exec(select(CostRate).where(CostRate.status == "active")).all():
        if _capped(budget, n):
            break
        if not (r.valid_until and now > r.valid_until):
            continue
        key = f"expired_rate:rate:{r.id}"
        ver = r.valid_until.isoformat()
        if already_handled(session, key, ver):
            continue
        if create_work_item_safe(session, actor=actor, type="expired_rate",
                                 title=f"Expired cost rate: {r.name or r.rate_type}",
                                 description=f"{r.rate_type} valid_until {ver} — review before pricing use.",
                                 idempotency_key=key, condition_version=ver, inferred=inferred):
            n += 1
    return n


def sync_failed_catalog_jobs(session, actor=None, inferred=False, budget=None) -> int:
    """Surface catalog generations that failed (bounded; a failure never blocks other workers)."""
    from .models import CatalogGenerationJob
    n = 0
    for j in session.exec(select(CatalogGenerationJob).where(
            CatalogGenerationJob.status == "failed")).all():
        if _capped(budget, n):
            break
        key = f"catalog_generation_failed:job:{j.id}"
        ver = f"v{j.version}"
        if already_handled(session, key, ver):
            continue
        if create_work_item_safe(session, actor=actor, type="catalog_generation_failed",
                                 title=f"Catalog generation failed (product {j.product_id})",
                                 description=(j.error or "generation failed")[:400], related_product_id=j.product_id,
                                 idempotency_key=key, condition_version=ver, inferred=inferred):
            n += 1
    return n


# --- Phase 6 commercial scanners ------------------------------------------------------------------
def sync_quotes_needing_review(session, actor=None, inferred=False, budget=None) -> int:
    from .models import Quote
    n = 0
    for q in session.exec(select(Quote).where(Quote.status == "needs_review")).all():
        if _capped(budget, n):
            break
        key = f"quote_needs_review:quote:{q.id}"
        if already_handled(session, key, f"v{q.version}"):
            continue
        if create_work_item_safe(session, actor=actor, type="quote_needs_review",
                                 title=f"Quote {q.tracking_code or q.id} needs review",
                                 tenant_id=q.owner_id, related_quote_id=q.id, related_lead_id=q.lead_id,
                                 idempotency_key=key, condition_version=f"v{q.version}", inferred=inferred):
            n += 1
    return n


def sync_expired_quotes(session, actor=None, inferred=False, budget=None) -> int:
    """Flip overdue approved/sent/viewed quotes to expired (never presented active) + raise a task once."""
    from .models import Quote
    from . import quote_workflow as QW
    n = 0
    for q in session.exec(select(Quote).where(Quote.status.in_(("approved", "sent", "viewed")))).all():
        if _capped(budget, n):
            break
        if not QW.is_expired(q):
            continue
        QW.mark_expired_if_due(session, q)
        key = f"quote_expired:quote:{q.id}"
        if already_handled(session, key, f"v{q.version}"):
            continue
        if create_work_item_safe(session, actor=actor, type="quote_expired",
                                 title=f"Quote {q.tracking_code or q.id} expired",
                                 tenant_id=q.owner_id, related_quote_id=q.id, related_lead_id=q.lead_id,
                                 idempotency_key=key, condition_version=f"v{q.version}", inferred=inferred):
            n += 1
    session.commit()
    return n


def sync_accepted_quotes_need_deal(session, actor=None, inferred=False, budget=None) -> int:
    """Durable repair: an accepted quote version with no Deal → a create-deal task (idempotent)."""
    from .models import Deal, Quote, QuoteVersion
    n = 0
    for q in session.exec(select(Quote).where(Quote.status == "accepted")).all():
        if _capped(budget, n):
            break
        ver = session.get(QuoteVersion, q.current_version_id) if q.current_version_id else None
        if ver is None:
            continue
        if session.exec(select(Deal).where(Deal.quote_version_id == ver.id)).first():
            continue
        key = f"accepted_quote_needs_deal:qv:{ver.id}"
        if already_handled(session, key, "accepted"):
            continue
        if create_work_item_safe(session, actor=actor, type="accepted_quote_needs_deal",
                                 title=f"Accepted quote {q.tracking_code or q.id} needs a deal",
                                 tenant_id=q.owner_id, related_quote_id=q.id, related_lead_id=q.lead_id,
                                 idempotency_key=key, condition_version="accepted", inferred=inferred):
            n += 1
    return n


def sync_contracts_awaiting_signature(session, actor=None, inferred=False, budget=None) -> int:
    from .models import Contract
    n = 0
    for c in session.exec(select(Contract).where(Contract.status == "sent")).all():
        if _capped(budget, n):
            break
        key = f"contract_awaiting_signature:contract:{c.id}"
        if already_handled(session, key, c.status):
            continue
        if create_work_item_safe(session, actor=actor, type="contract_awaiting_signature",
                                 title=f"Contract {c.tracking_code or c.id} awaiting signature",
                                 tenant_id=c.tenant_id, related_contract_id=c.id,
                                 idempotency_key=key, condition_version=c.status, inferred=inferred):
            n += 1
    return n


# --- Phase 7 operations scanners ------------------------------------------------------------------
# All are condition-versioned, non-blocking and bounded by BOTH record count (budget) and a wall-clock
# deadline (a slow provider/DB never lets a single scanner run away or block the others).
_SCAN_DEADLINE_S = 5.0


def _deadline(now=None):
    return (now or datetime.utcnow()) + timedelta(seconds=_SCAN_DEADLINE_S)


def sync_freight_offers_expiring(session, actor=None, inferred=False, budget=None) -> int:
    """A selected/offered freight offer inside its last 48h of validity → a review task (never auto-uses an
    expired offer)."""
    from .models import FreightOffer, FreightRequest
    n = 0
    now = datetime.utcnow()
    soon = now + timedelta(hours=48)
    stop = _deadline(now)
    for o in session.exec(select(FreightOffer).where(FreightOffer.selection_status.in_(("offered", "selected")),
                                                     FreightOffer.valid_until != None)).all():  # noqa: E711
        if _capped(budget, n) or datetime.utcnow() > stop:
            break
        if not (now < o.valid_until <= soon):
            continue
        fr = session.get(FreightRequest, o.freight_request_id)
        key = f"freight_offer_expiring:offer:{o.id}"
        if already_handled(session, key, o.valid_until.isoformat()):
            continue
        if create_work_item_safe(session, actor=actor, type="freight_offer_expiring",
                                 title=f"Freight offer on {fr.reference if fr else o.freight_request_id} expiring",
                                 tenant_id=fr.tenant_id if fr else None,
                                 related_operation_case_id=fr.operation_case_id if fr else None,
                                 idempotency_key=key, condition_version=o.valid_until.isoformat(),
                                 inferred=inferred):
            n += 1
    return n


def sync_stale_tracking(session, actor=None, inferred=False, budget=None) -> int:
    """An in-transit shipment with no tracking update in >72h → a tracking_stale task (+ mark it for an
    exception on review). Never invents a position; just flags the silence."""
    from .models import Shipment
    n = 0
    now = datetime.utcnow()
    cutoff = now - timedelta(hours=72)
    stop = _deadline(now)
    for s in session.exec(select(Shipment).where(Shipment.current_milestone == "in_transit",
                                                 Shipment.status == "active")).all():
        if _capped(budget, n) or datetime.utcnow() > stop:
            break
        last = s.last_tracking_update or s.actual_departure
        if last and last > cutoff:
            continue
        bucket = (last or s.created_at).strftime("%Y%m%d")
        key = f"tracking_stale:shipment:{s.id}"
        if already_handled(session, key, bucket):
            continue
        if create_work_item_safe(session, actor=actor, type="tracking_stale",
                                 title=f"Tracking stale on {s.reference}",
                                 tenant_id=s.tenant_id, related_shipment_id=s.id, related_deal_id=s.deal_id,
                                 related_operation_case_id=s.operation_case_id,
                                 idempotency_key=key, condition_version=bucket, inferred=inferred):
            n += 1
    return n


def sync_booking_confirmation(session, actor=None, inferred=False, budget=None) -> int:
    """A shipment still in 'planning' with a selected freight offer → booking confirmation required."""
    from .models import Shipment
    n = 0
    stop = _deadline()
    for s in session.exec(select(Shipment).where(Shipment.current_milestone == "planning",
                                                 Shipment.status == "active",
                                                 Shipment.freight_offer_id != None)).all():  # noqa: E711
        if _capped(budget, n) or datetime.utcnow() > stop:
            break
        key = f"booking_confirmation_required:shipment:{s.id}"
        if already_handled(session, key, "planning"):
            continue
        if create_work_item_safe(session, actor=actor, type="booking_confirmation_required",
                                 title=f"Confirm booking for {s.reference}",
                                 tenant_id=s.tenant_id, related_shipment_id=s.id, related_deal_id=s.deal_id,
                                 idempotency_key=key, condition_version="planning", inferred=inferred):
            n += 1
    return n


def sync_delivery_confirmation(session, actor=None, inferred=False, budget=None) -> int:
    """An import-cleared shipment with no delivery confirmation → delivery confirmation required (never
    auto-delivered by a passed ETA)."""
    from .models import DeliveryConfirmation, Shipment
    n = 0
    stop = _deadline()
    for s in session.exec(select(Shipment).where(Shipment.current_milestone == "import_cleared",
                                                 Shipment.status == "active")).all():
        if _capped(budget, n) or datetime.utcnow() > stop:
            break
        if session.exec(select(DeliveryConfirmation).where(DeliveryConfirmation.shipment_id == s.id)).first():
            continue
        key = f"delivery_confirmation_required:shipment:{s.id}"
        if already_handled(session, key, "import_cleared"):
            continue
        if create_work_item_safe(session, actor=actor, type="delivery_confirmation_required",
                                 title=f"Confirm delivery for {s.reference}",
                                 tenant_id=s.tenant_id, related_shipment_id=s.id, related_deal_id=s.deal_id,
                                 idempotency_key=key, condition_version="import_cleared", inferred=inferred):
            n += 1
    return n


def sync_overdue_payments(session, actor=None, inferred=False, budget=None) -> int:
    """A planned/awaiting payment milestone past its due date → payment_overdue."""
    from .models import PaymentMilestone
    n = 0
    now = datetime.utcnow()
    stop = _deadline(now)
    for p in session.exec(select(PaymentMilestone).where(
            PaymentMilestone.status.in_(("planned", "awaiting")),
            PaymentMilestone.due_date != None)).all():  # noqa: E711
        if _capped(budget, n) or datetime.utcnow() > stop:
            break
        if p.due_date >= now:
            continue
        bucket = p.due_date.strftime("%Y%m%d")
        key = f"payment_overdue:pm:{p.id}"
        if already_handled(session, key, bucket):
            continue
        if create_work_item_safe(session, actor=actor, type="payment_overdue", priority="high",
                                 title=f"Payment {p.reference} overdue",
                                 tenant_id=p.tenant_id, related_payment_id=p.id, related_deal_id=p.deal_id,
                                 idempotency_key=key, condition_version=bucket, inferred=inferred):
            n += 1
    return n


def sync_settlements_review(session, actor=None, inferred=False, budget=None) -> int:
    """A delivered deal whose buyer payments are all received and with no settlement yet → settlement review."""
    from .models import Deal, PaymentMilestone, Settlement
    n = 0
    stop = _deadline()
    for d in session.exec(select(Deal).where(Deal.stage == "delivered")).all():
        if _capped(budget, n) or datetime.utcnow() > stop:
            break
        if session.exec(select(Settlement).where(Settlement.deal_id == d.id)).first():
            continue
        buyer = session.exec(select(PaymentMilestone).where(
            PaymentMilestone.deal_id == d.id,
            PaymentMilestone.milestone_type.in_(("buyer_deposit", "buyer_balance")))).all()
        if not buyer or any(p.status != "received" for p in buyer):
            continue
        key = f"settlement_review_required:deal:{d.id}"
        if already_handled(session, key, "delivered"):
            continue
        if create_work_item_safe(session, actor=actor, type="settlement_review_required",
                                 title=f"Deal {d.tracking_code} ready to settle",
                                 tenant_id=d.owner_id, related_deal_id=d.id,
                                 idempotency_key=key, condition_version="delivered", inferred=inferred):
            n += 1
    return n


# --- Phase 8 intelligence scanners (bounded by count + wall-clock; condition-versioned) ---------------
def sync_opportunities_needing_review(session, actor=None, inferred=False, budget=None) -> int:
    """A new/ready opportunity → a review task. condition_version is a score bucket so a materially changed
    score regenerates the task, while an unchanged one never does."""
    from .models import Opportunity
    n = 0
    stop = _deadline()
    for o in session.exec(select(Opportunity).where(
            Opportunity.status.in_(("new", "ready_for_review")))).all():
        if _capped(budget, n) or datetime.utcnow() > stop:
            break
        bucket = f"s{o.score // 10 * 10}"
        key = f"opportunity_needs_review:opp:{o.id}"
        if already_handled(session, key, bucket):
            continue
        if create_work_item_safe(session, actor=actor, type="opportunity_needs_review",
                                 title=f"Opportunity {o.reference or o.id} needs review",
                                 tenant_id=o.tenant_id, related_opportunity_id=o.id,
                                 idempotency_key=key, condition_version=bucket, inferred=inferred):
            n += 1
    return n


def sync_demand_signals(session, actor=None, inferred=False, budget=None) -> int:
    """Generate DemandSignals + Opportunities from DETERMINISTIC positive evidence (accepted quotes, Deals,
    admin-confirmed positive replies) that don't have one yet. Idempotent (dedup_key), bounded. Never touches a
    scraped lead, a negative reply, an open/delivery or a bounce. Runs as the live demand-generation pass."""
    from . import demand as DEM
    from . import opportunities as OPP
    from .models import Deal, Lead, Quote
    n = 0
    stop = _deadline()
    for q in session.exec(select(Quote).where(Quote.status == "accepted")).all():
        if _capped(budget, n) or datetime.utcnow() > stop:
            return n
        sig, created = DEM.from_accepted_quote(session, q, actor=actor, inferred=inferred)
        if created and sig:
            OPP.ensure_from_signal(session, sig, actor=actor)
            n += 1
    for d in session.exec(select(Deal)).all():
        if _capped(budget, n) or datetime.utcnow() > stop:
            return n
        sig, created = DEM.from_deal(session, d, actor=actor, inferred=inferred)
        if created and sig:
            OPP.ensure_from_signal(session, sig, actor=actor)
            n += 1
    for ld in session.exec(select(Lead).where(Lead.reply_outcome == "positive")).all():
        if _capped(budget, n) or datetime.utcnow() > stop:
            return n
        sig, created = DEM.from_positive_reply(session, ld, actor=actor, inferred=inferred)
        if created and sig:
            OPP.ensure_from_signal(session, sig, actor=actor)
            n += 1
    return n


def sync_stale_sources(session, actor=None, inferred=False, budget=None) -> int:
    """A stale/failed configured data source → a source_stale_failed task."""
    from . import data_sources as DS
    n = 0
    stop = _deadline()
    for h in DS.source_health(session):
        if _capped(budget, n) or datetime.utcnow() > stop:
            break
        if h["freshness"] not in ("Stale", "Failed"):
            continue
        key = f"source_stale_failed:src:{h['key']}"
        if already_handled(session, key, h["freshness"]):
            continue
        if create_work_item_safe(session, actor=actor, type="source_stale_failed",
                                 title=f"Data source {h['label']} is {h['freshness'].lower()}",
                                 description=(h.get("error") or "")[:300],
                                 idempotency_key=key, condition_version=h["freshness"], inferred=inferred):
            n += 1
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
    # Phase 5 product/pricing scanners
    ("product_incomplete", sync_incomplete_products),
    ("expired_price", sync_expired_price_versions),
    ("expired_rate", sync_expired_cost_rates),
    ("catalog_generation_failed", sync_failed_catalog_jobs),
    # Phase 6 commercial scanners
    ("quote_needs_review", sync_quotes_needing_review),
    ("quote_expired", sync_expired_quotes),
    ("accepted_quote_needs_deal", sync_accepted_quotes_need_deal),
    ("contract_awaiting_signature", sync_contracts_awaiting_signature),
    # Phase 7 operations scanners
    ("freight_offer_expiring", sync_freight_offers_expiring),
    ("tracking_stale", sync_stale_tracking),
    ("booking_confirmation_required", sync_booking_confirmation),
    ("delivery_confirmation_required", sync_delivery_confirmation),
    ("payment_overdue", sync_overdue_payments),
    ("settlement_review_required", sync_settlements_review),
    # Phase 8 intelligence scanners
    ("demand_signals", sync_demand_signals),
    ("opportunity_needs_review", sync_opportunities_needing_review),
    ("source_stale_failed", sync_stale_sources),
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
