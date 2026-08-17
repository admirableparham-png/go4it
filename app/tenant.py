"""Per-user data isolation — the single hook every scoped query/route goes through.

go4it is multi-tenant by ROW OWNERSHIP: each trusted user (role "agent") sees ONLY the rows they own
(`owner_id == user.id`); the founder (role "admin") is the gatekeeper and sees EVERYTHING (scope is a
no-op). There is no shared workspace — every trader is a tenant of one.

Rules of the house:
- `scoped(stmt, owner_col, user)` — append the ownership filter to a SELECT. Admin → unchanged. A missing
  user (should never happen behind the auth gate) → fail CLOSED (matches nothing), never open.
- `owns(owner_id, user)` — the check for a single already-loaded row before showing/mutating it (fixes IDOR).
- Shared reference data (catalog, suppliers, rates, FX) is deliberately NOT owner-scoped; it is global and
  ADMIN-WRITE / trader-read. The moat surfaces (buyer search / research) are admin-only.

Keep this module dependency-light so it can be imported anywhere without cycles.
"""


def is_admin(user) -> bool:
    """True only for the founder/gatekeeper. The ONLY isolation bypass — keep it a pure role check."""
    return user is not None and getattr(user, "role", "") == "admin"


def scoped(stmt, owner_col, user):
    """Restrict a SELECT to the caller's own rows. Admin → no-op. No user → fail closed (never matches).

    `owner_col` is the explicit owner column of the primary table (e.g. Lead.owner_id, Quote.owner_id),
    passed explicitly so a joined query with two owner columns is unambiguous.
    """
    if is_admin(user):
        return stmt
    if user is None:
        return stmt.where(owner_col == -1)      # fail-closed sentinel: matches no real row
    return stmt.where(owner_col == user.id)


def owns(owner_id, user) -> bool:
    """Ownership check for one loaded row before read/mutate. Admin owns everything; a trader owns only
    rows stamped with their id. Unowned rows (owner_id is None) belong to the admin pool only."""
    if is_admin(user):
        return True
    return user is not None and owner_id is not None and owner_id == user.id
