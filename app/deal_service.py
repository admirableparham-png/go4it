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
