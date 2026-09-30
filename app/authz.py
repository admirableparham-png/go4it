"""Phase 10 — centralized, server-side authorization (the ONLY place permission decisions are made).

DEFAULT DENY. A permission decision resolves: role-template permissions + per-user GRANT overrides − DENY
overrides, intersected with the known catalog. Founder holds everything. A disabled/archived account holds
nothing. The HARD seller-confidentiality rule is machine-enforced here: a `seller` account can NEVER hold any
seller-forbidden permission (buyer PII, exports, outreach, costs/margins, research sources, internal actions),
no matter what role or override is set — so no permission, override, export, API route or UI path can leak buyer
data to a seller.

Backward-compatible: legacy code keeps calling `tenant.is_admin`/`tenant.scoped` (internal staff carry the
legacy `admin` role → they reach internal workspaces and see internal data; sellers carry `agent` → sanitized,
own-scoped). This module ADDS the granular layer that gates the dangerous surface (buyer-PII view/export, every
high-risk action, user management, exports, live Claude) and powers the admin access UI + audit.
"""
import json
from datetime import datetime

from sqlmodel import select

from . import permissions as P
from .models import AccessAuditLog, PermissionOverride, RoleTemplate, User, UserProfile
from .tenant import is_admin


def profile(session, user):
    if not user or getattr(user, "id", None) is None:
        return None
    return session.exec(select(UserProfile).where(UserProfile.user_id == user.id)).first()


def account_class(session, user) -> str:
    p = profile(session, user)
    if p:
        return p.account_class
    return "internal" if is_admin(user) else "seller"     # legacy fallback (pre-backfill / edge)


def is_seller(session, user) -> bool:
    return account_class(session, user) == "seller"


def _template_permissions(session, role_key: str) -> set:
    if not role_key:
        return set()
    rt = session.exec(select(RoleTemplate).where(RoleTemplate.key == role_key)).first()
    if rt and rt.permissions:
        try:
            return set(json.loads(rt.permissions)) & P.ALL_PERMISSIONS
        except Exception:  # noqa: BLE001
            pass
    return P.template_permissions(role_key)               # code catalog fallback


def effective_permissions(session, user) -> frozenset:
    """The resolved permission set for `user` (DEFAULT DENY). See module docstring for the rule."""
    if not user or getattr(user, "id", None) is None:
        return frozenset()
    p = profile(session, user)
    if p is None:
        # no profile row yet: preserve legacy authority exactly — admin => full internal; else nothing
        return frozenset(P.ALL_PERMISSIONS) if is_admin(user) else frozenset()
    if p.account_status != "active":
        return frozenset()                                # disabled/archived => no access
    perms = _template_permissions(session, p.role_key)
    grants, denies = set(), set()
    for o in session.exec(select(PermissionOverride).where(PermissionOverride.user_id == user.id)).all():
        (grants if o.effect == "grant" else denies).add(o.permission_key)
    eff = ((perms | grants) - denies) & P.ALL_PERMISSIONS
    if p.account_class == "seller":
        eff = eff - P.SELLER_FORBIDDEN                     # *** HARD seller-confidentiality boundary ***
    return frozenset(eff)


def has_permission(session, user, perm: str) -> bool:
    """The single authorization predicate every protected route/download/export/action must call."""
    return perm in effective_permissions(session, user)


# convenience alias used at call sites
def can(session, user, perm: str) -> bool:
    return has_permission(session, user, perm)


def data_scope(session, user) -> str:
    p = profile(session, user)
    if p:
        return p.scope or "own"
    return "platform" if is_admin(user) else "own"


def why(session, user, perm: str) -> str:
    """Human explanation for the admin 'why does this user have access?' view — never leaks secrets."""
    p = profile(session, user)
    if p is None:
        return "legacy admin (no profile)" if is_admin(user) else "no access"
    if p.account_status != "active":
        return f"account {p.account_status} — no access"
    ov = session.exec(select(PermissionOverride).where(PermissionOverride.user_id == user.id,
                                                       PermissionOverride.permission_key == perm)).first()
    if ov and ov.effect == "deny":
        return "explicitly DENIED by override"
    if p.account_class == "seller" and perm in P.SELLER_FORBIDDEN:
        return "blocked by the hard seller-confidentiality rule"
    if ov and ov.effect == "grant":
        return f"granted by override ({ov.reason or 'no reason'})"
    if perm in _template_permissions(session, p.role_key):
        return f"from role template '{p.role_key}'"
    return "not granted"


