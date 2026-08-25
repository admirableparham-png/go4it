"""Phase 7 — trade documents: hardened private storage (validate/quarantine/sha256/path-traversal), scan-clear
+ seller-safe gating, and the sanitized seller document-request flow (buyer/provider PII never in instructions)."""
from sqlmodel import Session, select

from app import tradedocs as TD
from app.models import DocumentRequirement, SellerUpdate, ServiceRequest, TradeDocument, User, WorkItem


def _seller(s):
    return s.exec(select(User).where(User.email == "sellerA@t.local")).one()


def test_store_validates_and_quarantines(ops_engine, tmp_path):
    with Session(ops_engine) as s:
        owner = _seller(s)
        doc, err = TD.store_document(s, files_dir=tmp_path, data=b"%PDF-1.4 hello",
                                     original_filename="invoice.pdf", content_type="application/pdf",
                                     doc_type="commercial_invoice", tenant_id=owner.id); s.commit()
        assert err == "" and doc is not None
        assert doc.quarantine == "quarantined" and len(doc.sha256) == 64 and doc.size_bytes > 0
        assert (tmp_path / doc.file_path).exists()
        # on-disk name is doc-id prefixed, never the raw upload name at the root
        assert doc.file_path.endswith(f"{doc.id}_invoice.pdf")


def test_store_rejects_dangerous_file(ops_engine, tmp_path):
    with Session(ops_engine) as s:
        owner = _seller(s)
        doc, err = TD.store_document(s, files_dir=tmp_path, data=b"<script>", original_filename="x.html",
                                     content_type="text/html", doc_type="other", tenant_id=owner.id)
        assert doc is None and err                                  # dangerous ext rejected
        # a path-traversal filename is refused by the validator
        doc2, err2 = TD.store_document(s, files_dir=tmp_path, data=b"%PDF-", original_filename="../evil.pdf",
                                       content_type="application/pdf", doc_type="other", tenant_id=owner.id)
        assert doc2 is None and err2


def test_attest_then_seller_safe_gate(ops_engine, tmp_path):
    with Session(ops_engine) as s:
        owner = _seller(s)
        doc, _ = TD.store_document(s, files_dir=tmp_path, data=b"%PDF-1.4", original_filename="coo.pdf",
                                   content_type="application/pdf", doc_type="certificate_of_origin",
                                   tenant_id=owner.id); s.commit()
        ok, err = TD.mark_seller_safe(s, doc)                       # refused while quarantined
        assert ok is False and "quarantine" in err
        assert TD.publishable_to_seller(doc) is False              # quarantined → never publishable
        _d, aerr = TD.admin_attest(s, doc); s.commit()
        assert aerr == "" and doc.quarantine == "admin_attested"   # honest state — NOT "scanned"
        # the honest label never claims a malware scan
        label, detail = TD.scan_status(doc)
        assert label == "Admin-attested" and "scan" not in label.lower() and "NOT malware-scanned" in detail
        ok2, _ = TD.mark_seller_safe(s, doc); s.commit()           # attestation is enough when no AV configured
        assert ok2 and doc.seller_safe is True and TD.publishable_to_seller(doc) is True


def test_attested_is_never_labelled_scanned_or_malware_free(ops_engine, tmp_path):
    with Session(ops_engine) as s:
        owner = _seller(s)
        doc, _ = TD.store_document(s, files_dir=tmp_path, data=b"%PDF-1.4", original_filename="x.pdf",
                                   content_type="application/pdf", doc_type="other", tenant_id=owner.id)
        s.commit()
        TD.admin_attest(s, doc); s.commit()
        label, detail = TD.scan_status(doc)
        blob = (label + " " + detail).lower()
        assert "scanned" not in label.lower() and "malware-free" not in blob


def test_configured_scanner_gates_seller_release(ops_engine, tmp_path, monkeypatch):
    monkeypatch.setenv("OPS_MALWARE_SCANNER", "clamav")            # a real scanner is configured
    with Session(ops_engine) as s:
        owner = _seller(s)
        doc, _ = TD.store_document(s, files_dir=tmp_path, data=b"%PDF-1.4", original_filename="c.pdf",
                                   content_type="application/pdf", doc_type="commercial_invoice",
                                   tenant_id=owner.id); s.commit()
        TD.admin_attest(s, doc); s.commit()
        ok, err = TD.mark_seller_safe(s, doc)                       # attestation alone is NOT enough now
        assert ok is False and "scanned clean" in err
        TD.record_scan(s, doc, clean=True, actor=owner, provider="clamav"); s.commit()
        assert doc.quarantine == "scanned_clean"
        ok2, _ = TD.mark_seller_safe(s, doc); s.commit()
        assert ok2 and doc.seller_safe is True
        # an infected verdict blocks + force-unpublishes
        TD.record_scan(s, doc, clean=False, actor=owner); s.commit()
        assert doc.quarantine == "infected" and doc.seller_safe is False
        assert TD.publishable_to_seller(doc) is False


def test_requirement_and_upload_link(ops_engine, tmp_path):
    with Session(ops_engine) as s:
        owner = _seller(s)
        req = TD.create_requirement(s, doc_type="packing_list", required_from="seller", tenant_id=owner.id)
        s.commit()
        doc, _ = TD.store_document(s, files_dir=tmp_path, data=b"%PDF-1.4", original_filename="pl.pdf",
                                   content_type="application/pdf", doc_type="packing_list",
                                   requirement=req, tenant_id=owner.id); s.commit()
        s.refresh(req)
        assert req.document_id == doc.id and req.status == "received"
        assert s.exec(select(WorkItem).where(WorkItem.type == "uploaded_document_needs_review")).first()


def test_seller_document_request_refuses_pii(ops_engine):
    with Session(ops_engine) as s:
        owner = _seller(s)
        sr = ServiceRequest(tracking_code="SR-9", requester_id=owner.id, owner_id=owner.id,
                            request_type="docs", status="approved")
        s.add(sr); s.commit(); s.refresh(sr)
        req = TD.create_requirement(s, doc_type="certificate_of_origin", required_from="seller",
                                    request_id=sr.id, tenant_id=owner.id); s.commit()
        ok, err = TD.request_seller_document(s, req, instructions="Email the buyer at cfo@acme.com")
        assert ok is False and "email" in err.lower()              # PII in instructions → refused
        ok2, _ = TD.request_seller_document(s, req, instructions="Please upload your certificate of origin.")
        s.commit()
        assert ok2 and req.status == "requested" and req.seller_action_required
        su = s.exec(select(SellerUpdate).where(SellerUpdate.request_id == sr.id)).first()
        assert su is not None and su.seller_id == owner.id
        assert s.exec(select(WorkItem).where(WorkItem.type == "seller_document_required")).first()
