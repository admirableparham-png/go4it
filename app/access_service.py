"""Phase 10 — access-management operations (create user, set role, grant/deny permission, change status,
update profile). Every mutation runs the safety controls + writes an immutable AccessAuditLog. Routes AND tests
call these, so authorization can't be bypassed by a UI path. Returns (ok, message).

Safety controls enforced here (server-side, default-deny):
  * only a holder of `users.manage` may change anyone's access;
  * a non-founder can never grant the founder role or any founder-level permission (founder.control / ai.live.use);
  * no one can increase their OWN authority;
  * the last active Founder can never be disabled/archived or demoted;
  * disabling / a critical security change revokes the target's active sessions;
  * a SELLER can never be granted a seller-forbidden (buyer-PII/internal) permission — the hard rule wins;
  * secrets (passwords/keys/tokens) are never written to the audit log.
"""
from datetime import datetime

from sqlmodel import select

from . import authz
from . import permissions as P
from .auth import hash_password
from .models import PermissionOverride, RoleTemplate, User, UserProfile


def _prof(session, user_id):
    return session.exec(select(UserProfile).where(UserProfile.user_id == user_id)).first()


def create_user(session, actor, *, email, name, account_class, role_key, password):
    if not authz.has_permission(session, actor, "users.manage"):
        return False, "you cannot manage users"
    email = (email or "").strip().lower()
    if not email or len(password or "") < 6:
        return False, "email and a 6+ char password are required"
    if session.exec(select(User).where(User.email == email)).first():
        return False, "a user with that email already exists"
    if account_class not in ("internal", "seller"):
        account_class = "seller"
    if role_key not in P.ROLE_TEMPLATES or P.ROLE_TEMPLATES[role_key]["account_class"] != account_class:
        role_key = P.DEFAULT_ROLE[account_class]
    if not authz.can_assign_role(session, actor, role_key):
        return False, "you cannot assign that role"
    # legacy role: internal staff carry the legacy 'admin' tier (reach internal workspaces); sellers carry 'agent'
    legacy = "admin" if account_class == "internal" else "agent"
    u = User(email=email, name=(name or "").strip(), role=legacy, password_hash=hash_password(password))
    session.add(u); session.flush()
    scope = P.ROLE_TEMPLATES[role_key]["scope"]
    session.add(UserProfile(user_id=u.id, account_class=account_class, role_key=role_key, scope=scope,
                            account_status="active", full_name=u.name, display_name=u.name,
                            password_changed_at=datetime.utcnow()))
    authz.audit(session, actor, u.id, "user_created", field="role_key", after=role_key,
                reason=f"account_class={account_class}")
    return True, "user created"


def set_role(session, actor, target, role_key, *, scope=None, reason=""):
    if not authz.has_permission(session, actor, "users.manage"):
        return False, "you cannot manage users"
    p = _prof(session, target.id)
    if p is None:
        p = authz.ensure_profile(session, target)
    if role_key not in P.ROLE_TEMPLATES:
        return False, "unknown role"
    if P.ROLE_TEMPLATES[role_key]["account_class"] != p.account_class:
        return False, "role does not match the account class"
    if not authz.can_assign_role(session, actor, role_key):
        return False, "only a founder may assign that role"
    # never demote the last founder away from founder
    if p.role_key == P.FOUNDER_ROLE and role_key != P.FOUNDER_ROLE and authz.is_last_founder(session, target.id):
        return False, "cannot demote the last active Founder"
    before = p.role_key
    p.role_key = role_key
    p.scope = scope if scope in P.SCOPES else P.ROLE_TEMPLATES[role_key]["scope"]
    p.sessions_revoked_at = datetime.utcnow()          # critical change → re-auth
    p.updated_at = datetime.utcnow(); session.add(p)
    authz.audit(session, actor, target.id, "role_changed", field="role_key", before=before, after=role_key,
                reason=reason)
    return True, "role updated"


def set_override(session, actor, target, permission_key, effect, *, reason=""):
    """effect in {'grant','deny','clear'}."""
    if permission_key not in P.ALL_PERMISSIONS:
        return False, "unknown permission"
    tp = _prof(session, target.id) or authz.ensure_profile(session, target)
    # HARD seller rule: a seller can never be granted a seller-forbidden permission (it would be stripped anyway)
    if effect == "grant" and tp.account_class == "seller" and permission_key in P.SELLER_FORBIDDEN:
        return False, "a seller can never hold that permission (hard confidentiality rule)"
    if effect in ("grant", "deny"):
        ok, msg = authz.can_grant_permission(session, actor, target, permission_key)
        if not ok:
            return False, msg
    existing = session.exec(select(PermissionOverride).where(
        PermissionOverride.user_id == target.id, PermissionOverride.permission_key == permission_key)).first()
    before = existing.effect if existing else "(none)"
    if effect == "clear":
        if existing:
            session.delete(existing)
        action = "permission_cleared"
    else:
        if existing:
            existing.effect = effect; existing.reason = reason[:200]; existing.granted_by = actor.id
            session.add(existing)
        else:
            session.add(PermissionOverride(user_id=target.id, permission_key=permission_key, effect=effect,
                                           reason=reason[:200], granted_by=actor.id))
        action = "permission_granted" if effect == "grant" else "permission_revoked"
    tp.sessions_revoked_at = datetime.utcnow(); session.add(tp)   # critical change
    authz.audit(session, actor, target.id, action, field=permission_key, before=before, after=effect,
                reason=reason)
    return True, "permission override updated"


def set_status(session, actor, target, status, *, reason=""):
    if not authz.has_permission(session, actor, "users.manage"):
        return False, "you cannot manage users"
    if status not in ("active", "disabled", "archived"):
        return False, "invalid status"
    p = _prof(session, target.id) or authz.ensure_profile(session, target)
    if status != "active" and p.role_key == P.FOUNDER_ROLE and authz.is_last_founder(session, target.id):
        return False, "cannot disable/archive the last active Founder"
    if actor is not None and target.id == actor.id and status != "active":
        return False, "you cannot disable your own account"
    before = p.account_status
    p.account_status = status
    target.active = (status == "active")                # keep the legacy flag in sync (current_user honors both)
    if status != "active":
        p.disabled_at = datetime.utcnow(); p.disabled_by = actor.id
        p.sessions_revoked_at = datetime.utcnow()       # revoke active sessions immediately
    p.updated_at = datetime.utcnow(); session.add(p); session.add(target)
    authz.audit(session, actor, target.id, f"account_{status}", field="account_status", before=before,
                after=status, reason=reason)
    return True, f"account {status}"


def update_profile(session, actor, target, fields: dict, *, self_edit=False):
    """Self-service or manager profile edit. Never changes role/scope/status/account_class here (those are the
    access routes). Seller-provided fields never grant access to buyer data."""
    if not self_edit and not authz.has_permission(session, actor, "users.manage"):
        return False, "you cannot edit this profile"
    p = _prof(session, target.id) or authz.ensure_profile(session, target)
    allowed = {"full_name", "display_name", "job_title", "department", "company", "country", "timezone",
               "preferred_language", "phone", "avatar_url", "notification_prefs", "trading_interests",
               "preferred_markets"}
    changed = []
    for k, v in (fields or {}).items():
        if k in allowed and getattr(p, k, None) != v:
            setattr(p, k, v); changed.append(k)
    if changed:
        p.updated_at = datetime.utcnow(); session.add(p)
        authz.audit(session, actor, target.id, "profile_updated", field=",".join(changed)[:80],
                    reason="self" if self_edit else "manager")
    return True, "profile saved"
