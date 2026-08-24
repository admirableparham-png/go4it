"""Explicit, idempotent production migration for the Phase-6 Commercial schema (quotes/contracts/deals).

Creates the new tables + unique constraints/indexes + additive columns; dry-run, integrity check, pre/post
operational-count invariants (quotes/deals/requests/outreach/products unchanged), index verification.

    ./.venv/bin/python scripts/migrate_gate_p6.py --dry-run
    ./.venv/bin/python scripts/migrate_gate_p6.py

Run AFTER scripts/migrate.py. Take a dated backup first. Rollback = restore the backup (additions are
non-destructive). Never deletes quotes, deals, requests, outreach, product or pricing history.
"""
import os
import sys

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)

import app.models  # noqa: E402,F401
from sqlalchemy import inspect, text   # noqa: E402
from sqlmodel import Session, func, select   # noqa: E402

from app.db import _is_sqlite, engine   # noqa: E402
from app.models import (Contract, ContractDocument, ContractParty, ContractStatusEvent, ContractTemplate,
                        ContractVersion, Deal, Outreach, Product, Quote, QuoteAccessToken, QuoteApproval,
                        QuoteDocument, QuoteLineItem, QuoteStatusEvent, QuoteVersion, ServiceRequest,
                        SignatureEvent)   # noqa: E402

_NEW_TABLES = [QuoteVersion, QuoteLineItem, QuoteStatusEvent, QuoteApproval, QuoteAccessToken, QuoteDocument,
               Contract, ContractVersion, ContractParty, ContractStatusEvent, ContractTemplate,
               ContractDocument, SignatureEvent]
_GATE_INDEXES = [
    ("uq_quoteaccesstoken_hash", "quoteaccesstoken", "token_hash", True, "token_hash != ''"),
    ("uq_deal_quote_version", "deal", "quote_version_id", True, "quote_version_id IS NOT NULL"),
    ("ix_quoteversion_quote", "quoteversion", "quote_id", False),
    ("ix_contract_status", "contract", "status", False),
]
_QUOTE_COLS = {"current_version_id", "viewed_at"}
_DEAL_COLS = {"quote_version_id", "ready_for_ops"}
_OPERATIONAL = {"quotes": Quote, "deals": Deal, "requests": ServiceRequest, "outreach": Outreach,
                "products": Product}


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
    if "quote" in tables:
        cols = {c["name"] for c in insp.get_columns("quote")}
        for col in sorted(_QUOTE_COLS - cols):
            ops.append(f"ADD COLUMN quote.{col}")
    if "deal" in tables:
        cols = {c["name"] for c in insp.get_columns("deal")}
        for col in sorted(_DEAL_COLS - cols):
            ops.append(f"ADD COLUMN deal.{col}")
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
            where = f" WHERE {idx[4]}" if len(idx) > 4 else ""
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
    print("Phase-6 gate migration complete (idempotent).")


if __name__ == "__main__":
    main(dry="--dry-run" in sys.argv)
