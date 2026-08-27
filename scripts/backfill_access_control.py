"""Phase 10 backfill — seed role templates + a conservative access profile for every existing user.

    ./.venv/bin/python scripts/backfill_access_control.py --dry-run   # report; change nothing
    ./.venv/bin/python scripts/backfill_access_control.py             # apply (transactional)
    ./.venv/bin/python scripts/backfill_access_control.py --rollback  # PRE-GO-LIVE: drop backfilled profiles/templates

RUN ORDER (prod): backup_db.py -> migrate.py -> migrate_gate_p10.py -> THIS.

CONSERVATIVE + backward-compatible — it NEVER changes User.role/active and NEVER reduces access:
  * legacy role 'admin'   -> internal, role_key 'founder'         (preserves full authority)
  * legacy role 'manager' -> internal, role_key 'admin_manager'
  * legacy role 'viewer'  -> internal, role_key 'auditor'
  * legacy role 'agent'   -> seller,   role_key 'seller'          (sanitized; access NEVER broadened)
Seeds the 11 system RoleTemplates from permissions.ROLE_TEMPLATES. Operational counts (users/leads/quotes/deals/
requests/outreach/products) are asserted invariant; only the new access tables grow.
"""
import json
import os
import sys

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)

from datetime import datetime  # noqa: E402

from sqlmodel import Session, func, select  # noqa: E402

from app.db import engine, init_db  # noqa: E402
from app import permissions as P  # noqa: E402
from app.models import (Deal, Lead, Outreach, Product, RoleTemplate, ServiceRequest, Quote, User,  # noqa: E402
                        UserProfile)

_OPERATIONAL = ("users", "leads", "quotes", "deals", "requests", "outreach", "products")
_ROLE_MAP = {"admin": ("internal", "founder"), "manager": ("internal", "admin_manager"),
             "viewer": ("internal", "auditor"), "agent": ("seller", "seller")}


def _counts(s):
    m = {"users": User, "leads": Lead, "quotes": Quote, "deals": Deal, "requests": ServiceRequest,
         "outreach": Outreach, "products": Product}
    return {k: s.exec(select(func.count()).select_from(v)).one() for k, v in m.items()}


def _seed_templates(s):
    created = 0
    for key, t in P.ROLE_TEMPLATES.items():
        row = s.exec(select(RoleTemplate).where(RoleTemplate.key == key)).first()
        perms = json.dumps(sorted(t["permissions"]))
        if row is None:
            s.add(RoleTemplate(key=key, name=t["name"], account_class=t["account_class"],
                               scope_default=t["scope"], permissions=perms,
                               is_system=t.get("system", False), editable=not t.get("system", False)))
            created += 1
        else:
            # keep system templates in sync with the code catalog (permissions may evolve); never delete
            if row.is_system:
                row.permissions = perms; row.name = t["name"]; row.account_class = t["account_class"]
                row.scope_default = t["scope"]; row.updated_at = datetime.utcnow(); s.add(row)
    return created


def _backfill_profiles(s):
    created = 0
    for u in s.exec(select(User)).all():
        if s.exec(select(UserProfile).where(UserProfile.user_id == u.id)).first():
            continue
        ac, rk = _ROLE_MAP.get(u.role, ("seller", "seller"))   # unknown legacy role => safest (seller)
        scope = P.ROLE_TEMPLATES.get(rk, {}).get("scope", "own")
        s.add(UserProfile(user_id=u.id, account_class=ac, role_key=rk, scope=scope,
                          account_status="active" if u.active else "disabled",
                          full_name=u.name or "", display_name=u.name or "",
                          created_at=datetime.utcnow(), updated_at=datetime.utcnow()))
        created += 1
    return created


def migrate(dry=False):
    init_db()
    if dry:
        conn = engine.connect(); trans = conn.begin()
        try:
            ds = Session(bind=conn)
            pre = _counts(ds); print("PRE :", pre)
            tpl = _seed_templates(ds); prof = _backfill_profiles(ds)
            post = _counts(ds); ds.close()
        finally:
            trans.rollback(); conn.close()
        print(f"[dry-run] would seed {tpl} role template(s), create {prof} user profile(s)")
        print("POST:", post, "(rolled back — nothing persisted)")
        return
    with Session(engine) as s:
        pre = _counts(s); print("PRE :", pre)
        try:
            tpl = _seed_templates(s)
            prof = _backfill_profiles(s)
            post = _counts(s)
            for k in _OPERATIONAL:
                if pre[k] != post[k]:
                    raise RuntimeError(f"operational count changed for {k}: {pre[k]} -> {post[k]}")
            # every profile maps to a real user + a known template
            for pr in s.exec(select(UserProfile)).all():
                if pr.role_key not in P.ROLE_TEMPLATES:
                    raise RuntimeError(f"profile {pr.id} has unknown role_key {pr.role_key}")
            s.commit()
            print(f"OK — seeded {tpl} role template(s), created {prof} user profile(s)")
            print("POST:", post)
        except Exception:
            s.rollback(); print("ERROR — rolled back, no partial backfill applied"); raise


def rollback():
    """PRE-GO-LIVE revert: drop all backfilled profiles + seeded templates (safe only before real access edits)."""
    init_db()
    with Session(engine) as s:
        for pr in s.exec(select(UserProfile)).all():
            s.delete(pr)
        for rt in s.exec(select(RoleTemplate)).all():
            s.delete(rt)
        s.commit()
        print("rolled back: profiles + role templates removed")


if __name__ == "__main__":
    if "--rollback" in sys.argv:
        rollback()
    else:
        migrate(dry="--dry-run" in sys.argv)
