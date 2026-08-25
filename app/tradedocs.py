"""Phase 7 — trade documents: requirements + hardened private storage + seller document requests.

TradeDocuments reuse the hardened private-storage pattern (validate via attachments._secure_validate, generated
on-disk filename, sha256, quarantine-by-default, archive-not-delete, admin-download-gated). A seller-uploaded
document is linked to the seller's own request/op, quarantined, and can never become buyer-facing automatically.
An admin document reaches a seller ONLY after being marked seller_safe and published through the owner-scoped
RequestDeliverable mechanism. Buyer identity/documents never leak to a seller.

Malware status is HONEST: a quarantined document is released either by admin ATTESTATION (human review — never
labelled "scanned"/"malware-free") or by a CONFIGURED anti-virus scanner (scanned_clean/infected). Quarantined
or infected documents are never publishable or reused, and when a real scanner is configured, seller-facing
release requires a clean scan, not attestation alone.
"""
import os
import re
from datetime import datetime

from . import attachments as ATT
from .models import DocumentRequirement, ServiceRequest, TradeDocument
from .pipeline import audit, sanitize_scan

DOC_TYPES = ("commercial_invoice", "packing_list", "certificate_of_origin", "inspection_certificate",
             "insurance_certificate", "export_declaration", "import_declaration", "bill_of_lading",
             "airway_bill", "cmr", "customs_document", "proof_of_delivery", "other")

# --- malware / quarantine status — HONEST by construction -----------------------------------------
# A document is 'quarantined' on upload. It leaves quarantine one of two ways, which are NEVER conflated:
#   * 'admin_attested' — a human admin visually reviewed it. This is NOT a malware scan and must never be
#     displayed as "scanned" or "malware-free".
#   * 'scanned_clean' / 'infected' — the verdict of a CONFIGURED anti-virus scanner (record_scan()).
# 'quarantined' and 'infected' documents can never be marked seller-safe, published, or reused.
QUARANTINE_DEFAULT = "quarantined"
RELEASED_STATES = ("admin_attested", "scanned_clean")   # out of quarantine (still not necessarily seller-safe)

_SCAN_STATUS = {
    "quarantined": ("Quarantined", "Not yet reviewed — quarantined."),
    "admin_attested": ("Admin-attested", "Reviewed by an admin. NOT malware-scanned (no AV configured)."),
    "scanned_clean": ("Scanned clean", "Cleared by the configured malware scanner."),
    "infected": ("Infected", "Flagged by the malware scanner — blocked."),
}


def av_configured() -> bool:
    """True only when a real anti-virus scanner is wired in (env OPS_MALWARE_SCANNER). Until then documents are
    admin-attested only, never claimed as scanned."""
    return bool(os.environ.get("OPS_MALWARE_SCANNER", "").strip())


def scan_status(doc: TradeDocument):
    """(label, detail) — an HONEST status. Admin attestation is never labelled as a malware scan."""
    return _SCAN_STATUS.get(doc.quarantine, ("Unknown", ""))


def is_released(doc: TradeDocument) -> bool:
    return doc.quarantine in RELEASED_STATES


def publishable_to_seller(doc: TradeDocument) -> bool:
    """A document may reach a seller ONLY if it is active, released from quarantine, and admin-confirmed
    seller-safe. A quarantined/infected document is never publishable or reusable."""
    return bool(doc.seller_safe and doc.status == "active" and is_released(doc))


def _safe_name(name: str) -> str:
    """A conservative on-disk basename: strip directories, keep [A-Za-z0-9._-], cap length. The stored name is
    always prefixed with the document id so it is unguessable and collision-free."""
    base = os.path.basename(name or "file")
    base = re.sub(r"[^A-Za-z0-9._-]", "_", base).lstrip(".") or "file"
    return base[:120]


def create_requirement(session, *, doc_type, required_from="seller", operation_case_id=None, deal_id=None,
                       shipment_id=None, request_id=None, tenant_id=None, due_date=None, actor=None,
                       notes="", now=None):
    now = now or datetime.utcnow()
    req = DocumentRequirement(doc_type=doc_type, required_from=required_from,
                              operation_case_id=operation_case_id, deal_id=deal_id, shipment_id=shipment_id,
                              request_id=request_id, tenant_id=tenant_id, due_date=due_date, notes=notes,
                              seller_action_required=(required_from == "seller"),
                              buyer_action_required=(required_from == "buyer"), created_at=now)
    session.add(req)
    session.flush()
    audit(session, actor, "document_requirement", req.id, "requirement_created",
          {"doc_type": doc_type, "required_from": required_from}, tenant_id=tenant_id)
    return req


