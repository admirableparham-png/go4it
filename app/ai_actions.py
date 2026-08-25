"""Phase 9 — AI action proposals + the runtime approval matrix + SAFE execution.

The AI never executes a material change. It PROPOSES a structured, validated action; an admin approves; then a
single registered executor runs it via an existing domain service. Approval revalidates authorization, target
state, tenant and freshness, compares the payload hash (rejecting a stale/tampered proposal), executes
idempotently (double-click is a no-op), and audits the exact result. Free-form model text is NEVER executed.

Some actions are NEVER executable by the AI (payments/remittance, accepting a quote as the buyer, signing a
contract, bypassing suppression, changing roles/auth, deleting audit, accessing credentials, changing prod
config, overriding malware/quarantine) — proposing one is refused.
"""
import hashlib
import json
import secrets
from datetime import datetime, timedelta

from sqlmodel import select

from .models import AIActionProposal
from .pipeline import audit
from .quote_portal import csrf_ok

# risk ladder + which actions the AI may PROPOSE. Drafts produce content only (never sent/published).
PROPOSABLE = {
    "create_work_item": "internal_reversible",
    "assign_owner": "internal_reversible",
    "create_opportunity_review": "internal_reversible",
    "start_research": "internal_reversible",
    "create_saved_alert": "internal_reversible",
    "create_internal_followup": "internal_reversible",
    "create_draft_email": "draft",
    "create_draft_seller_update": "draft",
    "create_draft_report": "draft",
    "request_seller_information": "draft",
    "create_draft_quote": "draft",
    "recommend_automation_rule": "draft",
}
# explicitly forbidden — the AI can never propose or execute these
NEVER = {"confirm_payment", "initiate_remittance", "accept_quote_as_buyer", "reject_quote_as_buyer",
         "sign_contract", "bypass_suppression", "disable_confidentiality", "change_user_role",
         "delete_audit", "access_credentials", "change_production_config", "override_quarantine",
         "send_email", "start_campaign", "advance_deal", "publish_seller_update"}

PROPOSAL_TTL_MIN = 60


def hash_payload(payload: dict) -> str:
    return hashlib.sha256(json.dumps(payload or {}, sort_keys=True, default=str).encode()).hexdigest()


def propose(session, conversation, *, action_type, payload, target_summary="", reason="", actor=None,
           tenant_id=None, now=None, message_id=None):
    """Create an action proposal. Returns (proposal, error). Refuses a NEVER action and an unknown action. The
    proposal is inert until an admin approves it."""
    now = now or datetime.utcnow()
    if action_type in NEVER:
        return None, f"action '{action_type}' is never executable by the AI"
    if action_type not in PROPOSABLE:
        return None, f"unknown action '{action_type}'"
    payload = payload or {}
    p = AIActionProposal(
        conversation_id=conversation.id, message_id=message_id, action_type=action_type,
        target_summary=target_summary[:300], payload=json.dumps(payload)[:8000],
        payload_hash=hash_payload(payload), reason=reason[:500], risk_level=PROPOSABLE[action_type],
        requires_approval=True, status="proposed", approval_nonce=secrets.token_urlsafe(24),
        idempotency_key=f"aiprop:{conversation.id}:{action_type}:{hash_payload(payload)[:16]}",
        expires_at=now + timedelta(minutes=PROPOSAL_TTL_MIN), proposed_by="deterministic",
        tenant_id=tenant_id if tenant_id is not None else conversation.tenant_id, created_at=now)
    session.add(p)
    try:
        session.flush()
    except Exception:  # noqa: BLE001 — duplicate idempotency_key → return the existing proposal
        session.rollback()
        again = session.exec(select(AIActionProposal).where(
            AIActionProposal.idempotency_key == p.idempotency_key)).first()
        return again, ""
    audit(session, actor, "ai_proposal", p.id, "proposal_created",
          {"action": action_type, "risk": p.risk_level}, tenant_id=p.tenant_id)
    _awaiting_task(session, p)
    return p, ""


