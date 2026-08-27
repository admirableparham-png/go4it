"""Phase 10 gate — additive access-control tables + indexes. Idempotent, count-invariant.

    ./.venv/bin/python scripts/migrate_gate_p10.py --dry-run   # show pending, change nothing
    ./.venv/bin/python scripts/migrate_gate_p10.py             # apply

Run AFTER scripts/migrate.py. Take a dated backup first. Creates UserProfile / RoleTemplate /
PermissionOverride / AccessAuditLog (no columns on existing tables). Operational counts (incl. users) are
asserted invariant — this migration never touches a user, lead, quote, deal, request, outreach or product row.
"""
import os
import sys

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)

import app.models  # noqa: F401,E402  (register metadata)
from sqlalchemy import inspect, text  # noqa: E402
from sqlmodel import Session, func, select  # noqa: E402

from app.db import _is_sqlite, engine  # noqa: E402
from app.models import (AccessAuditLog, Deal, Lead, Outreach, PermissionOverride, Product, RoleTemplate,  # noqa: E402
                        ServiceRequest, Quote, User, UserProfile)

_NEW_TABLES = [UserProfile, RoleTemplate, PermissionOverride, AccessAuditLog]
_GATE_INDEXES = [
    ("uq_userprofile_user", "userprofile", "user_id", True, "user_id IS NOT NULL"),
    ("uq_roletemplate_key", "roletemplate", "key", True, "key != ''"),
    ("uq_permoverride_user_perm", "permissionoverride", "user_id, permission_key", True, ""),
    ("ix_accessaudit_target", "accessauditlog", "target_user_id", False),
    ("ix_accessaudit_actor", "accessauditlog", "actor_id", False),
    ("ix_userprofile_role", "userprofile", "role_key", False),
]
_OPERATIONAL = {"users": User, "leads": Lead, "quotes": Quote, "deals": Deal, "requests": ServiceRequest,
                "outreach": Outreach, "products": Product}


def _counts(s):
    return {k: s.exec(select(func.count()).select_from(m)).one() for k, m in _OPERATIONAL.items()}


def _integrity_ok():
    if not _is_sqlite:
        return True
    with engine.connect() as c:
        return (c.execute(text("PRAGMA integrity_check")).fetchone() or ["?"])[0] == "ok"


def _plan():
    insp = inspect(engine)
    tables = set(insp.get_table_names())
    ops = []
    for m in _NEW_TABLES:
        if m.__tablename__ not in tables:
            ops.append(f"CREATE TABLE {m.__tablename__}")
    for idx in _GATE_INDEXES:
        name, table = idx[0], idx[1]
        if table not in tables:
            continue
        have = {i["name"] for i in insp.get_indexes(table)} | {u["name"] for u in
                                                               insp.get_unique_constraints(table)}
        if name not in have:
            ops.append(f"CREATE INDEX {name}")
    return ops


def _apply():
    for m in _NEW_TABLES:
        m.__table__.create(bind=engine, checkfirst=True)
    with engine.begin() as c:
        for idx in _GATE_INDEXES:
            name, table, col, uniq = idx[0], idx[1], idx[2], idx[3]
            where = f" WHERE {idx[4]}" if len(idx) > 4 and idx[4] else ""
            u = "UNIQUE " if uniq else ""
            c.execute(text(f"CREATE {u}INDEX IF NOT EXISTS {name} ON {table}({col}){where}"))


def _verify():
    insp = inspect(engine)
    tables = set(insp.get_table_names())
    problems = [f"missing table {m.__tablename__}" for m in _NEW_TABLES if m.__tablename__ not in tables]
    for idx in _GATE_INDEXES:
        name, table = idx[0], idx[1]
        if table not in tables:
            problems.append(f"missing table {table}")
            continue
        have = {i["name"] for i in insp.get_indexes(table)} | {u["name"] for u in
                                                               insp.get_unique_constraints(table)}
        if name not in have:
            problems.append(f"missing index {name}")
    return problems


def main(dry=False):
    if not _integrity_ok():
        print("ABORT: PRAGMA integrity_check failed — restore a backup before migrating.")
        sys.exit(2)
    with Session(engine) as s:
        pre = _counts(s)
    ops = _plan()
    print("PRE :", pre)
    print("pending operations:", ops or "none (already up to date)")
    if dry:
        print("[dry-run] no changes made.")
        return
    _apply()
    with Session(engine) as s:
        post = _counts(s)
    changed = {k: (pre[k], post[k]) for k in _OPERATIONAL if pre[k] != post[k]}
    if changed:
        print("ABORT: operational counts changed (must be invariant):", changed)
        sys.exit(3)
    problems = _verify()
    print("POST:", post)
    print("index verification:", "OK" if not problems else problems)
    if problems:
        sys.exit(4)
    print("Phase-10 gate migration complete (idempotent).")


if __name__ == "__main__":
    main(dry="--dry-run" in sys.argv)