def store_document(session, *, files_dir, data: bytes, original_filename, content_type, doc_type,
                   uploaded_by_role="admin", requirement=None, operation_case_id=None, shipment_id=None,
                   deal_id=None, tenant_id=None, actor=None, now=None):
    """Validate + store a private trade document. Returns (doc, error). Quarantined by default; sha256-hashed;
    on-disk name is `<opcase-or-x>/<docid>_<safe>`; path stays under files_dir. Never overwrites."""
    now = now or datetime.utcnow()
    ok, reason = ATT._secure_validate(original_filename, content_type, len(data or b""))
    if not ok:
        return None, reason
    doc = TradeDocument(operation_case_id=operation_case_id or (requirement.operation_case_id if requirement else None),
                        shipment_id=shipment_id or (requirement.shipment_id if requirement else None),
                        requirement_id=requirement.id if requirement else None,
                        deal_id=deal_id or (requirement.deal_id if requirement else None),
                        tenant_id=tenant_id or (requirement.tenant_id if requirement else None),
                        doc_type=doc_type, kind="upload", uploaded_by_role=uploaded_by_role,
                        original_filename=original_filename[:255], content_type=content_type,
                        size_bytes=len(data or b""), uploaded_by=getattr(actor, "id", None), created_at=now)
    session.add(doc)
    session.flush()
    import hashlib
    from pathlib import Path
    root = Path(files_dir)
    sub = str(doc.operation_case_id or "x")
    folder = root / sub
    folder.mkdir(parents=True, exist_ok=True)
    fname = f"{doc.id}_{_safe_name(original_filename)}"
    path = (folder / fname).resolve()
    # defence-in-depth: the resolved path must stay under files_dir
    if not str(path).startswith(str(root.resolve()) + os.sep):
        session.rollback()
        return None, "unsafe path"
    path.write_bytes(data or b"")
    doc.file_path = f"{sub}/{fname}"
    doc.sha256 = hashlib.sha256(data or b"").hexdigest()
    session.add(doc)
    if requirement is not None:
        requirement.document_id = doc.id
        requirement.status = "received"
        session.add(requirement)
        _uploaded_review_task(session, doc, requirement)
    audit(session, actor, "trade_document", doc.id, "document_stored",
          {"doc_type": doc_type, "role": uploaded_by_role, "sha256": doc.sha256}, tenant_id=doc.tenant_id)
    return doc, ""


def _uploaded_review_task(session, doc, requirement):
    try:
        from . import work_queue as WQ
        WQ.create_work_item_safe(
            session, tenant_id=doc.tenant_id, type="uploaded_document_needs_review", source="automatic",
            title=f"Uploaded {doc.doc_type} needs review/scan",
            description="A trade document was uploaded — quarantined until reviewed + scan-cleared.",
            related_operation_case_id=doc.operation_case_id, related_deal_id=doc.deal_id,
            idempotency_key=f"uploaded_document_needs_review:doc:{doc.id}", condition_version="quarantined")
    except Exception:  # noqa: BLE001
        pass


def admin_attest(session, doc: TradeDocument, *, actor=None):
    """A human admin attests they reviewed the document. This releases it from quarantine but is NOT a malware
    scan — it is recorded and displayed as 'admin_attested', never as 'scanned' or 'malware-free'. An infected
    document (if a scanner ran) cannot be attested away."""
    if doc.quarantine == "infected":
        return None, "document was flagged infected by the scanner — it cannot be attested"
    doc.quarantine = "admin_attested"
    session.add(doc)
    try:
        from . import work_queue as WQ
        WQ.resolve_by_key(session, f"uploaded_document_needs_review:doc:{doc.id}", note="admin-attested",
                          actor=actor)
    except Exception:  # noqa: BLE001
        pass
    audit(session, actor, "trade_document", doc.id, "document_admin_attested",
          {"note": "admin review only — not an AV scan"}, tenant_id=doc.tenant_id)
    return doc, ""