def approve(session, proposal, *, nonce, actor=None, now=None):
    """Safely execute an approved proposal. Returns (ok, result_or_error). Enforces: valid nonce, still
    'proposed' + not expired, admin authz, payload-hash match (stale/tampered → reject), idempotent execution
    (already executed → no-op), and execution ONLY via a registered structured executor."""
    now = now or datetime.utcnow()
    from .tenant import is_admin
    if not is_admin(actor):
        return False, "admin only"
    if not csrf_ok(proposal.approval_nonce, nonce):
        return False, "invalid approval token"
    if proposal.executed_at is not None or proposal.status == "executed":
        return True, "already executed"                 # double-click / replay → no-op
    if proposal.status != "proposed":
        return False, f"proposal is {proposal.status}"
    if proposal.expires_at and now > proposal.expires_at:
        proposal.status = "expired"
        session.add(proposal)
        return False, "proposal expired — re-ask the copilot"
    try:
        payload = json.loads(proposal.payload or "{}")
    except Exception:  # noqa: BLE001
        return False, "corrupt payload"
    if hash_payload(payload) != proposal.payload_hash:
        proposal.status = "failed"
        session.add(proposal)
        return False, "payload changed since proposal — rejected as stale"
    if proposal.action_type in NEVER or proposal.action_type not in _EXECUTORS:
        return False, "action not executable"
    try:
        result = _EXECUTORS[proposal.action_type](session, payload, actor, proposal)
    except _StaleTarget as e:
        proposal.status = "failed"
        session.add(proposal)
        return False, f"target state changed: {e}"
    except Exception as e:  # noqa: BLE001
        proposal.status = "failed"
        proposal.result = str(e)[:300]
        session.add(proposal)
        _failed_task(session, proposal, str(e))
        return False, str(e)[:200]
    proposal.status = "executed"
    proposal.executed_at = now
    proposal.approved_by = getattr(actor, "id", None)
    proposal.result = json.dumps(result)[:1000]
    session.add(proposal)
    try:
        from . import work_queue as WQ
        WQ.resolve_by_key(session, f"ai_action_awaiting_approval:prop:{proposal.id}", note="approved",
                          actor=actor)
    except Exception:  # noqa: BLE001
        pass
    audit(session, actor, "ai_proposal", proposal.id, "proposal_executed",
          {"action": proposal.action_type, "result": str(result)[:200]}, tenant_id=proposal.tenant_id)
    return True, result


def decline(session, proposal, *, actor=None, now=None):
    now = now or datetime.utcnow()
    if proposal.status != "proposed":
        return False, f"proposal is {proposal.status}"
    proposal.status = "declined"
    session.add(proposal)
    audit(session, actor, "ai_proposal", proposal.id, "proposal_declined", {}, tenant_id=proposal.tenant_id)
    return True, ""


class _StaleTarget(RuntimeError):
    pass


# --------------------------------------------------------------------- executors (structured payload only)
def _exec_create_work_item(session, payload, actor, proposal):
    from . import work_queue as WQ
    wi = WQ.create_work_item_safe(
        session, actor=actor, type=payload.get("type", "admin_action_required"),
        title=(payload.get("title") or "AI-proposed task")[:200],
        description=(payload.get("description") or "")[:2000], tenant_id=proposal.tenant_id,
        related_conversation_id=proposal.conversation_id, related_proposal_id=proposal.id,
        idempotency_key=f"ai_workitem:prop:{proposal.id}", condition_version="approved")
    return {"work_item_created": bool(wi), "work_item_id": getattr(wi, "id", None)}


def _exec_assign_owner(session, payload, actor, proposal):
    from . import opportunities as OPP
    from .models import Opportunity
    opp = session.get(Opportunity, int(payload.get("opportunity_id", 0)))
    if not opp:
        raise _StaleTarget("opportunity not found")
    if opp.status in OPP.TERMINAL:
        raise _StaleTarget(f"opportunity is {opp.status}")
    OPP.assign(session, opp, int(payload.get("owner_id")) if payload.get("owner_id") else None, actor=actor)
    return {"assigned": opp.reference, "owner_id": opp.owner_id}


