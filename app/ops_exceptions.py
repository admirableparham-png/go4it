"""Phase 7 — operational exceptions (central).

An OperationalException carries an INTERNAL description and a separate seller-safe description; only the latter
may ever reach a seller (after sanitization). Raising one is de-duplicated (an already-open exception for the
same (type, linked record) is reused, not re-created) and it integrates with the Work Queue WITHOUT creating a
duplicate unresolved task.
"""
from datetime import datetime

from sqlmodel import select

from .models import OperationalException, Shipment
from .pipeline import audit

EXC_TYPES = ("missing_documents", "document_rejected", "booking_failed", "provider_unresponsive",
             "tracking_stale", "departure_delayed", "customs_hold", "customs_rejection", "payment_overdue",
             "payment_failed", "remittance_failure", "cargo_damage", "shortage", "failed_delivery",
             "system_failure", "compliance_review")
SEVERITIES = ("low", "medium", "high", "critical")
STATUSES = ("open", "investigating", "waiting_seller", "waiting_buyer", "waiting_provider", "resolved",
            "dismissed")
OPEN_STATUSES = ("open", "investigating", "waiting_seller", "waiting_buyer", "waiting_provider")


def _ref(row_id, now=None):
    now = now or datetime.utcnow()
    return f"EX-{now:%Y%m}-{row_id:04d}"


def raise_exception(session, *, exc_type, severity="medium", actor=None, internal_description="",
                    seller_safe_description="", operation_case_id=None, shipment_id=None, deal_id=None,
                    tenant_id=None, due_date=None, now=None, wq=True):
    """Create (or reuse) an open OperationalException. De-duplicates on (exc_type, shipment_id, operation_case_id,
    deal_id) so a repeated signal doesn't spawn duplicates. Returns the exception."""
    now = now or datetime.utcnow()
    existing = session.exec(select(OperationalException).where(
        OperationalException.exc_type == exc_type,
        OperationalException.shipment_id == shipment_id,
        OperationalException.operation_case_id == operation_case_id,
        OperationalException.deal_id == deal_id,
        OperationalException.status.in_(OPEN_STATUSES))).first()
    if existing:
        return existing
    exc = OperationalException(exc_type=exc_type, severity=severity, operation_case_id=operation_case_id,
                              shipment_id=shipment_id, deal_id=deal_id, tenant_id=tenant_id,
                              owner_id=getattr(actor, "id", None), status="open", due_date=due_date,
                              internal_description=internal_description,
                              seller_safe_description=seller_safe_description,
                              created_by=getattr(actor, "id", None), created_at=now, updated_at=now)
    session.add(exc)
    session.flush()
    exc.reference = _ref(exc.id, now)
    session.add(exc)
    if shipment_id:
        sh = session.get(Shipment, shipment_id)
        if sh:
            sh.exception_state = "open"
            session.add(sh)
    audit(session, actor, "operational_exception", exc.id, "exception_raised",
          {"type": exc_type, "severity": severity}, tenant_id=tenant_id)
    if wq:
        _wq(session, exc)
    # a sensitive exception NEVER auto-publishes to a seller — it creates a DRAFT update for admin approval
    if tenant_id and severity in ("high", "critical"):
        try:
            from . import seller_progress as SP
            SP.draft_exception_update(session, exc, actor=actor)
        except Exception:  # noqa: BLE001
            pass
    return exc


def _wq(session, exc):
    try:
        from . import work_queue as WQ
        WQ.create_work_item_safe(
            session, tenant_id=exc.tenant_id, type="cargo_damage_shortage"
            if exc.exc_type in ("cargo_damage", "shortage", "failed_delivery") else "external_integration_failure"
            if exc.exc_type == "system_failure" else "operational_handoff_required",
            source="automatic", priority="high" if exc.severity in ("high", "critical") else "normal",
            title=f"Exception {exc.reference}: {exc.exc_type}",
            description=exc.internal_description or exc.exc_type,
            related_exception_id=exc.id, related_shipment_id=exc.shipment_id,
            related_operation_case_id=exc.operation_case_id, related_deal_id=exc.deal_id,
            idempotency_key=f"exception:{exc.id}", condition_version="open")
    except Exception:  # noqa: BLE001
        pass


def resolve_exception(session, exc: OperationalException, *, resolution="", status="resolved", actor=None,
                      now=None):
    now = now or datetime.utcnow()
    exc.status = status
    exc.resolution = (resolution or "")[:1000]
    exc.updated_at = now
    if status in ("resolved", "dismissed"):
        exc.resolved_at = now
        if exc.shipment_id:
            # clear the shipment flag only if no other open exception remains for it
            other = session.exec(select(OperationalException).where(
                OperationalException.shipment_id == exc.shipment_id,
                OperationalException.id != exc.id,
                OperationalException.status.in_(OPEN_STATUSES))).first()
            if not other:
                sh = session.get(Shipment, exc.shipment_id)
                if sh:
                    sh.exception_state = "none"
                    session.add(sh)
    session.add(exc)
    try:
        from . import work_queue as WQ
        WQ.resolve_by_key(session, f"exception:{exc.id}",
                          note=exc.resolution or "resolved", actor=actor)
    except Exception:  # noqa: BLE001
        pass
    audit(session, actor, "operational_exception", exc.id, "exception_resolved",
          {"status": status}, tenant_id=exc.tenant_id)
    return exc


def exception_seller_view(exc: OperationalException) -> dict:
    """The ONLY exception fields a seller may see — never the internal description, owner, or linked buyer/
    provider records."""
    return {"reference": exc.reference, "status": exc.status,
            "summary": exc.seller_safe_description or "An operational issue is being handled."}
