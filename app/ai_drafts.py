"""Phase 9 — evidence-based draft generation.

Drafts are prepared but NEVER sent or published. Seller-facing drafts are confidentiality-scanned (no buyer
identity/contact); buyer-facing drafts strip the seller's identity and carry no internal costs/margin; no draft
contains an unresolved placeholder. The Phase-4 send guards / suppression / attachment lock remain the only path
to an actual send — a draft is inert content.
"""
import re

from .pipeline import audit, sanitize_scan, strip_seller_identity

SELLER_FACING = ("create_draft_seller_update", "request_seller_information")
BUYER_FACING = ("create_draft_email",)
_PLACEHOLDER = re.compile(r"\{\{.*?\}\}|\[\s*(?:name|company|insert|todo|xxx|placeholder)[^\]]*\]", re.I)
_COST_WORDS = re.compile(r"\b(margin|markup|exw cost|cost component|our cost|factory gate)\b", re.I)


def has_placeholder(text: str) -> bool:
    return bool(_PLACEHOLDER.search(text or ""))


def build_email_draft(*, subject, body, seller=None, seller_emails=()):
    """A buyer-facing email draft: strip the seller's identity, refuse internal cost/margin language, label it a
    DRAFT. Returns {subject, body, warnings}."""
    body = strip_seller_identity(body or "", seller, seller_emails) if seller else (body or "")
    warnings = []
    if _COST_WORDS.search(body):
        warnings.append("removed internal cost/margin language")
        body = _COST_WORDS.sub("[internal detail removed]", body)
    return {"subject": (subject or "").strip(), "body": body.strip(), "label": "DRAFT", "warnings": warnings}


def build_seller_update_draft(*, summary, next_action=""):
    """A seller-facing update draft. Returns {summary, next_action, blocked, hits} — blocked if it contains buyer
    PII (which must be removed before it can ever be published)."""
    hits = sanitize_scan(summary + " " + next_action)
    return {"summary": (summary or "").strip(), "next_action": (next_action or "").strip(),
            "label": "DRAFT", "blocked": bool(hits), "hits": [h["kind"] for h in hits]}


def finalize_draft(session, action_type, payload, *, actor=None, proposal=None):
    """Validate a draft at approval time. Raises ValueError if it has an unresolved placeholder, if a
    seller-facing draft contains buyer PII, or if a buyer-facing draft leaks internal cost/margin. On success
    returns a safe result marker — the draft is prepared, NEVER sent/published."""
    subject = payload.get("subject", "")
    body = payload.get("body") or payload.get("summary") or ""
    full = f"{subject} {body} {payload.get('next_action', '')}"
    if has_placeholder(full):
        raise ValueError("draft has an unresolved placeholder — fill it before approving")
    if action_type in SELLER_FACING:
        hits = sanitize_scan(full)
        if hits:
            raise ValueError(f"seller-facing draft contains buyer PII ({', '.join(h['kind'] for h in hits)}) "
                             "— blocked")
    if action_type in BUYER_FACING and _COST_WORDS.search(full):
        raise ValueError("buyer-facing draft contains internal cost/margin — blocked")
    audit(session, actor, "ai_draft", getattr(proposal, "id", None), "draft_finalized",
          {"kind": action_type}, tenant_id=getattr(proposal, "tenant_id", None))
    return {"draft_ready": True, "kind": action_type, "sent": False, "published": False,
            "note": "draft prepared — not sent or published; use the existing route to send with full guards"}
