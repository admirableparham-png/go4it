"""Phase 7 — OperationCase lifecycle: idempotent creation from a Deal / Request, and seller-safe projection."""
from sqlmodel import Session, select

from app import operations as OPS
from app.models import Deal, Lead, OperationCase, ServiceRequest, User


def _seller(s):
    return s.exec(select(User).where(User.email == "sellerA@t.local")).one()


def _deal(s, owner):
    lead = Lead(product="rebar", tracking_code="G4-1", dest_country="GE", buyer_company="ACME", owner_id=owner.id)
    s.add(lead); s.commit(); s.refresh(lead)
    d = Deal(lead_id=lead.id, owner_id=owner.id, stage="won", tracking_code="G4-1-D")
    s.add(d); s.commit(); s.refresh(d)
    return d


def test_case_for_deal_is_idempotent(ops_engine):
    with Session(ops_engine) as s:
        owner = _seller(s)
        d = _deal(s, owner)
        c1, made1 = OPS.ensure_case_for_deal(s, d, actor=None); s.commit()
        c2, made2 = OPS.ensure_case_for_deal(s, d, actor=None); s.commit()
        assert made1 is True and made2 is False        # second call reuses, never duplicates
        assert c1.id == c2.id
        assert c1.reference.startswith("OP-")
        assert c1.tenant_id == owner.id and c1.case_type == "deal"
        assert len(s.exec(select(OperationCase).where(OperationCase.deal_id == d.id)).all()) == 1


def test_case_for_request_is_idempotent(ops_engine):
    with Session(ops_engine) as s:
        owner = _seller(s)
        req = ServiceRequest(tracking_code="SR-1", requester_id=owner.id, owner_id=owner.id,
                             request_type="freight", status="approved")
        s.add(req); s.commit(); s.refresh(req)
        c1, made1 = OPS.ensure_case_for_request(s, req, actor=None); s.commit()
        c2, made2 = OPS.ensure_case_for_request(s, req, actor=None); s.commit()
        assert made1 and not made2 and c1.id == c2.id
        assert c1.case_type == "request" and c1.request_id == req.id
        assert c1.category == "freight"


def test_standalone_case_and_seller_view(ops_engine):
    with Session(ops_engine) as s:
        owner = _seller(s)
        c = OPS.create_standalone_case(s, tenant_id=owner.id, category="docs", origin_country="IR",
                                       dest_country="GE", notes="internal only note"); s.commit()
        assert c.case_type == "standalone" and c.reference.startswith("OP-")
        view = OPS.case_seller_view(c)
        assert view == {"reference": c.reference, "status": "open", "origin_country": "IR",
                        "dest_country": "GE", "category": "docs"}
        assert "notes" not in view and "owner_id" not in view      # internal fields never projected
