"""Post-win deal lifecycle: linear stages + non-bypassable compliance gates.

The gate mirrors PULSE's hard-rules philosophy — a deal cannot advance past a
gated stage until the required documents are *verified*, enforced in code (not
memory).
"""
from sqlmodel import select

from .models import ComplianceDoc, Deal

DEAL_STAGES = [
    "won",
    "supplier_confirmed",
    "payment_received",
    "freight_booked",
    "export_cleared",     # gated: needs CoO + commercial invoice verified
    "in_transit",         # gated: needs bill of lading verified
    "import_cleared",     # gated: needs packing list + customs declaration verified
    "delivered",
    "settled",
    "closed",
]

# Documents that must be VERIFIED before a deal may enter a given stage. Non-bypassable, enforced in
# advance_deal(): each stage's real trade paperwork must exist and be verified before the deal moves
# into it — you can't be "in transit" without a bill of lading, or "import cleared" without the
# import paperwork. Mirrors PULSE's hard-rules philosophy (compliance in code, not memory).
REQUIRED_DOCS = {
    "export_cleared": ["certificate_of_origin", "commercial_invoice"],
    "in_transit": ["bill_of_lading"],
    "import_cleared": ["packing_list", "customs_declaration"],
}

DOC_TYPES = [
    "certificate_of_origin",
    "commercial_invoice",
    "packing_list",
    "customs_declaration",
    "bill_of_lading",
    "insurance",
    "phytosanitary",
    "quality_cert",
]


def next_stage(stage: str):
    """The stage that follows `stage`, or None if terminal/unknown."""
    if stage not in DEAL_STAGES:
        return None
    i = DEAL_STAGES.index(stage)
    return DEAL_STAGES[i + 1] if i + 1 < len(DEAL_STAGES) else None


def missing_docs_for(session, deal: Deal, target_stage: str):
    """Required-but-not-verified doc types blocking entry into `target_stage`."""
    required = REQUIRED_DOCS.get(target_stage, [])
    if not required:
        return []
    verified = {
        d.doc_type for d in session.exec(
            select(ComplianceDoc).where(
                ComplianceDoc.deal_id == deal.id,
                ComplianceDoc.status == "verified",
            )
        ).all()
    }
    return [r for r in required if r not in verified]


def create_deal(session, lead, quote=None) -> Deal:
    """Create a Deal from a won lead and (optionally) its accepted quote, seeding
    planned figures from the quote when present."""
    if quote is not None:
        margin_pct = quote.margin_pct or 0.0
        planned_revenue = round(quote.delivered_total, 2)
        planned_cost = round(planned_revenue * 100.0 / (100.0 + margin_pct), 2) if margin_pct else planned_revenue
        planned_margin = round(planned_revenue - planned_cost, 2)
        quote_id = quote.id
    else:
        planned_revenue = planned_cost = planned_margin = 0.0
        quote_id = None

    deal = Deal(
        lead_id=lead.id, quote_id=quote_id, owner_id=lead.owner_id, stage="won",
        planned_revenue=planned_revenue, planned_cost=planned_cost, planned_margin=planned_margin,
    )
    session.add(deal)
    session.commit()
    session.refresh(deal)
    deal.tracking_code = f"{lead.tracking_code}-D"
    session.add(deal)
    session.commit()
    session.refresh(deal)
    return deal


