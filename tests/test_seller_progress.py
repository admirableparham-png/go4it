"""Phase 7 — seller-safe operational progress + automatic updates: sanitized allowlisted templates, idempotent
per (deal, stage), sensitive exceptions drafted (not auto-published), and NO buyer/provider/cost/margin leakage."""
from sqlmodel import Session, select

from app import ops_exceptions as EXC
from app import operations as OPS
from app import seller_progress as SP
from app import shipments as SH
from app.models import (Deal, DocumentRequirement, Lead, OperationalException, PaymentMilestone, SellerUpdate,
                        ServiceRequest, Shipment, User)


def _seller(s):
    return s.exec(select(User).where(User.email == "sellerA@t.local")).one()


def _deal(s, owner, stage="won"):
    req = ServiceRequest(tracking_code="SR-SP", requester_id=owner.id, owner_id=owner.id,
                         request_type="buyer_hunt", status="running")
    s.add(req); s.commit(); s.refresh(req)
    lead = Lead(product="Zinc Sulphate", tracking_code="G4-SP", dest_country="GE",
                buyer_company="ACME Baghdad", owner_id=owner.id, request_id=req.id)
    s.add(lead); s.commit(); s.refresh(lead)
    d = Deal(lead_id=lead.id, owner_id=owner.id, stage=stage, tracking_code="G4-SP-D")
    s.add(d); s.commit(); s.refresh(d)
    return d


def test_stage_change_publishes_sanitized_update_idempotently(ops_engine):
    with Session(ops_engine) as s:
        owner = _seller(s)
        d = _deal(s, owner)
        su1 = SP.on_deal_stage_change(s, d, "won", "freight_booked"); s.commit()
        su2 = SP.on_deal_stage_change(s, d, "won", "freight_booked"); s.commit()   # same stage again
        assert su1 is not None and su2.id == su1.id                # idempotent per (deal, stage)
        ups = s.exec(select(SellerUpdate).where(SellerUpdate.seller_id == owner.id)).all()
        assert len(ups) == 1 and ups[0].published is True
        assert ups[0].public_status == "Freight booked"


def test_projector_emits_updates(ops_engine):
    with Session(ops_engine) as s:
        owner = _seller(s)
        d = _deal(s, owner)
        SH.book_shipment(s, deal_id=d.id, tenant_id=owner.id, mode="sea", booking_reference="BK"); s.commit()
        OPS.project_deal_stage(s, d, actor=None); s.commit()
        # supplier_confirmed + freight_booked reached → two allowlisted updates
        labels = {u.public_status for u in s.exec(select(SellerUpdate)).all()}
        assert "Freight booked" in labels and "Supply confirmed" in labels


def test_sensitive_exception_is_drafted_not_published(ops_engine):
    with Session(ops_engine) as s:
        owner = _seller(s)
        d = _deal(s, owner)
        EXC.raise_exception(s, exc_type="cargo_damage", severity="high", tenant_id=owner.id, deal_id=d.id,
                            internal_description="pallets crushed at Poti",
                            seller_safe_description="A delivery issue is being resolved."); s.commit()
        drafts = s.exec(select(SellerUpdate).where(SellerUpdate.published == False)).all()  # noqa: E712
        assert len(drafts) == 1 and drafts[0].summary == "A delivery issue is being resolved."


def test_seller_progress_leaks_nothing(ops_engine):
    with Session(ops_engine) as s:
        owner = _seller(s)
        d = _deal(s, owner, stage="in_transit")
        SH.book_shipment(s, deal_id=d.id, tenant_id=owner.id, mode="sea", origin="Bandar Abbas, IR",
                         destination="Poti, GE", booking_reference="BK-SECRET-9",
                         carrier_name_cache="Maersk Confidential"); s.commit()
        # an admin-only supplier payment + a seller-visible one
        s.add(PaymentMilestone(deal_id=d.id, milestone_type="supplier_advance", currency="USD",
                               expected_amount="9999", reference_code="INTERNAL", seller_visible=False)); s.commit()
        prog = SP.deal_seller_progress(s, d)
        blob = str(prog).lower()
        for secret in ["bk-secret", "maersk", "acme baghdad", "9999", "internal", "margin"]:
            assert secret not in blob, secret
        assert prog["stage"] == "In transit" and "Freight booked" in prog["completed_milestones"]
