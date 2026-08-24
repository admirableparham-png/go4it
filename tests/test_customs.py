"""Phase 7 — customs coordination: explicit status transitions (clearance never inferred), holds raise an
exception, documents_required raises a task, and the seller view hides broker/declaration detail."""
from sqlmodel import Session, select

from app import customs as CU
from app.models import CustomsCase, OperationalException, User, WorkItem


def _seller(s):
    return s.exec(select(User).where(User.email == "sellerA@t.local")).one()


def test_create_and_clear(ops_engine):
    with Session(ops_engine) as s:
        owner = _seller(s)
        cc = CU.create_customs_case(s, side="export", country="IR", tenant_id=owner.id,
                                    declaration_reference="EXP-123"); s.commit()
        assert cc.reference.startswith("CU-") and cc.status == "not_started"
        ok, _ = CU.set_customs_status(s, cc, "submitted"); s.commit()
        assert ok and cc.submitted_date is not None
        ok2, _ = CU.set_customs_status(s, cc, "cleared"); s.commit()
        assert ok2 and cc.status == "cleared" and cc.clearance_date is not None


def test_hold_raises_exception(ops_engine):
    with Session(ops_engine) as s:
        owner = _seller(s)
        cc = CU.create_customs_case(s, side="import", country="GE", tenant_id=owner.id); s.commit()
        CU.set_customs_status(s, cc, "query_hold", reason="HS code mismatch"); s.commit()
        exc = s.exec(select(OperationalException).where(OperationalException.exc_type == "customs_hold")).first()
        assert exc is not None and cc.hold_reason == "HS code mismatch"


def test_documents_required_raises_task(ops_engine):
    with Session(ops_engine) as s:
        owner = _seller(s)
        cc = CU.create_customs_case(s, side="export", country="IR", tenant_id=owner.id); s.commit()
        CU.set_customs_status(s, cc, "documents_required"); s.commit()
        wi = s.exec(select(WorkItem).where(WorkItem.type == "customs_document_missing")).first()
        assert wi is not None


def test_seller_view_hides_broker_and_declaration(ops_engine):
    with Session(ops_engine) as s:
        owner = _seller(s)
        cc = CU.create_customs_case(s, side="export", country="IR", tenant_id=owner.id,
                                    declaration_reference="SECRET-DECL", broker_company_id=42); s.commit()
        view = CU.customs_seller_view(cc)
        blob = str(view).lower()
        assert "secret-decl" not in blob and "42" not in blob
        assert view["side"] == "export" and view["cleared"] is False
