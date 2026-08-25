"""Explicit, idempotent production migration for the Phase-8 Intelligence schema.

Creates the new intelligence tables + partial-unique indexes + the additive WorkItem linkage columns; dry-run,
integrity check, pre/post operational-count invariants (leads/quotes/deals/requests/outreach/products
unchanged), index verification.

    ./.venv/bin/python scripts/migrate_gate_p8.py --dry-run
    ./.venv/bin/python scripts/migrate_gate_p8.py

Run AFTER scripts/migrate.py. Take a dated backup first (scripts/backup_db.py). Rollback = restore the backup
(additions are non-destructive). Never deletes any operational history.
"""
import os
import sys

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)

import app.models  # noqa: E402,F401
from sqlalchemy import inspect, text   # noqa: E402
from sqlmodel import Session, func, select   # noqa: E402

from app.db import _is_sqlite, engine   # noqa: E402
from app.models import (AnalyticsReport, AnalyticsSnapshot, Deal, DemandSignal, IntelAlert, Lead, Opportunity,
                        OpportunityMatch, OpportunitySignal, Outreach, Product, Quote, ServiceRequest)   # noqa: E402

_NEW_TABLES = [DemandSignal, Opportunity, OpportunitySignal, OpportunityMatch, AnalyticsSnapshot, IntelAlert,
               AnalyticsReport]
_GATE_INDEXES = [
    ("uq_demandsignal_dedup", "demandsignal", "dedup_key", True, "dedup_key != ''"),
    ("uq_intelalert_key", "intelalert", "alert_key, condition_version", True, "alert_key != ''"),
    ("ix_opportunitysignal_opp", "opportunitysignal", "opportunity_id", False),
    ("ix_opportunitymatch_opp", "opportunitymatch", "opportunity_id", False),
    ("ix_analyticssnapshot_key", "analyticssnapshot", "cache_key", False),
]
_WORKITEM_COLS = {"related_opportunity_id", "related_alert_id"}
_OPERATIONAL = {"leads": Lead, "quotes": Quote, "deals": Deal, "requests": ServiceRequest,
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
    if "workitem" in tables:
        cols = {c["name"] for c in insp.get_columns("workitem")}
        for col in sorted(_WORKITEM_COLS - cols):
            ops.append(f"ADD COLUMN workitem.{col}")
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
    insp = inspect(engine)
    if "workitem" in set(insp.get_table_names()):
        have = {c["name"] for c in insp.get_columns("workitem")}
        with engine.begin() as c:
            for col in sorted(_WORKITEM_COLS - have):
                c.execute(text(f"ALTER TABLE workitem ADD COLUMN {col} INTEGER"))
    with engine.begin() as c:
        for idx in _GATE_INDEXES:
            name, table, col, uniq = idx[0], idx[1], idx[2], idx[3]
            where = f" WHERE {idx[4]}" if len(idx) > 4 else ""
            u = "UNIQUE " if uniq else ""
            c.execute(text(f"CREATE {u}INDEX IF NOT EXISTS {name} ON {table}({col}){where}"))


def _verify():
    insp = inspect(engine)
    tables = set(insp.get_table_names())
    problems = [f"missing table {m.__tablename__}" for m in _NEW_TABLES if m.__tablename__ not in tables]
    if "workitem" in tables:
        have = {c["name"] for c in insp.get_columns("workitem")}
        for col in sorted(_WORKITEM_COLS - have):
            problems.append(f"missing column workitem.{col}")
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
    print("Phase-8 gate migration complete (idempotent).")


if __name__ == "__main__":
    main(dry="--dry-run" in sys.argv)
