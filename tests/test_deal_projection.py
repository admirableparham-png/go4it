"""Phase 7 — the CENTRAL Deal-stage projector: advances DEAL_STAGES from VERIFIED operational milestones only,
idempotently, monotonically (never backward), doc-gated, with audited override + controlled correction. The
existing DEAL_STAGES vocabulary is unchanged."""
from datetime import datetime

from sqlmodel import Session, select

from app import operations as OPS
from app import shipments as SH
from app.deal_service import DEAL_STAGES
from app.models import AuditLog, ComplianceDoc, Deal, Lead, PaymentMilestone, Shipment, User, WorkItem


def _seller(s):
    return s.exec(select(User).where(User.email == "sellerA@t.local")).one()


def _deal(s, owner, stage="won"):
    lead = Lead(product="rebar", tracking_code="G4-9", dest_country="GE", buyer_company="ACME", owner_id=owner.id)
    s.add(lead); s.commit(); s.refresh(lead)
    d = Deal(lead_id=lead.id, owner_id=owner.id, stage=stage, tracking_code="G4-9-D")
    s.add(d); s.commit(); s.refresh(d)
    return d


def _verify_docs(s, deal, *types):
    for t in types:
        s.add(ComplianceDoc(deal_id=deal.id, doc_type=t, status="verified"))
    s.commit()


def test_stages_unchanged():
    assert DEAL_STAGES == ["won", "supplier_confirmed", "payment_received", "freight_booked",
                           "export_cleared", "in_transit", "import_cleared", "delivered", "settled", "closed"]


def test_projects_forward_through_verified_milestones(ops_engine):
    with Session(ops_engine) as s:
        owner = _seller(s)
        d = _deal(s, owner)
        # a booked shipment implies supplier_confirmed + freight_booked; a received buyer payment → payment_received
        sh = SH.book_shipment(s, deal_id=d.id, tenant_id=owner.id, mode="sea", booking_reference="BK"); s.commit()
        s.add(PaymentMilestone(deal_id=d.id, milestone_type="buyer_deposit", status="received",
                               expected_amount="1000", currency="USD")); s.commit()
        final, hops = OPS.project_deal_stage(s, d); s.commit()
        assert d.stage == "freight_booked"
        assert hops == ["supplier_confirmed", "payment_received", "freight_booked"]


def test_projection_is_idempotent(ops_engine):
    with Session(ops_engine) as s:
        owner = _seller(s)
        d = _deal(s, owner)
        SH.book_shipment(s, deal_id=d.id, tenant_id=owner.id, mode="sea", booking_reference="BK"); s.commit()
        OPS.project_deal_stage(s, d); s.commit()
        stage_after_first = d.stage
        _final, hops2 = OPS.project_deal_stage(s, d); s.commit()
        assert hops2 == [] and d.stage == stage_after_first        # no further movement, no churn


def test_doc_gate_blocks_export_cleared(ops_engine):
    with Session(ops_engine) as s:
        owner = _seller(s)
        d = _deal(s, owner, stage="freight_booked")
        # export clearance evidence exists, but the required CoO + invoice are NOT verified → gate holds
        from app.models import CustomsCase
        s.add(CustomsCase(deal_id=d.id, side="export", status="cleared")); s.commit()
        OPS.project_deal_stage(s, d); s.commit()
        assert d.stage == "freight_booked"                          # blocked by the compliance gate
        assert s.exec(select(WorkItem).where(WorkItem.type == "customs_document_missing")).first() is not None
        # once the docs are verified the projector advances
        _verify_docs(s, d, "certificate_of_origin", "commercial_invoice")
        OPS.project_deal_stage(s, d); s.commit()
        assert d.stage == "export_cleared"


def test_never_moves_backward_when_evidence_disappears(ops_engine):
    with Session(ops_engine) as s:
        owner = _seller(s)
        d = _deal(s, owner)
        sh = SH.book_shipment(s, deal_id=d.id, tenant_id=owner.id, mode="sea", booking_reference="BK"); s.commit()
        OPS.project_deal_stage(s, d); s.commit()
        assert d.stage == "freight_booked"
        # delete the shipment (evidence gone) — a re-projection must NOT regress the stage
        s.delete(sh); s.commit()
        OPS.project_deal_stage(s, d); s.commit()
        assert d.stage == "freight_booked"


def test_override_is_forward_only_and_audited(ops_engine):
    with Session(ops_engine) as s:
        owner = _seller(s)
        d = _deal(s, owner)
        ok, err = OPS.override_deal_stage(s, d, "payment_received", reason="")     # no reason
        assert ok is False and "reason" in err
        ok2, _ = OPS.override_deal_stage(s, d, "payment_received", reason="wire confirmed by bank"); s.commit()
        assert ok2 and d.stage == "payment_received"
        bad, err2 = OPS.override_deal_stage(s, d, "won", reason="x")               # backward via override → refused
        assert bad is False and "forward-only" in err2
        assert s.exec(select(AuditLog).where(AuditLog.action == "deal_stage_override")).first() is not None


def test_correction_can_move_backward_with_audit(ops_engine):
    with Session(ops_engine) as s:
        owner = _seller(s)
        d = _deal(s, owner, stage="in_transit")
        ok, err = OPS.correct_deal_stage(s, d, "freight_booked", reason="")        # needs a reason
        assert ok is False
        ok2, _ = OPS.correct_deal_stage(s, d, "freight_booked", reason="mis-recorded departure"); s.commit()
        assert ok2 and d.stage == "freight_booked"
        assert s.exec(select(AuditLog).where(AuditLog.action == "deal_stage_correction")).first() is not None
