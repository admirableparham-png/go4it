"""Lightweight idempotent migrations for the SQLite DB.

    ./.venv/bin/python scripts/migrate.py     (or: make db-migrate)

SQLModel's create_all() creates missing TABLES but never ALTERs existing ones, so new columns on
an existing table need this. Each entry is (table, column, sqlite_type_with_default); adding a
column that already exists is skipped. Safe to run repeatedly. Run after pulling changes that add
columns, before starting the app.
"""
import os
import sqlite3

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB = os.path.join(BASE, "data.db")

MIGRATIONS = [
    ("lead", "next_action_at", "TIMESTAMP"),
    ("lead", "next_action_note", "VARCHAR DEFAULT ''"),
    ("quote", "share_token", "VARCHAR DEFAULT ''"),
    ("lead", "buyer_replied_at", "TIMESTAMP"),
    ("outreach", "direction", "VARCHAR DEFAULT 'out'"),
    ("outreach", "from_addr", "VARCHAR DEFAULT ''"),
    ("outreach", "message_id", "VARCHAR DEFAULT ''"),
]


def run():
    if not os.path.exists(DB):
        print("no data.db yet — create_all will include new columns on first run")
        return
    con = sqlite3.connect(DB)
    cur = con.cursor()
    applied = 0
    for table, column, decl in MIGRATIONS:
        cols = {r[1] for r in cur.execute(f"PRAGMA table_info({table})")}
        if column not in cols:
            cur.execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")
            print(f"+ {table}.{column}")
            applied += 1
    # Indexes the models declare (Field(index=True)) that ALTER TABLE ADD COLUMN doesn't create.
    # create_all() makes them on fresh DBs; migrated DBs need them here to match (else full scans).
    tables = {r[0] for r in cur.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    for idx, table, column in [("ix_quote_share_token", "quote", "share_token"),
                               ("ix_outreach_message_id", "outreach", "message_id")]:
        if table in tables:
            cur.execute(f"CREATE INDEX IF NOT EXISTS {idx} ON {table}({column})")
    con.commit()
    con.close()
    print(f"migrations applied: {applied}")


if __name__ == "__main__":
    run()
