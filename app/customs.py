"""Phase 7 — customs coordination (NOT legal/customs advice).

A CustomsCase tracks an export/import clearance interaction. Clearance is set only by an explicit status change
— never inferred from shipment movement. Broker contacts + buyer/importer info are ADMIN-ONLY. A query/hold or
rejection raises an OperationalException; entering `documents_required` raises a customs_document_missing task.
"""
from datetime import datetime

from . import ops_exceptions as EXC
from .models import CustomsCase, OperationCase
from .pipeline import audit

STATUSES = ("not_started", "documents_required", "ready_to_submit", "submitted", "query_hold", "cleared",
            "rejected", "cancelled")


def _ref(row_id, now=None):
    now = now or datetime.utcnow()
    return f"CU-{now:%Y%m}-{row_id:04d}"


def create_customs_case(session, *, case: OperationCase = None, side="export", country="", actor=None,
                        now=None, **fields):
    now = now or datetime.utcnow()
    cc = CustomsCase(side=side, country=country, owner_id=getattr(actor, "id", None), created_at=now,
                     updated_at=now, **fields)
    if case is not None:
        cc.operation_case_id = case.id
        cc.deal_id = cc.deal_id or case.deal_id
        cc.tenant_id = cc.tenant_id or case.tenant_id
    session.add(cc)
    session.flush()
    cc.reference = _ref(cc.id, now)
    session.add(cc)
    audit(session, actor, "customs_case", cc.id, "customs_case_created", {"side": side, "country": country},
          tenant_id=cc.tenant_id)
    return cc


def set_customs_status(session, cc: CustomsCase, to_status: str, *, reason="", actor=None, now=None):
    """Move a customs case to `to_status`. Returns (ok, error). Sets submitted/clearance dates on the
    corresponding transitions; a query/hold or rejection raises an exception; entering documents_required
    raises a document task. Clearance is NEVER auto-derived — only this explicit call sets it."""
    now = now or datetime.utcnow()
    if to_status not in STATUSES:
        return False, "unknown customs status"
    frm = cc.status
    cc.status = to_status
    cc.updated_at = now
    if to_status == "submitted" and not cc.submitted_date:
        cc.submitted_date = now
    if to_status == "cleared":
        cc.clearance_date = now
        cc.hold_reason = ""
    if to_status in ("query_hold", "rejected"):
        cc.hold_reason = (reason or "")[:500]
    session.add(cc)
    audit(session, actor, "customs_case", cc.id, "customs_status_change",
          {"from": frm, "to": to_status}, tenant_id=cc.tenant_id)
    if to_status in ("query_hold", "rejected"):
        EXC.raise_exception(
            session, exc_type="customs_hold" if to_status == "query_hold" else "customs_rejection",
            severity="high", actor=actor,
            internal_description=f"Customs {cc.reference} {to_status}: {reason}"[:1000],
            seller_safe_description="Customs clearance needs attention.",
            operation_case_id=cc.operation_case_id, deal_id=cc.deal_id, tenant_id=cc.tenant_id, now=now)
    elif to_status == "documents_required":
        _docs_task(session, cc)
    return True, ""


def _docs_task(session, cc):
    try:
        from . import work_queue as WQ
        WQ.create_work_item_safe(
            session, tenant_id=cc.tenant_id, type="customs_document_missing", source="automatic",
            title=f"Customs {cc.reference}: documents required",
            description=f"{cc.side} clearance in {cc.country} is blocked pending documents.",
            related_operation_case_id=cc.operation_case_id, related_deal_id=cc.deal_id,
            idempotency_key=f"customs_document_missing:customs:{cc.id}", condition_version="documents_required")
    except Exception:  # noqa: BLE001
        pass


def customs_seller_view(cc: CustomsCase) -> dict:
    """Seller-safe: side + coarse status only — never broker identity, declaration numbers, or hold reasons."""
    return {"reference": cc.reference, "side": cc.side, "country": cc.country,
            "cleared": cc.status == "cleared"}
