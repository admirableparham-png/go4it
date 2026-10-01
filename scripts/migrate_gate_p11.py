"""Phase 11 gate — outreach readiness. Additive, idempotent, count-invariant.

    ./.venv/bin/python scripts/migrate_gate_p11.py --dry-run   # show pending, change nothing
    ./.venv/bin/python scripts/migrate_gate_p11.py             # apply

Run AFTER scripts/migrate.py (which adds mailaccount.sender_company / postal_address and campaignstep.body_html at
boot; this gate re-checks them). Take a dated backup first. Creates the InboundSeen ledger table and the two
"enrolled at most once per campaign" unique indexes. Existing duplicate enrolments are REPORTED (exit 4), never
deleted — a person decides. Operational counts are asserted invariant.
"""
import os
import sys

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)

import app.models  # noqa: F401,E402  (register metadata)
from sqlalchemy import inspect, text  # noqa: E402
from sqlmodel import Session, func, select  # noqa: E402

from app.db import _is_sqlite, engine  # noqa: E402
from app.models import (CampaignRecipient, Deal, InboundSeen, Lead, Outreach, Product, Quote,  # noqa: E402
                        ServiceRequest, User)

_NEW_TABLES = [InboundSeen]
_COLUMNS = [("mailaccount", "sender_company", "VARCHAR DEFAULT ''"),
            ("mailaccount", "postal_address", "VARCHAR DEFAULT ''"),
            ("campaignstep", "body_html", "VARCHAR DEFAULT ''"),
            ("campaign", "bounce_baseline", "VARCHAR DEFAULT ''"),
            ("campaignstep", "attachment_path", "VARCHAR DEFAULT ''"),
            ("campaignstep", "plain_text_only", "BOOLEAN DEFAULT 0"),
            ("campaignstep", "list_unsubscribe", "BOOLEAN DEFAULT 1")]
_GATE_INDEXES = [
    ("uq_camprcpt_campaign_lead", "campaignrecipient", "campaign_id, lead_id", "lead_id IS NOT NULL"),
    ("uq_camprcpt_campaign_email", "campaignrecipient", "campaign_id, to_email", "to_email != ''"),
]
_DUP_SQL = {
    "uq_camprcpt_campaign_lead": "SELECT COUNT(*) FROM (SELECT 1 FROM campaignrecipient WHERE lead_id IS NOT NULL "
                                 "GROUP BY campaign_id, lead_id HAVING COUNT(*) > 1)",
    "uq_camprcpt_campaign_email": "SELECT COUNT(*) FROM (SELECT 1 FROM campaignrecipient WHERE to_email != '' "
                                  "GROUP BY campaign_id, to_email HAVING COUNT(*) > 1)",
}
_OPERATIONAL = {"users": User, "leads": Lead, "quotes": Quote, "deals": Deal, "requests": ServiceRequest,
                "outreach": Outreach, "products": Product, "campaign_recipients": CampaignRecipient}


def _counts(s):
    return {k: s.exec(select(func.count()).select_from(m)).one() for k, m in _OPERATIONAL.items()}


def _integrity_ok():
    if not _is_sqlite:
        return True
    with engine.connect() as c:
        return (c.execute(text("PRAGMA integrity_check")).fetchone() or ["?"])[0] == "ok"


def _have_indexes(insp, table):
    return {i["name"] for i in insp.get_indexes(table)} | {u["name"] for u in insp.get_unique_constraints(table)}


def _duplicates():
    with engine.connect() as c:
        return {name: c.execute(text(sql)).scalar() or 0 for name, sql in _DUP_SQL.items()}


def _plan():
    insp = inspect(engine)
    tables = set(insp.get_table_names())
    ops = [f"CREATE TABLE {m.__tablename__}" for m in _NEW_TABLES if m.__tablename__ not in tables]
    for table, col, _t in _COLUMNS:
        if table in tables and col not in {c["name"] for c in insp.get_columns(table)}:
            ops.append(f"ADD COLUMN {table}.{col}")
    for name, table, _cols, _where in _GATE_INDEXES:
        if table in tables and name not in _have_indexes(insp, table):
            ops.append(f"CREATE UNIQUE INDEX {name}")
    return ops


def _apply(dups):
    for m in _NEW_TABLES:
        m.__table__.create(bind=engine, checkfirst=True)
    insp = inspect(engine)
    with engine.begin() as c:
        for table, col, typ in _COLUMNS:
            if col not in {x["name"] for x in insp.get_columns(table)}:
                c.execute(text(f"ALTER TABLE {table} ADD COLUMN {col} {typ}"))
        for name, table, cols, where in _GATE_INDEXES:
            if not dups.get(name):
                c.execute(text(f"CREATE UNIQUE INDEX IF NOT EXISTS {name} ON {table}({cols}) WHERE {where}"))


def _verify():
    insp = inspect(engine)
    tables = set(insp.get_table_names())
    problems = [f"missing table {m.__tablename__}" for m in _NEW_TABLES if m.__tablename__ not in tables]
    for table, col, _t in _COLUMNS:
        if col not in {c["name"] for c in insp.get_columns(table)}:
            problems.append(f"missing column {table}.{col}")
    for name, table, _cols, _where in _GATE_INDEXES:
        if name not in _have_indexes(insp, table):
            problems.append(f"missing index {name}")
    return problems


def main(dry=False):
    if not _integrity_ok():
        print("ABORT: PRAGMA integrity_check failed — restore a backup before migrating.")
        sys.exit(2)
    with Session(engine) as s:
        pre = _counts(s)
    ops, dups = _plan(), _duplicates()
    print("PRE :", pre)
    print("pending operations:", ops or "none (already up to date)")
    print("existing duplicate enrolments (must be 0):", dups)
    if dry:
        print("[dry-run] no changes made.")
        return
    _apply(dups)
    with Session(engine) as s:
        post = _counts(s)
    changed = {k: (pre[k], post[k]) for k in _OPERATIONAL if pre[k] != post[k]}
    if changed:
        print("ABORT: operational counts changed (must be invariant):", changed)
        sys.exit(3)
    problems = _verify()
    print("POST:", post)
    print("verification:", "OK" if not problems else problems)
    if problems:
        if any(dups.values()):
            print("duplicate enrolments exist — the unique index was NOT created; resolve them, then re-run.")
        sys.exit(4)
    print("Phase-11 gate migration complete (idempotent).")


if __name__ == "__main__":
    main(dry="--dry-run" in sys.argv)