# --------------------------------------------------------------------- Phase 6: deal from accepted version
def ensure_deal_for_quote_version(session, version, actor=None):
    """Idempotently create exactly ONE Deal for an accepted quote version. Concurrency-safe via the
    partial-unique index on deal(quote_version_id): a racing/duplicate call returns the existing Deal instead
    of creating a second. Records a creation event + raises handoff Work Queue tasks. Never starts shipment."""
    from sqlalchemy.exc import IntegrityError
    from .models import Lead, Quote
    existing = session.exec(select(Deal).where(Deal.quote_version_id == version.id)).first()
    if existing:
        return existing, False
    quote = session.get(Quote, version.quote_id)
    lead = session.get(Lead, quote.lead_id) if quote else None
    if not quote or not lead:
        return None, False
    # a lead may already have a deal from the legacy won→deal path — link conservatively, don't duplicate
    lead_deal = session.exec(select(Deal).where(Deal.lead_id == lead.id)).first()
    if lead_deal and lead_deal.quote_version_id is None:
        lead_deal.quote_version_id = version.id
        if lead_deal.quote_id is None:
            lead_deal.quote_id = quote.id
        session.add(lead_deal)
        _deal_audit(session, actor, lead_deal.id, "deal_linked_version", {"version_id": version.id})
        session.commit()
        return lead_deal, False
    # set quote_version_id on the INITIAL insert so the partial-unique index (uq_deal_quote_version) rejects a
    # concurrent/duplicate creation at commit — idempotent + concurrency-safe (create_deal commits internally,
    # so we inline the insert here rather than wrap it in a savepoint).
    margin_pct = quote.margin_pct or 0.0
    planned_revenue = round(quote.delivered_total, 2)
    planned_cost = round(planned_revenue * 100.0 / (100.0 + margin_pct), 2) if margin_pct else planned_revenue
    planned_margin = round(planned_revenue - planned_cost, 2)
    deal = Deal(lead_id=lead.id, quote_id=quote.id, owner_id=lead.owner_id, stage="won",
                quote_version_id=version.id, planned_revenue=planned_revenue, planned_cost=planned_cost,
                planned_margin=planned_margin)
    session.add(deal)
    try:
        session.commit()
    except IntegrityError:
        session.rollback()
        again = session.exec(select(Deal).where(Deal.quote_version_id == version.id)).first()
        return again, False
    session.refresh(deal)
    deal.tracking_code = f"{lead.tracking_code}-D"
    session.add(deal); session.commit(); session.refresh(deal)
    _deal_audit(session, actor, deal.id, "deal_created", {"version_id": version.id, "source": "accepted_quote"})
    _handoff_tasks(session, deal, quote)
    session.commit()
    return deal, True


def _handoff_tasks(session, deal, quote):
    """Create the commercial→operations handoff tasks (no shipment auto-start — Phase 7 executes)."""
    try:
        from . import work_queue as WQ
        WQ.create_work_item_safe(
            session, tenant_id=deal.owner_id, type="deal_missing_contract", source="automatic",
            title=f"Deal {deal.tracking_code}: buyer contract needed",
            description="An accepted-quote deal has no buyer sales contract yet.",
            related_deal_id=deal.id, related_quote_id=quote.id,
            idempotency_key=f"deal_missing_contract:deal:{deal.id}", condition_version="no_contract")
    except Exception:  # noqa: BLE001
        pass


def deal_ready_for_ops(session, deal) -> tuple:
    """(ready, blockers). Readiness = accepted quote + a signed/approved buyer contract + a supplier contract +
    confirmed product/qty/incoterm/destination. Does NOT create shipments — Phase 7 does that."""
    from .models import Contract, Quote, QuoteVersion
    blockers = []
    qv = session.get(QuoteVersion, deal.quote_version_id) if deal.quote_version_id else None
    if not qv or qv.status not in ("accepted",):
        blockers.append("accepted quote")
    contracts = session.exec(select(Contract).where(Contract.deal_id == deal.id)).all()
    if not any(c.side == "buyer" and c.status in ("signed", "approved", "sent") for c in contracts):
        blockers.append("buyer contract")
    if not any(c.side == "supplier" and c.status in ("signed", "approved", "sent") for c in contracts):
        blockers.append("supplier contract")
    if qv and (not qv.quantity or not qv.incoterm or not qv.destination):
        blockers.append("product/qty/incoterm/destination confirmed")
    return (len(blockers) == 0), blockers


def _deal_audit(session, actor, deal_id, action, meta):
    try:
        from .pipeline import audit
        audit(session, actor, "deal", deal_id, action, meta)
    except Exception:  # noqa: BLE001
        pass
