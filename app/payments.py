"""Phase 7 — payment milestones + settlement.

Administrative payment tracking ONLY — Go4it is not a payment processor. A milestone is confirmed solely by an
authorized admin (manager+) WITH a reference or evidence document; never on the strength of an email or a
screenshot alone. Amounts are Decimal (via pricing._d/_q). Corrections are audited. Card numbers / banking
passwords / crypto keys / seeds are NEVER stored. A settlement is an IMMUTABLE per-currency snapshot; corrections
append a SettlementAdjustment rather than editing it. Sellers never see Go4it margin, buyer payments, or
unrelated costs — only their own authorized, masked figures.
"""
from datetime import datetime

from sqlmodel import select

from .models import Deal, PaymentMilestone, Settlement, SettlementAdjustment
from .pipeline import audit
from .pricing import _d, _q

MILESTONE_TYPES = ("buyer_deposit", "buyer_balance", "supplier_advance", "supplier_balance", "freight_payment",
                   "customs_payment", "refund", "other")
STATUSES = ("planned", "awaiting", "partially_received", "received", "payment_failed", "refunded", "cancelled",
            "disputed")


def _ref(prefix, row_id, now=None):
    now = now or datetime.utcnow()
    return f"{prefix}-{now:%Y%m}-{row_id:04d}"


def _authorized(actor) -> bool:
    """Confirming money movement requires an authorized admin (manager or admin)."""
    return getattr(actor, "role", "") in ("manager", "admin")


def mask_reference(ref: str) -> str:
    """A seller-safe masked reference — only the last 4 chars survive."""
    ref = (ref or "").strip()
    if len(ref) <= 4:
        return "••••" if ref else ""
    return "••••" + ref[-4:]


def create_milestone(session, *, milestone_type, currency="", expected_amount="0", deal_id=None,
                     operation_case_id=None, tenant_id=None, due_date=None, payer_role="", payee_role="",
                     seller_visible=False, actor=None, now=None):
    now = now or datetime.utcnow()
    pm = PaymentMilestone(milestone_type=milestone_type, currency=currency.upper(),
                          expected_amount=str(_q(expected_amount)), deal_id=deal_id,
                          operation_case_id=operation_case_id, tenant_id=tenant_id, due_date=due_date,
                          payer_role=payer_role, payee_role=payee_role, seller_visible=seller_visible,
                          status="planned", created_by=getattr(actor, "id", None), created_at=now,
                          updated_at=now)
    session.add(pm)
    session.flush()
    pm.reference = _ref("PM", pm.id, now)
    session.add(pm)
    audit(session, actor, "payment_milestone", pm.id, "milestone_created",
          {"type": milestone_type, "expected": pm.expected_amount, "currency": pm.currency},
          tenant_id=tenant_id)
    return pm


def confirm_payment(session, pm: PaymentMilestone, *, confirmed_amount, reference_code="",
                    evidence_document_id=None, actor=None, now=None):
    """Confirm receipt. Returns (ok, error). REQUIRES an authorized admin AND (a reference OR an evidence
    document) — never confirmed on an email/screenshot alone. Sets received vs partially_received by amount."""
    now = now or datetime.utcnow()
    if not _authorized(actor):
        return False, "only an authorized admin (manager+) may confirm a payment"
    if not (reference_code or "").strip() and not evidence_document_id:
        return False, "a payment reference or evidence document is required to confirm"
    amt = _q(confirmed_amount)
    if amt <= 0:
        return False, "confirmed amount must be positive"
    pm.confirmed_amount = str(amt)
    pm.confirmed_date = now
    pm.reference_code = (reference_code or "")[:120]
    pm.evidence_document_id = evidence_document_id
    pm.confirmed_by = getattr(actor, "id", None)
    pm.status = "received" if amt >= _d(pm.expected_amount) else "partially_received"
    pm.updated_at = now
    session.add(pm)
    audit(session, actor, "payment_milestone", pm.id, "payment_confirmed",
          {"amount": pm.confirmed_amount, "status": pm.status, "has_evidence": bool(evidence_document_id)},
          tenant_id=pm.tenant_id)
    return True, ""


def set_status(session, pm: PaymentMilestone, to_status: str, *, reason="", actor=None, now=None):
    """Non-confirmation status changes (awaiting/failed/refunded/cancelled/disputed). A correction to a
    previously confirmed amount is audited. 'received' must go through confirm_payment (evidence-gated)."""
    now = now or datetime.utcnow()
    if to_status not in STATUSES:
        return False, "unknown status"
    if to_status in ("received", "partially_received"):
        return False, "use confirm_payment (evidence required) to mark a payment received"
    frm = pm.status
    pm.status = to_status
    pm.updated_at = now
    session.add(pm)
    audit(session, actor, "payment_milestone", pm.id, "milestone_status_change",
          {"from": frm, "to": to_status, "reason": (reason or "")[:300]}, tenant_id=pm.tenant_id)
    if to_status in ("payment_failed",):
        _overdue_or_failed_task(session, pm, "payment_failed")
    return True, ""