# --------------------------------------------------------------------- safety controls
def active_founders(session):
    rows = session.exec(select(UserProfile).where(UserProfile.role_key == P.FOUNDER_ROLE,
                                                  UserProfile.account_status == "active")).all()
    return [r.user_id for r in rows]


def is_last_founder(session, user_id) -> bool:
    fs = active_founders(session)
    return fs == [user_id]


def is_founder(session, user) -> bool:
    """A Founder account: founder role template, or founder-level control via an override. A legacy admin with no
    profile yet counts as one (that is the authority it holds before the backfill)."""
    if not user or getattr(user, "id", None) is None:
        return False
    p = profile(session, user)
    if p is None:
        return is_admin(user)
    return p.role_key == P.FOUNDER_ROLE or "founder.control" in effective_permissions(session, user)


def founder_protected(session, actor, target) -> bool:
    """True when `actor` must NOT change `target`'s password or access: the target is a Founder and the actor lacks
    founder-level control. Stops an Admin/Manager (who holds users.manage) from taking a Founder account over via a
    password reset, or neutralising it via disable / demote / permission-deny."""
    return is_founder(session, target) and not has_permission(session, actor, "founder.control")


def can_assign_role(session, actor, role_key: str) -> bool:
    """Only a founder may assign the founder role or any template carrying founder-level control."""
    if not has_permission(session, actor, "users.manage"):
        return False
    template = P.ROLE_TEMPLATES.get(role_key, {})
    grants_founder = role_key == P.FOUNDER_ROLE or "founder.control" in template.get("permissions", set())
    return has_permission(session, actor, "founder.control") if grants_founder else True


def can_grant_permission(session, actor, target_user, perm: str) -> tuple:
    """(ok, reason). Enforces: manage right; no founder-level grant by a non-founder; no self-escalation."""
    if not has_permission(session, actor, "users.manage"):
        return False, "you cannot manage access"
    if perm in ("founder.control",) and not has_permission(session, actor, "founder.control"):
        return False, "only a founder may grant founder-level control"
    if perm == "ai.live.use" and not has_permission(session, actor, "founder.control"):
        return False, "only a founder may grant live Claude"
    # no self-escalation: you cannot grant yourself a permission you don't already hold
    if actor is not None and target_user is not None and actor.id == target_user.id \
            and not has_permission(session, actor, perm):
        return False, "you cannot increase your own authority"
    return True, ""


# --------------------------------------------------------------------- audit (immutable, never secrets)
_SECRETY = ("password", "secret", "token", "api_key", "apikey", "credential", "mailbox")


def audit(session, actor, target_user_id, action, *, field="", before="", after="", reason=""):
    """Append an immutable access-change record. Refuses to store anything that looks like a secret."""
    def _clean(v):
        s = str(v or "")
        low = s.lower()
        return "[redacted]" if any(w in low for w in _SECRETY) else s[:500]
    rec = AccessAuditLog(actor_id=getattr(actor, "id", None), target_user_id=target_user_id, action=action,
                         field=field[:80], before=_clean(before), after=_clean(after), reason=_clean(reason))
    session.add(rec)
    return rec


# --------------------------------------------------------------------- profile helpers
def ensure_profile(session, user, *, account_class=None, role_key="", scope=None):
    """Get-or-create the 1:1 profile. Never downgrades an existing one."""
    p = profile(session, user)
    if p:
        return p
    ac = account_class or ("internal" if is_admin(user) else "seller")
    rk = role_key or (P.FOUNDER_ROLE if is_admin(user) else "seller")
    sc = scope or P.ROLE_TEMPLATES.get(rk, {}).get("scope", "own")
    p = UserProfile(user_id=user.id, account_class=ac, role_key=rk, scope=sc,
                    account_status="active" if user.active else "disabled",
                    full_name=user.name or "", display_name=user.name or "")
    session.add(p)
    session.flush()
    return p


def revoke_sessions(session, user):
    """Invalidate any active session for `user` (called on disable / critical security change)."""
    p = profile(session, user)
    if p:
        p.sessions_revoked_at = datetime.utcnow()
        session.add(p)
