"""Requests + Work Queue (Phase 3) — request workflow helpers.

The legacy `ServiceRequest.status` (submitted|approved|rejected|running|done) is UNCHANGED and stays the
operational driver for `scripts/load_managed_buyers.py`, `worker.run_request_reminders`, the pending badge, sort
and existing templates. This module adds the richer, additive **workflow_status** (11 states) that the admin
Requests surface + Work Queue read, plus the authoritative status-change history. Legacy and workflow are kept
loosely in sync via `advance_workflow` (called by the existing transition routes) so their meanings never
silently diverge.
"""
from datetime import datetime

from sqlmodel import select

from .models import RequestStatusEvent, ServiceRequest
from .pipeline import audit

# The 11 additive workflow states (in order).
WORKFLOW_STATES = [
    "submitted", "under_review", "approved", "in_progress", "waiting_requester", "waiting_external",
    "ready_for_delivery", "delivered", "completed", "rejected", "cancelled",
]
WORKFLOW_LABELS = {
    "submitted": "Submitted", "under_review": "Under review", "approved": "Approved",
    "in_progress": "In progress", "waiting_requester": "Waiting for requester",
    "waiting_external": "Waiting for external party", "ready_for_delivery": "Ready for delivery",
    "delivered": "Delivered", "completed": "Completed", "rejected": "Rejected", "cancelled": "Cancelled",
}
# workflow_status -> a badge token understood by base.html's badge CSS
WORKFLOW_BADGE = {
    "submitted": "queued", "under_review": "amber", "approved": "approved", "in_progress": "running",
    "waiting_requester": "amber", "waiting_external": "amber", "ready_for_delivery": "sky",
    "delivered": "ok", "completed": "won", "rejected": "error", "cancelled": "slate",
}
# The compatibility map: the legacy 5-state status -> its workflow equivalent. Used to derive workflow_status
# for any request that predates Phase 3 (blank workflow_status) and to keep the two in step.
LEGACY_TO_WORKFLOW = {
    "submitted": "submitted", "approved": "approved", "running": "in_progress",
    "done": "delivered", "rejected": "rejected",
}
# When the admin advances the additive workflow_status, keep the LEGACY status (the ONLY status a seller
# sees) consistent so admin and seller-visible states can NEVER contradict. None = an intermediate state that
# leaves the legacy status unchanged (the seller keeps seeing "in progress"). The seller never sees the finer
# workflow states — only this reconciled legacy status — so e.g. workflow "cancelled" maps to legacy
# "rejected" (both read as "closed, not delivered"), never leaving the seller on a stale "approved".
WORKFLOW_TO_LEGACY = {
    "submitted": "submitted", "under_review": "submitted", "approved": "approved",
    "in_progress": "running", "waiting_requester": None, "waiting_external": None,
    "ready_for_delivery": None, "delivered": "done", "completed": "done",
    "rejected": "rejected", "cancelled": "rejected",
}
# request_type -> default direction (existing buyer-search requests are sell-side).
TYPE_TO_DIRECTION = {
    "buyer_hunt": "sell", "find_supplier": "buy",
    "remittance": "service", "freight": "service", "contract": "service", "docs": "service",
    "market_research": "service", "other": "service",
}


def workflow_from_legacy(legacy: str) -> str:
    """The workflow_status equivalent of a legacy status (default 'submitted' for unknown)."""
    return LEGACY_TO_WORKFLOW.get((legacy or "").strip(), "submitted")


def effective_workflow(sr: ServiceRequest) -> str:
    """The request's workflow_status, deriving it from the legacy status when not yet set (read-only)."""
    return sr.workflow_status or workflow_from_legacy(sr.status)


def direction_for_type(request_type: str) -> str:
    """Default direction for a request type (sell|buy|service)."""
    return TYPE_TO_DIRECTION.get((request_type or "").strip(), "service")


def touch_activity(sr: ServiceRequest) -> None:
    """Bump last_activity_at — call after any admin/requester action on a request."""
    sr.last_activity_at = datetime.utcnow()


def reconcile_legacy(sr: ServiceRequest, to: str) -> bool:
    """Keep the seller-visible legacy `status` consistent with the additive workflow state `to`, so the
    admin and seller can never see contradictory states. Intermediate states (waiting_*, ready_for_delivery)
    leave legacy unchanged. Sets the matching lifecycle timestamp. Returns True if legacy changed."""
    legacy = WORKFLOW_TO_LEGACY.get(to)
    if not legacy or sr.status == legacy:
        return False
    sr.status = legacy
    now = datetime.utcnow()
    if legacy == "approved" and not sr.approved_at:
        sr.approved_at = now
    elif legacy == "running" and not sr.started_at:
        sr.started_at = now
    elif legacy in ("done", "rejected") and not sr.done_at:
        sr.done_at = now
    return True


def states_consistent(sr: ServiceRequest) -> bool:
    """Invariant: the legacy status and the effective workflow_status can never contradict — the legacy
    status must be the workflow's mapped legacy (or, for intermediate workflow states, the request's prior
    in-flight legacy). Used by tests to prove no conflicting admin/seller-visible state can arise."""
    wf = effective_workflow(sr)
    mapped = WORKFLOW_TO_LEGACY.get(wf)
    if mapped is not None:
        return sr.status == mapped
    # intermediate workflow (waiting_*/ready_for_delivery): legacy must be an in-flight, non-terminal value
    return sr.status in ("submitted", "approved", "running")


def advance_workflow(session, sr: ServiceRequest, to: str, actor=None, reason: str = "") -> tuple[bool, str]:
    """Move a request's additive workflow_status to `to`, recording an authoritative RequestStatusEvent,
    bumping last_activity_at, and writing an audit row. Does NOT touch the legacy `status` (the caller owns
    that) — this only enriches the Phase-3 surface. Idempotent no-op when already at `to`. Returns (ok, err)."""
    to = (to or "").strip()
    if to not in WORKFLOW_STATES:
        return False, f"unknown workflow status: {to}"
    frm = effective_workflow(sr)
    if frm == to and sr.workflow_status:
        touch_activity(sr)
        session.add(sr)
        return True, ""
    sr.workflow_status = to
    touch_activity(sr)
    session.add(sr)
    session.add(RequestStatusEvent(request_id=sr.id, from_status=frm, to_status=to,
                                   from_legacy=sr.status, to_legacy=sr.status,
                                   actor_id=getattr(actor, "id", None), reason=(reason or "")[:500]))
    audit(session, actor, "request", sr.id, "request_status_change",
          {"from": frm, "to": to, "reason": (reason or "")[:200]}, tenant_id=sr.owner_id)
    return True, ""


def status_history(session, request_id: int):
    """The request's workflow status history, oldest first."""
    return session.exec(select(RequestStatusEvent).where(RequestStatusEvent.request_id == request_id)
                        .order_by(RequestStatusEvent.id)).all()
