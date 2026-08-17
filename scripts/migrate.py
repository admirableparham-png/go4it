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
DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///data.db")


def _db_path():
    """The on-disk SQLite file this migration should ALTER, derived from DATABASE_URL so it matches the
    file the app actually serves (in Docker that's /app/var/go4it.db, not <repo>/data.db). Returns None
    for a non-SQLite URL (Postgres — schema handled by create_all) or an in-memory DB."""
    try:
        from sqlalchemy.engine import make_url
        url = make_url(DATABASE_URL)
    except Exception:  # noqa: BLE001 — fall back to the legacy repo path
        return os.path.join(BASE, "data.db")
    if url.get_backend_name() != "sqlite":
        return None
    path = url.database
    if not path or path == ":memory:":
        return None
    return path if os.path.isabs(path) else os.path.join(BASE, path)


MIGRATIONS = [
    ("lead", "next_action_at", "TIMESTAMP"),
    ("lead", "next_action_note", "VARCHAR DEFAULT ''"),
    ("quote", "share_token", "VARCHAR DEFAULT ''"),
    ("lead", "buyer_replied_at", "TIMESTAMP"),
    ("outreach", "direction", "VARCHAR DEFAULT 'out'"),
    ("outreach", "from_addr", "VARCHAR DEFAULT ''"),
    ("outreach", "message_id", "VARCHAR DEFAULT ''"),
    ("lead", "accepted_at", "TIMESTAMP"),
    ("quote", "accepted_at", "TIMESTAMP"),
    ("quote", "buyer_response", "VARCHAR DEFAULT ''"),
    ("ratecard", "dest_country", "VARCHAR DEFAULT ''"),
    ("quote", "owner_id", "INTEGER"),          # tenant scope; backfilled from lead.owner_id below
    ("servicerequest", "result_file_path", "VARCHAR DEFAULT ''"),   # Phase 3 file delivery
    ("servicerequest", "result_url", "VARCHAR DEFAULT ''"),
]


def run():
    db = _db_path()
    if db is None:
        print("non-SQLite (or in-memory) DATABASE_URL — additive migrations handled by create_all; skipping")
        return
    if not os.path.exists(db):
        print(f"no DB yet at {db} — create_all will include new columns on first run")
        return
    con = sqlite3.connect(db)
    cur = con.cursor()
    applied = 0
    for table, column, decl in MIGRATIONS:
        cols = {r[1] for r in cur.execute(f"PRAGMA table_info({table})")}
        if column not in cols:
            cur.execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")
            print(f"+ {table}.{column}")
            applied += 1
    # Backfill quote.owner_id from the owning lead (tenant scope) for any rows still NULL — so pre-existing
    # quotes are correctly isolated the moment scoping goes live. Idempotent (only touches NULLs).
    qcols = {r[1] for r in cur.execute("PRAGMA table_info(quote)")}
    if "owner_id" in qcols:
        cur.execute("UPDATE quote SET owner_id = (SELECT owner_id FROM lead WHERE lead.id = quote.lead_id) "
                    "WHERE owner_id IS NULL")
        if cur.rowcount:
            print(f"~ backfilled quote.owner_id for {cur.rowcount} row(s)")

    # Indexes the models declare (Field(index=True)) that ALTER TABLE ADD COLUMN doesn't create.
    # create_all() makes them on fresh DBs; migrated DBs need them here to match (else full scans).
    tables = {r[0] for r in cur.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    for idx, table, column in [("ix_quote_share_token", "quote", "share_token"),
                               ("ix_outreach_message_id", "outreach", "message_id"),
                               ("ix_quote_owner_id", "quote", "owner_id")]:
        if table in tables:
            cur.execute(f"CREATE INDEX IF NOT EXISTS {idx} ON {table}({column})")
    con.commit()
    con.close()
    print(f"migrations applied: {applied}")


if __name__ == "__main__":
    run()
