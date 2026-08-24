"""Central suppression / do-not-contact list (Phase 4).

EVERY buyer-send path MUST call `is_suppressed()` immediately before sending. Suppression is durable —
addresses are never deleted, only deactivated by an audited admin action. `scope='platform'` applies across
ALL tenants (enforced internally without revealing which tenant created it); `scope='tenant'` applies within
one seller. Only test sends to an explicitly-entered authorized admin address may bypass buyer suppression.
"""
import hashlib

from sqlmodel import select

from .models import Suppression
from .pipeline import audit

REASONS = ["hard_bounce", "persistent_soft", "unsubscribe", "spam_complaint", "manual", "legal"]


def normalize_email(email: str) -> str:
    return (email or "").strip().lower()


def _hash(email_norm: str) -> str:
    return hashlib.sha256(email_norm.encode()).hexdigest()[:32] if email_norm else ""


def is_suppressed(session, email, tenant_id=None) -> bool:
    """True if the address is on an ACTIVE platform suppression, or a tenant suppression for THIS tenant.
    A platform suppression applies to everyone; the caller never learns which tenant created it (no leak)."""
    e = normalize_email(email)
    if not e:
        return False
    rows = session.exec(select(Suppression).where(Suppression.email_normalized == e,
                                                  Suppression.active == True)).all()   # noqa: E712
    for s in rows:
        if s.scope == "platform" or (s.scope == "tenant" and tenant_id is not None
                                     and s.tenant_id == tenant_id):
            return True
    return False


def suppress(session, email, reason, actor=None, scope="platform", tenant_id=None, source_event="", note=""):
    """Get-or-reactivate a suppression for an address (idempotent via the unique active index). Audited.
    Returns the Suppression, or None for an empty address."""
    e = normalize_email(email)
    if not e:
        return None
    reason = reason if reason in REASONS else "manual"
    scoped_tenant = tenant_id if scope == "tenant" else None
    existing = session.exec(select(Suppression).where(
        Suppression.email_normalized == e, Suppression.scope == scope,
        Suppression.tenant_id == scoped_tenant)).first()
    if existing:
        existing.active = True
        existing.reason = reason
        if source_event:
            existing.source_event = source_event
        session.add(existing)
        sup = existing
    else:
        sup = Suppression(email_normalized=e, email_hash=_hash(e), reason=reason, scope=scope,
                          tenant_id=scoped_tenant, source_event=source_event, note=(note or "")[:500],
                          suppressed_by=getattr(actor, "id", None))
        session.add(sup)
    try:
        audit(session, actor, "suppression", getattr(sup, "id", None), "suppress",
              {"reason": reason, "scope": scope}, tenant_id=scoped_tenant)
    except Exception:  # noqa: BLE001
        pass
    return sup


def unsuppress(session, sup, actor=None):
    """Deactivate a suppression (audited admin action). The row is kept for history."""
    sup.active = False
    session.add(sup)
    try:
        audit(session, actor, "suppression", sup.id, "unsuppress", {}, tenant_id=sup.tenant_id)
    except Exception:  # noqa: BLE001
        pass
    return sup