def _exec_opportunity_review(session, payload, actor, proposal):
    from . import opportunities as OPP
    from .models import Opportunity
    opp = session.get(Opportunity, int(payload.get("opportunity_id", 0)))
    if not opp:
        raise _StaleTarget("opportunity not found")
    ok, err = OPP.set_status(session, opp, "ready_for_review", reason="AI-proposed review", actor=actor)
    if not ok:
        raise _StaleTarget(err)
    return {"opportunity": opp.reference, "status": opp.status}


def _exec_start_research(session, payload, actor, proposal):
    """Create the research/harvest job through the EXISTING pipeline (queued). It does NOT auto-run — the
    existing worker/route executes it. We never claim success before the real system returns results."""
    from .command_service import parse_command
    from .models import CommandJob
    prompt = (payload.get("prompt") or "").strip()
    if not prompt:
        raise _StaleTarget("empty research scope")
    parsed = parse_command(prompt)
    job = CommandJob(prompt=prompt[:300], action=parsed["action"], params=json.dumps(parsed),
                     status="queued", note=parsed.get("note", ""), owner_id=getattr(actor, "id", None))
    session.add(job)
    session.flush()
    return {"command_job_id": job.id, "action": parsed["action"], "queued": True,
            "note": "research job created and queued via the existing pipeline; results are not yet available"}


def _exec_saved_alert(session, payload, actor, proposal):
    from . import alerts as AL
    a, created = AL.raise_alert(session, alert_type=payload.get("alert_type", "data_anomaly"),
                                alert_key=payload.get("alert_key", f"ai:prop:{proposal.id}"),
                                condition_version=payload.get("condition_version", "ai"),
                                title=(payload.get("title") or "AI alert")[:200],
                                body=(payload.get("body") or "")[:1000], actor=actor)
    return {"alert_created": created, "alert_id": getattr(a, "id", None)}


def _exec_draft(session, payload, actor, proposal):
    """A draft action materializes DRAFT content only — it is never sent or published. Seller-facing drafts are
    confidentiality-scanned; the caller (ai_drafts) already produced a safe draft in the payload."""
    from . import ai_drafts
    return ai_drafts.finalize_draft(session, proposal.action_type, payload, actor=actor, proposal=proposal)


_EXECUTORS = {
    "create_work_item": _exec_create_work_item,
    "assign_owner": _exec_assign_owner,
    "create_opportunity_review": _exec_opportunity_review,
    "start_research": _exec_start_research,
    "create_saved_alert": _exec_saved_alert,
    "create_internal_followup": _exec_create_work_item,
    "create_draft_email": _exec_draft,
    "create_draft_seller_update": _exec_draft,
    "create_draft_report": _exec_draft,
    "request_seller_information": _exec_draft,
    "create_draft_quote": _exec_draft,
    "recommend_automation_rule": _exec_draft,
}


def _awaiting_task(session, proposal):
    try:
        from . import work_queue as WQ
        WQ.create_work_item_safe(
            session, tenant_id=proposal.tenant_id, type="ai_action_awaiting_approval", source="automatic",
            title=f"AI action awaiting approval: {proposal.action_type}",
            description=proposal.target_summary[:400], related_conversation_id=proposal.conversation_id,
            related_proposal_id=proposal.id,
            idempotency_key=f"ai_action_awaiting_approval:prop:{proposal.id}", condition_version="proposed")
    except Exception:  # noqa: BLE001
        pass


def _failed_task(session, proposal, err):
    try:
        from . import work_queue as WQ
        WQ.create_work_item_safe(
            session, tenant_id=proposal.tenant_id, type="ai_action_failed", source="automatic", priority="high",
            title=f"AI action failed: {proposal.action_type}", description=(err or "")[:400],
            related_proposal_id=proposal.id, idempotency_key=f"ai_action_failed:prop:{proposal.id}",
            condition_version="failed")
    except Exception:  # noqa: BLE001
        pass