def _overdue_or_failed_task(session, pm, kind):
    try:
        from . import work_queue as WQ
        WQ.create_work_item_safe(
            session, tenant_id=pm.tenant_id, type="payment_overdue" if kind == "overdue" else "payment_confirmation_required",
            source="automatic", priority="high",
            title=f"Payment {pm.reference}: {kind.replace('_', ' ')}",
            description=f"{pm.milestone_type} {pm.currency} {pm.expected_amount} — {kind}.",
            related_payment_id=pm.id, related_deal_id=pm.deal_id,
            idempotency_key=f"{kind}:pm:{pm.id}", condition_version=pm.status)
    except Exception:  # noqa: BLE001
        pass


def payment_seller_view(pm: PaymentMilestone):
    """A payment a seller may see ONLY when seller_visible. Never internal notes, margin, or the other party's
    figures; the reference is masked. Returns None if the milestone is not authorized for the seller."""
    if not pm.seller_visible:
        return None
    return {"reference": pm.reference, "type": pm.milestone_type, "currency": pm.currency,
            "amount": pm.expected_amount, "status": pm.status,
            "confirmed": pm.status in ("received", "partially_received"),
            "payment_reference": mask_reference(pm.reference_code)}


# --------------------------------------------------------------------- settlement (immutable snapshot)
def settlement_ready(session, deal: Deal):
    """(ready, blockers). A deal may settle only once its required financial milestones are complete: every
    buyer milestone received and no failed/disputed milestone outstanding."""
    blockers = []
    pays = session.exec(select(PaymentMilestone).where(PaymentMilestone.deal_id == deal.id)).all()
    buyer = [p for p in pays if p.milestone_type in ("buyer_deposit", "buyer_balance")]
    if not buyer:
        blockers.append("no buyer payment milestone")
    if any(p.status != "received" for p in buyer):
        blockers.append("buyer payments not fully received")
    if any(p.status in ("payment_failed", "disputed") for p in pays):
        blockers.append("a payment is failed/disputed")
    return (len(blockers) == 0), blockers


def record_settlement(session, deal: Deal, *, revenue="0", verified_costs="0", supplier_proceeds="0",
                      operational_costs="0", currency="", fx_snapshot="", actor=None, force=False,
                      now=None):
    """Create the IMMUTABLE settlement snapshot for a deal (single currency; no cross-currency sum without an
    explicit FX snapshot). Returns (settlement, error). Blocks unless settlement_ready (or force + authorized).
    Marks the deal settled and records realized margin on the deal (which sellers never see)."""
    now = now or datetime.utcnow()
    if not _authorized(actor):
        return None, "only an authorized admin (manager+) may record a settlement"
    existing = session.exec(select(Settlement).where(Settlement.deal_id == deal.id)).first()
    if existing:
        return existing, "a settlement already exists for this deal (corrections are adjustments)"
    ready, blockers = settlement_ready(session, deal)
    if not ready and not force:
        return None, "not ready to settle: " + ", ".join(blockers)
    rev, vc = _q(revenue), _q(verified_costs)
    margin = _q(rev - vc)
    st = Settlement(deal_id=deal.id, revenue=str(rev), verified_costs=str(vc),
                    supplier_proceeds=str(_q(supplier_proceeds)), operational_costs=str(_q(operational_costs)),
                    go4it_margin=str(margin), currency=currency.upper(), fx_snapshot=fx_snapshot,
                    outstanding="0", realized_margin=str(margin), settlement_date=now,
                    approved_by=getattr(actor, "id", None), created_at=now)
    session.add(st)
    deal.stage = "settled"
    deal.actual_revenue = float(rev)
    deal.actual_cost = float(vc)
    deal.realized_margin = float(margin)
    deal.closed_at = now
    deal.updated_at = now
    session.add(deal)
    session.flush()
    audit(session, actor, "deal", deal.id, "deal_settled",
          {"settlement_id": st.id, "currency": st.currency, "realized_margin": st.realized_margin},
          tenant_id=deal.owner_id)
    return st, ""


def add_adjustment(session, settlement: Settlement, *, field, delta, reason, currency="", actor=None,
                   now=None):
    """A controlled correction: append a signed adjustment (never edit the immutable snapshot). Requires a
    reason + matching currency (no cross-currency mixing without an explicit FX snapshot)."""
    now = now or datetime.utcnow()
    if not _authorized(actor):
        return None, "only an authorized admin (manager+) may adjust a settlement"
    if not (reason or "").strip():
        return None, "an adjustment reason is required"
    ccy = (currency or settlement.currency).upper()
    if ccy != settlement.currency and not settlement.fx_snapshot:
        return None, "cross-currency adjustment needs an explicit FX snapshot"
    adj = SettlementAdjustment(settlement_id=settlement.id, field=field, delta=str(_q(delta)), currency=ccy,
                               reason=reason[:500], approved_by=getattr(actor, "id", None), created_at=now)
    session.add(adj)
    session.flush()
    audit(session, actor, "settlement", settlement.id, "settlement_adjusted",
          {"field": field, "delta": adj.delta, "reason": adj.reason})
    return adj, ""


def settlement_seller_view(settlement: Settlement):
    """A settlement carries Go4it's margin + buyer payments — a seller sees NONE of it. Only the fact of
    settlement is safe."""
    return {"settled": True, "settlement_date": settlement.settlement_date.date().isoformat()
            if settlement.settlement_date else ""}
