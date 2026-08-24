"""Explicit, idempotent production migration for the Phase-7 Operations schema.

Creates the new operational tables + partial-unique indexes + the additive WorkItem linkage columns; dry-run,
integrity check, pre/post operational-count invariants (quotes/deals/requests/outreach/products unchanged),
index verification.

    ./.venv/bin/python scripts/migrate_gate_p7.py --dry-run
    ./.venv/bin/python scripts/migrate_gate_p7.py

Run AFTER scripts/migrate.py. Take a dated backup first (scripts/backup_db.py). Rollback = restore the backup
(additions are non-destructive). Never deletes quotes, deals, requests, outreach, product/pricing or any
operational history.
"""
import os
import sys

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)

import app.models  # noqa: E402,F401
from sqlalchemy import inspect, text   # noqa: E402
from sqlmodel import Session, func, select   # noqa: E402

from app.db import _is_sqlite, engine   # noqa: E402
from app.models import (CustomsCase, Deal, DeliveryConfirmation, DocumentRequirement, FreightOffer,
                        FreightRequest, OperationCase, OperationalException, Outreach, PaymentMilestone,
                        Product, Quote, RemittanceCase, ServiceRequest, Settlement, SettlementAdjustment,
                        Shipment, ShipmentEvent, ShipmentLeg, TradeDocument)   # noqa: E402

_NEW_TABLES = [OperationCase, FreightRequest, FreightOffer, Shipment, ShipmentLeg, ShipmentEvent,
               DocumentRequirement, TradeDocument, CustomsCase, DeliveryConfirmation, OperationalException,
               PaymentMilestone, RemittanceCase, Settlement, SettlementAdjustment]
_GATE_INDEXES = [
    ("uq_shipmentevent_ext", "shipmentevent", "source, external_event_id", True, "external_event_id != ''"),
    ("uq_operationcase_reference", "operationcase", "reference", True, "reference != ''"),
    ("uq_opcase_deal_primary", "operationcase", "deal_id", True, "case_type = 'deal' AND deal_id IS NOT NULL"),
    ("ix_shipment_deal", "shipment", "deal_id", False),
    ("ix_paymentmilestone_deal", "paymentmilestone", "deal_id", False),
]
_WORKITEM_COLS = {"related_operation_case_id", "related_shipment_id", "related_payment_id",
                  "related_exception_id"}
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
    # additive WorkItem columns (idempotent — only added when absent)
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
    print("Phase-7 gate migration complete (idempotent).")


if __name__ == "__main__":
    main(dry="--dry-run" in sys.argv)
