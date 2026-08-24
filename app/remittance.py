"""Phase 7 — remittance / Sarafi coordination.

A RemittanceCase is a COORDINATION + TRACKING record, not an executed transfer, unless a licensed provider
integration is configured. Go4it never claims a transfer occurred without verified evidence and never
auto-initiates a crypto/bank/hawala/third-country transfer. No wallet seeds / private keys / banking passwords
are stored; sensitive account identifiers are ENCRYPTED at rest (outreach.mail_encrypt) or kept as masked
references. FX uses the Phase-5 snapshot system and manual FX is never presented as live. Provider contacts +
account details are admin-only; a seller sees only safe status, amount/currency where authorized, and the
required next action. Compliance rejection reasons stay internal unless an approved safe explanation is published.
"""
import json
from datetime import datetime

from .models import RemittanceCase
from .outreach import mail_decrypt, mail_encrypt
from .pipeline import audit
from .pricing import _fx_for, _q, fx_state

STATUSES = ("requested", "information_required", "compliance_review", "quoted", "approved", "awaiting_funds",
            "processing", "paid", "confirmed", "rejected", "cancelled", "failed")
ROUTE_CATEGORIES = ("bank", "exchange_house", "lc", "third_country", "other")


def _ref(row_id, now=None):
    now = now or datetime.utcnow()
    return f"RM-{now:%Y%m}-{row_id:04d}"


def create_remittance(session, *, source_currency, dest_currency, source_amount="0",
                      route_method_category="", request_id=None, deal_id=None, operation_case_id=None,
                      tenant_id=None, provider_company_id=None, origin_country="", dest_country="",
                      payer_role="", payee_role="", actor=None, now=None):
    """Create a remittance case. Captures an FX snapshot (never live if the rate is manual/stale) when the
    currencies differ; expected destination amount is derived from that snapshot, never invented."""
    now = now or datetime.utcnow()
    rc = RemittanceCase(source_currency=source_currency.upper(), dest_currency=dest_currency.upper(),
                        source_amount=str(_q(source_amount)), route_method_category=route_method_category,
                        request_id=request_id, deal_id=deal_id, operation_case_id=operation_case_id,
                        tenant_id=tenant_id, provider_company_id=provider_company_id,
                        origin_country=origin_country, dest_country=dest_country, payer_role=payer_role,
                        payee_role=payee_role, status="requested", owner_id=getattr(actor, "id", None),
                        created_by=getattr(actor, "id", None), created_at=now, updated_at=now)
    if rc.source_currency and rc.dest_currency and rc.source_currency != rc.dest_currency:
        fx = _fx_for(session, rc.dest_currency, rc.source_currency)
        rc.fx_snapshot = json.dumps(fx)[:2000]
        if fx.get("rate"):
            rc.expected_dest_amount = str(_q(_q(source_amount) * (_q(fx["rate"]))))
    session.add(rc)
    session.flush()
    rc.reference = _ref(rc.id, now)
    session.add(rc)
    audit(session, actor, "remittance_case", rc.id, "remittance_created",
          {"route": route_method_category, "src": rc.source_currency, "dst": rc.dest_currency},
          tenant_id=tenant_id)
    return rc


def store_account_ref(session, rc: RemittanceCase, plaintext: str, *, actor=None):
    """Encrypt a sensitive account identifier at rest (never store plaintext). A blank clears it."""
    rc.account_ref_enc = mail_encrypt(plaintext) if (plaintext or "").strip() else ""
    session.add(rc)
    audit(session, actor, "remittance_case", rc.id, "account_ref_stored", {}, tenant_id=rc.tenant_id)
    return rc


def reveal_account_ref(rc: RemittanceCase) -> str:
    """Admin-only decrypt of the stored account identifier ('' if none/corrupt)."""
    return mail_decrypt(rc.account_ref_enc) if rc.account_ref_enc else ""


def fx_is_live(rc: RemittanceCase, now=None) -> bool:
    try:
        return fx_state(json.loads(rc.fx_snapshot or "{}"), now) == "live"
    except Exception:  # noqa: BLE001
        return False


def set_status(session, rc: RemittanceCase, to_status: str, *, reason="", compliance_reason="", actor=None,
               now=None):
    """Advance a remittance case. Returns (ok, error). A compliance rejection reason stays INTERNAL. Entering
    compliance_review raises a review task; delayed/failed raises an exception. Never marks paid/confirmed
    without an admin action (this call is that action)."""
    now = now or datetime.utcnow()
    if to_status not in STATUSES:
        return False, "unknown remittance status"
    frm = rc.status
    rc.status = to_status
    rc.updated_at = now
    if compliance_reason:
        rc.compliance_reason = compliance_reason[:1000]     # internal
    if to_status == "rejected":
        rc.compliance_status = "rejected"
    if to_status == "confirmed":
        rc.actual_completion = now
    if reason:
        rc.exception_reason = reason[:500]
    session.add(rc)
    audit(session, actor, "remittance_case", rc.id, "remittance_status_change",
          {"from": frm, "to": to_status}, tenant_id=rc.tenant_id)
    if to_status == "compliance_review":
        _wq(session, rc, "remittance_compliance_review", "compliance review required")
    elif to_status == "failed":
        _wq(session, rc, "remittance_delayed", "remittance failed")
    return True, ""


def _wq(session, rc, wtype, why):
    try:
        from . import work_queue as WQ
        WQ.create_work_item_safe(
            session, tenant_id=rc.tenant_id, type=wtype, source="automatic",
            title=f"Remittance {rc.reference}: {why}",
            description=f"{rc.source_currency}→{rc.dest_currency} {rc.route_method_category} — {why}.",
            related_operation_case_id=rc.operation_case_id, related_deal_id=rc.deal_id,
            idempotency_key=f"{wtype}:rm:{rc.id}", condition_version=rc.status)
    except Exception:  # noqa: BLE001
        pass


def remittance_seller_view(rc: RemittanceCase):
    """The ONLY remittance fields a seller may see: safe status, amount/currency, and the required next action.
    Never provider identity, account references, compliance reasons, or fees."""
    next_action = ""
    if rc.status == "information_required":
        next_action = "Please provide the requested information."
    elif rc.status in ("awaiting_funds",):
        next_action = "Awaiting funds."
    return {"reference": rc.reference, "status": rc.status, "amount": rc.source_amount,
            "currency": rc.source_currency, "next_action": next_action}