def record_scan(session, doc: TradeDocument, *, clean: bool, actor=None, provider=""):
    """Record the verdict of a CONFIGURED anti-virus scanner. clean → 'scanned_clean'; otherwise 'infected'
    (and the document is force-unpublished). This is the only path that may claim a document is scanned."""
    doc.quarantine = "scanned_clean" if clean else "infected"
    if not clean:
        doc.seller_safe = False
    session.add(doc)
    try:
        from . import work_queue as WQ
        WQ.resolve_by_key(session, f"uploaded_document_needs_review:doc:{doc.id}",
                          note=f"scan: {'clean' if clean else 'INFECTED'}", actor=actor)
    except Exception:  # noqa: BLE001
        pass
    audit(session, actor, "trade_document", doc.id, "document_scanned",
          {"clean": clean, "provider": provider}, tenant_id=doc.tenant_id)
    return doc


def mark_seller_safe(session, doc: TradeDocument, *, actor=None):
    """Admin confirms the document carries NO buyer/provider PII → it may be published to the owning seller.
    Refused while quarantined/infected. When a real AV scanner IS configured, admin attestation alone is not
    enough — the document must be 'scanned_clean' before it can go to a seller (real scanning gates production
    seller-facing exchange)."""
    if not is_released(doc):
        return False, "document must be released from quarantine (admin-attested or scanned clean) first"
    if av_configured() and doc.quarantine != "scanned_clean":
        return False, "a malware scanner is configured — the document must be scanned clean before seller release"
    doc.seller_safe = True
    session.add(doc)
    audit(session, actor, "trade_document", doc.id, "document_seller_safe",
          {"basis": doc.quarantine}, tenant_id=doc.tenant_id)
    return True, ""


def archive_document(session, doc: TradeDocument, *, actor=None):
    doc.status = "archived"
    session.add(doc)
    audit(session, actor, "trade_document", doc.id, "document_archived", {}, tenant_id=doc.tenant_id)
    return doc


def approve_requirement(session, req: DocumentRequirement, *, approve=True, reason="", actor=None, now=None):
    now = now or datetime.utcnow()
    req.approval_state = "approved" if approve else "rejected"
    req.status = "approved" if approve else "rejected"
    if not approve:
        req.rejection_reason = (reason or "")[:500]   # seller-safe reason when required_from == seller
    req.completed_at = now if approve else req.completed_at
    session.add(req)
    audit(session, actor, "document_requirement", req.id, "requirement_reviewed",
          {"approved": approve}, tenant_id=req.tenant_id)
    return req


def request_seller_document(session, req: DocumentRequirement, *, instructions="", due_date=None, actor=None,
                            now=None):
    """Request a missing document FROM the seller through the sanitized SellerUpdate mechanism. Returns
    (ok, error). Refuses if the instructions contain any PII/contact (never leak buyer/provider details).
    Requires the requirement to be tied to the seller's ServiceRequest so the upload lands on the right request."""
    now = now or datetime.utcnow()
    hits = sanitize_scan(instructions or "")
    if hits:
        return False, f"instructions contain unsafe content: {', '.join(h['kind'] for h in hits)}"
    if not req.request_id or not req.tenant_id:
        return False, "a seller document request needs the seller's linked request"
    from .models import SellerUpdate
    sr = session.get(ServiceRequest, req.request_id)
    su = SellerUpdate(request_id=req.request_id, seller_id=req.tenant_id,
                      public_status="Document requested",
                      summary=(instructions or f"Please provide your {req.doc_type.replace('_', ' ')}.")[:1000],
                      next_action=f"Upload {req.doc_type.replace('_', ' ')}"
                      + (f" by {due_date:%Y-%m-%d}" if due_date else ""),
                      deadline=due_date, published=True,
                      published_by=getattr(actor, "email", "") or "")
    session.add(su)
    req.status = "requested"
    req.seller_action_required = True
    if due_date:
        req.due_date = due_date
    session.add(req)
    session.flush()
    try:
        from . import work_queue as WQ
        WQ.create_work_item_safe(
            session, tenant_id=req.tenant_id, type="seller_document_required", source="automatic",
            visibility="requester_visible",
            title=f"Seller document requested: {req.doc_type}",
            description="Awaiting the seller's document upload on their request.",
            related_request_id=req.request_id, related_operation_case_id=req.operation_case_id,
            related_seller_update_id=su.id,
            idempotency_key=f"seller_document_required:req:{req.id}", condition_version="requested")
    except Exception:  # noqa: BLE001
        pass
    audit(session, actor, "document_requirement", req.id, "seller_document_requested",
          {"doc_type": req.doc_type, "request_id": req.request_id}, tenant_id=req.tenant_id)
    return True, ""
