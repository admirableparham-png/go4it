"""Explicit, idempotent production migration for the Phase-4 crash-safe send layer.

Rather than relying on ORM create_all as the production procedure, this EXPLICITLY and idempotently ensures:
  * the `campaignsend` table
  * its UNIQUE (campaign_id, recipient_id, sequence_version, step_index) constraint
  * lease / retry / status / rfc_message_id indexes used by the claim + recovery scans
  * the durable `rfc_message_id` column
  * `mailaccount.cred_enc_version` (encryption-scheme metadata)

    ./.venv/bin/python scripts/migrate_gate.py --dry-run   # show pending ops + counts; change nothing
    ./.venv/bin/python scripts/migrate_gate.py             # apply (idempotent; re-running is a no-op)

Run AFTER scripts/migrate.py (additive columns). Pre/post operational counts are asserted invariant; a
PRAGMA integrity_check runs first. Never deletes suppression, bounce, outreach, reply or campaign history.
Take a dated backup first (scripts/backup_db.py). Rollback/recovery guidance is in docs/PRODUCTION_MIGRATION.md.
"""
import os
import sys

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)

import app.models  # noqa: E402,F401 — register every model (incl. CampaignSend) in metadata
from sqlalchemy import inspect, text   # noqa: E402
from sqlmodel import Session, func, select   # noqa: E402

from app.db import engine, _is_sqlite   # noqa: E402
from app.models import (BounceRecord, Campaign, CampaignRecipient, CampaignSend, Lead, Outreach,
                        ServiceRequest, Suppression)   # noqa: E402

# indexes the claim + recovery scans rely on (lease_expires_at + next_attempt_at are not model-declared)
_GATE_INDEXES = [
    ("ix_campaignsend_lease_expires_at", "campaignsend", "lease_expires_at"),
    ("ix_campaignsend_next_attempt_at", "campaignsend", "next_attempt_at"),
    ("ix_campaignsend_status", "campaignsend", "status"),
    ("ix_campaignsend_rfc_message_id", "campaignsend", "rfc_message_id"),
    ("ix_campaignsend_campaign_id", "campaignsend", "campaign_id"),
    ("ix_campaignsend_recipient_id", "campaignsend", "recipient_id"),
]
_OPERATIONAL = {"leads": Lead, "outreach": Outreach, "requests": ServiceRequest, "campaigns": Campaign,
                "recipients": CampaignRecipient, "suppression": Suppression, "bounces": BounceRecord}


def _counts(s):
    return {k: s.exec(select(func.count()).select_from(m)).one() for k, m in _OPERATIONAL.items()}


def _integrity_ok():
    if not _is_sqlite:
        return True
    with engine.connect() as c:
        return (c.execute(text("PRAGMA integrity_check")).fetchone() or ["?"])[0] == "ok"


def _plan():
    """What is still missing (idempotent): returns a list of human-readable pending operations."""
    insp = inspect(engine)
    tables = set(insp.get_table_names())
    ops = []
    if "campaignsend" not in tables:
        ops.append("CREATE TABLE campaignsend (+ unique uq_campaignsend_crvs + declared indexes)")
    else:
        cols = {c["name"] for c in insp.get_columns("campaignsend")}
        if "rfc_message_id" not in cols:
            ops.append("ADD COLUMN campaignsend.rfc_message_id")
        have = {i["name"] for i in insp.get_indexes("campaignsend")}
        for name, _t, _c in _GATE_INDEXES:
            if name not in have:
                ops.append(f"CREATE INDEX {name}")
        uniq = {u["name"] for u in insp.get_unique_constraints("campaignsend")}
        if "uq_campaignsend_crvs" not in uniq and "uq_campaignsend_crvs" not in have:
            ops.append("ADD UNIQUE uq_campaignsend_crvs (recreate table — see note)")
    if "mailaccount" in tables:
        mcols = {c["name"] for c in insp.get_columns("mailaccount")}
        if "cred_enc_version" not in mcols:
            ops.append("ADD COLUMN mailaccount.cred_enc_version")
    return ops


def _apply():
    insp = inspect(engine)
    tables = set(insp.get_table_names())
    # 1) the table itself (with its UniqueConstraint + Field(index=True) indexes) — idempotent via checkfirst
    CampaignSend.__table__.create(bind=engine, checkfirst=True)
    # 2) additive columns on pre-existing tables
    with engine.begin() as c:
        cs_cols = {col["name"] for col in inspect(engine).get_columns("campaignsend")}
        if "rfc_message_id" not in cs_cols:
            c.execute(text("ALTER TABLE campaignsend ADD COLUMN rfc_message_id VARCHAR DEFAULT ''"))
        if "mailaccount" in tables:
            m_cols = {col["name"] for col in inspect(engine).get_columns("mailaccount")}
            if "cred_enc_version" not in m_cols:
                c.execute(text("ALTER TABLE mailaccount ADD COLUMN cred_enc_version INTEGER DEFAULT 1"))
        # 3) lease/retry/status/rfc indexes (idempotent)
        for name, table, col in _GATE_INDEXES:
            c.execute(text(f"CREATE INDEX IF NOT EXISTS {name} ON {table}({col})"))


def _verify():
    """Confirm the constraint + indexes + column now exist (index verification)."""
    insp = inspect(engine)
    problems = []
    cols = {c["name"] for c in insp.get_columns("campaignsend")}
    if "rfc_message_id" not in cols:
        problems.append("missing campaignsend.rfc_message_id")
    uniq = {u["name"] for u in insp.get_unique_constraints("campaignsend")}
    autoidx = {i["name"] for i in insp.get_indexes("campaignsend")}
    if "uq_campaignsend_crvs" not in uniq and not any(
            i.get("unique") and set(i["column_names"]) ==
            {"campaign_id", "recipient_id", "sequence_version", "step_index"} for i in insp.get_indexes("campaignsend")):
        problems.append("missing unique (campaign,recipient,version,step) constraint")
    have = {i["name"] for i in insp.get_indexes("campaignsend")} | uniq | autoidx
    for name, _t, _c in _GATE_INDEXES:
        if name not in have:
            problems.append(f"missing index {name}")
    if "cred_enc_version" not in {c["name"] for c in insp.get_columns("mailaccount")}:
        problems.append("missing mailaccount.cred_enc_version")
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
    changed = {k: (pre[k], post[k]) for k in ("leads", "outreach", "requests", "campaigns", "suppression",
                                              "bounces") if pre[k] != post[k]}
    if changed:
        print("ABORT: operational counts changed (must be invariant):", changed)
        sys.exit(3)
    problems = _verify()
    print("POST:", post)
    print("index verification:", "OK" if not problems else problems)
    if problems:
        sys.exit(4)
    print("gate migration complete (idempotent).")


if __name__ == "__main__":
    main(dry="--dry-run" in sys.argv)
