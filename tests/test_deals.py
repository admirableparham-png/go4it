"""Tests for the deal lifecycle: stage order, compliance gate, planned margin."""
from app.deal_service import (DEAL_STAGES, REQUIRED_DOCS, create_deal,
                              missing_docs_for, next_stage)
from app.models import ComplianceDoc, Deal, Lead, Quote


class _FakeSession:
    """Minimal stand-in so missing_docs_for can be unit-tested without a real DB.
    It only needs to return the verified ComplianceDocs for the deal."""
    def __init__(self, docs):
        self._docs = docs

    def exec(self, *_):
        return self

    def all(self):
        return [d for d in self._docs if d.status == "verified"]


def test_stage_order_and_next():
    assert next_stage("won") == "supplier_confirmed"
    assert next_stage("delivered") == "settled"
    assert next_stage("closed") is None          # terminal
    assert DEAL_STAGES[0] == "won" and DEAL_STAGES[-1] == "closed"


def test_export_gate_requires_both_docs():
    deal = Deal(id=1, lead_id=1)
    # only the certificate of origin is verified
    docs = [ComplianceDoc(deal_id=1, doc_type="certificate_of_origin", status="verified"),
            ComplianceDoc(deal_id=1, doc_type="commercial_invoice", status="received")]
    missing = missing_docs_for(_FakeSession(docs), deal, "export_cleared")
    assert missing == ["commercial_invoice"]     # still blocked


def test_export_gate_clears_when_all_verified():
    deal = Deal(id=1, lead_id=1)
    docs = [ComplianceDoc(deal_id=1, doc_type="certificate_of_origin", status="verified"),
            ComplianceDoc(deal_id=1, doc_type="commercial_invoice", status="verified")]
    assert missing_docs_for(_FakeSession(docs), deal, "export_cleared") == []


def test_ungated_stage_has_no_requirements():
    assert missing_docs_for(_FakeSession([]), Deal(id=1, lead_id=1), "in_transit") == []
    assert "export_cleared" in REQUIRED_DOCS


def test_planned_margin_backs_out_from_delivered_total():
    # delivered 108 at 8% margin -> cost 100, margin 8
    quote = Quote(id=1, lead_id=1, product_id=1, delivered_total=108.0, margin_pct=8.0)

    class _S:
        def add(self, *_): pass
        def commit(self): pass
        def refresh(self, *_): pass
        def exec(self, *_): return self
        def all(self): return []
    lead = Lead(id=1, product="x", tracking_code="G4-1", owner_id=None)
    deal = create_deal(_S(), lead, quote)
    assert deal.planned_revenue == 108.0
    assert deal.planned_cost == 100.0
    assert deal.planned_margin == 8.0
