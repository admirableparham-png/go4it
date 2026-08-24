"""Phase 6 — contracts: workflow, versions, templates, confidentiality.

Buyer-side and supplier-side contracts are SEPARATE documents (confidential by default): a buyer contract never
carries seller/supplier contact info; a supplier contract never carries buyer identity/contact; internal
margin/sourcing never appears in an external contract. Admins explicitly choose the type + parties. Signed/
approved versions are immutable — amendments are new linked versions. Templates use an allowlist of variables.
"""
import html as _html
import json
import re
from datetime import datetime

from sqlmodel import select

STATUSES = ["draft", "needs_review", "approved", "sent", "viewed", "change_requested", "signed", "declined",
            "expired", "terminated", "superseded", "archived"]
TRANSITIONS = {
    "draft": {"needs_review", "approved", "cancelled", "archived"},
    "needs_review": {"approved", "draft", "declined", "archived"},
    "approved": {"sent", "draft", "signed", "expired", "superseded", "archived"},
    "sent": {"viewed", "signed", "declined", "change_requested", "expired", "superseded", "archived"},
    "viewed": {"signed", "declined", "change_requested", "expired", "superseded", "archived"},
    "change_requested": {"draft", "archived"},
    "signed": {"terminated", "superseded", "archived"},
    "declined": {"draft", "archived"},
    "expired": {"draft", "archived"},
    "terminated": {"archived"},
    "superseded": {"archived"},
    "archived": set(),
}
CONTRACT_TYPES = ["buyer_sales", "supplier_purchase", "service", "nda", "amendment", "other"]


def can_transition(frm, to) -> bool:
    return to in TRANSITIONS.get(frm or "draft", set())


def transition(session, contract, to, actor=None, reason="") -> tuple:
    from .models import ContractStatusEvent
    frm = contract.status or "draft"
    if to == frm:
        return True, "no change"
    if not can_transition(frm, to):
        return False, f"invalid transition {frm} → {to}"
    contract.status = to
    contract.updated_at = datetime.utcnow()
    ver = _current_version(session, contract)
    if ver is not None:
        ver.status = to
        if to == "approved":
            ver.approved_by = getattr(actor, "email", "") or ""
            ver.approved_at = datetime.utcnow()
        session.add(ver)
    session.add(contract)
    session.add(ContractStatusEvent(contract_id=contract.id, contract_version_id=(ver.id if ver else None),
                                    from_status=frm, to_status=to, actor_id=getattr(actor, "id", None),
                                    reason=(reason or "")[:500]))
    _audit(session, actor, contract.id, "contract_transition", {"from": frm, "to": to})
    return True, ""


def _current_version(session, contract):
    from .models import ContractVersion
    if contract.current_version_id:
        return session.get(ContractVersion, contract.current_version_id)
    return session.exec(select(ContractVersion).where(ContractVersion.contract_id == contract.id)
                        .order_by(ContractVersion.version.desc())).first()


def create_contract(session, *, contract_type, side, tenant_id=None, country="", jurisdiction="", category="",
                    product_id=None, quote_id=None, quote_version_id=None, deal_id=None, company_id=None,
                    owner_id=None, terms="", payment_terms="", delivery_terms="", template_id=None,
                    actor=None, inferred=False):
    """Create a contract header + its immutable v1 version. Admin explicitly supplies type + side + parties."""
    from .models import Contract, ContractVersion
    if contract_type not in CONTRACT_TYPES:
        contract_type = "other"
    if side not in ("buyer", "supplier", "internal"):
        side = "buyer"
    c = Contract(contract_type=contract_type, side=side, tenant_id=tenant_id, country=country,
                 jurisdiction=jurisdiction, category=category, product_id=product_id, quote_id=quote_id,
                 quote_version_id=quote_version_id, deal_id=deal_id, company_id=company_id, owner_id=owner_id,
                 status="draft", created_by=getattr(actor, "email", "") or "")
    session.add(c); session.commit(); session.refresh(c)
    c.tracking_code = f"C-{c.id:05d}"
    v = ContractVersion(contract_id=c.id, version=1, status="draft", contract_type=contract_type,
                        parties_snapshot=json.dumps({"side": side, "company_id": company_id}),
                        terms=terms, payment_terms=payment_terms, delivery_terms=delivery_terms,
                        template_id=template_id, created_by=getattr(actor, "email", "") or "", inferred=inferred)
    session.add(v); session.commit(); session.refresh(v)
    c.current_version_id = v.id; session.add(c); session.commit(); session.refresh(c)
    _audit(session, actor, c.id, "contract_create", {"type": contract_type, "side": side})
    return c, v


def revise_contract(session, contract, actor=None, is_amendment=False):
    """Duplicate the current version into a new draft (or amendment) — never edits a signed/approved one."""
    from .models import ContractVersion
    cur = _current_version(session, contract)
    version = (session.exec(select(ContractVersion).where(ContractVersion.contract_id == contract.id)
                            .order_by(ContractVersion.version.desc())).first().version) + 1
    v = ContractVersion(contract_id=contract.id, version=version, status="draft",
                        contract_type=cur.contract_type, parties_snapshot=cur.parties_snapshot,
                        terms=cur.terms, payment_terms=cur.payment_terms, delivery_terms=cur.delivery_terms,
                        template_id=cur.template_id, supersedes_id=cur.id, is_amendment=is_amendment,
                        created_by=getattr(actor, "email", "") or "")
    session.add(v); session.commit(); session.refresh(v)
    contract.current_version_id = v.id; contract.status = "draft"; session.add(contract); session.commit()
    _audit(session, actor, contract.id, "contract_revise", {"version": version, "amendment": is_amendment})
    return v


# --------------------------------------------------------------------- templates (allowlisted vars)
_VAR = re.compile(r"\{\{\s*([a-zA-Z0-9_]+)\s*\}\}")


def template_vars(body: str):
    return set(_VAR.findall(body or ""))


def render_template(body: str, allowed: list, values: dict):
    """Render a template body: only allowlisted variables may be substituted (escaped); an unknown variable in
    the body, or a variable with no supplied value, is an error. Returns (text, error)."""
    allowed = set(allowed or [])
    used = template_vars(body)
    unknown = used - allowed
    if unknown:
        return "", f"unknown variables: {', '.join(sorted(unknown))}"
    missing = [v for v in used if v not in values]
    if missing:
        return "", f"unresolved variables: {', '.join(sorted(missing))}"

    def _sub(m):
        return _html.escape(str(values.get(m.group(1), "")))
    out = _VAR.sub(_sub, body or "")
    if _VAR.search(out):
        return "", "unresolved variables remain"
    return out, ""


LEGAL_NOTICE = ("INTERNAL NOTE: This template is not legal advice. Every generated contract must be reviewed "
                "by appropriate legal counsel before execution.")


def _audit(session, actor, contract_id, action, meta):
    try:
        from .pipeline import audit
        audit(session, actor, "contract", contract_id, action, meta)
    except Exception:  # noqa: BLE001
        pass
