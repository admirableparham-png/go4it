"""Timestamped, online-consistent SQLite backup.

    ./.venv/bin/python scripts/backup_db.py     (or: make backup)

Uses sqlite3's online .backup API (safe even with WAL + a live writer), writes to ./backups/
(gitignored), and keeps the last KEEP copies (BACKUP_KEEP, default 14). save.sh calls this before each daily commit so
the operational data (leads/quotes/deals) always has a recovery point independent of git. For real
safety, sync ./backups to an offsite location (make pull-backup / Dropbox / rclone / S3).

Each snapshot holds buyer contact data, so it is owner-only (0600) from the moment it is created, and it is ONE
self-contained file (rollback journal, no -wal/-shm companions next to it).
"""
import glob
import os
import sqlite3
import sys
from datetime import datetime

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)
try:
    from app.config import DATABASE_URL
except Exception:  # noqa: BLE001
    DATABASE_URL = "sqlite:///data.db"


def _keep() -> int:
    try:
        return max(1, int(os.getenv("BACKUP_KEEP", "14")))
    except ValueError:
        return 14


OUT = os.path.join(BASE, "backups")
KEEP = _keep()
_COMPANIONS = ("-wal", "-shm", "-journal")


def _db_path():
    """Resolve the SQLite file from DATABASE_URL (None for Postgres -> use pg_dump instead)."""
    if not DATABASE_URL.startswith("sqlite"):
        return None
    raw = DATABASE_URL.split("sqlite:///", 1)[-1]      # rel: 'data.db' | abs: '/data/x.db'
    return raw if os.path.isabs(raw) else os.path.join(BASE, raw)


def _remove(path):
    try:
        os.remove(path)
    except FileNotFoundError:
        pass


def _prune():
    """Keep the newest KEEP snapshots (with any companion files); drop companions whose snapshot is gone; make older
    snapshots owner-only too (best effort — a file owned by another user is left as is)."""
    snaps = sorted(glob.glob(os.path.join(OUT, "data-*.db")))
    for old in snaps[:-KEEP]:
        for f in (old,) + tuple(old + s for s in _COMPANIONS):
            _remove(f)
        print(f"pruned {old}")
    for comp in glob.glob(os.path.join(OUT, "data-*.db-*")):
        if comp.endswith(_COMPANIONS) and not os.path.exists(comp.rsplit("-", 1)[0]):
            _remove(comp)
    for snap in snaps[-KEEP:]:
        try:
            if os.stat(snap).st_mode & 0o077:
                os.chmod(snap, 0o600)
        except OSError:
            pass


def run():
    DB = _db_path()
    if DB is None:
        print("DATABASE_URL is not SQLite — use pg_dump for Postgres backups")
        return
    if not os.path.exists(DB):
        print(f"no database at {DB} to back up")
        return
    os.makedirs(OUT, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    dest = os.path.join(OUT, f"data-{stamp}.db")
    os.close(os.open(dest, os.O_WRONLY | os.O_CREAT, 0o600))   # owner-only BEFORE any buyer data goes in
    src = sqlite3.connect(DB)
    dst = sqlite3.connect(dest)
    try:
        with dst:
            src.backup(dst)            # online-consistent snapshot (SQLite backup API — WAL-safe, no raw copy)
    finally:
        src.close()
        dst.close()
    os.chmod(dest, 0o600)
    print(f"backup -> {dest} ({os.path.getsize(dest):,} bytes) via SQLite online backup API")
    # prove the snapshot is a valid, restorable database (not a torn copy): open it and integrity-check.
    chk = sqlite3.connect(dest)
    try:
        # the backup copies the live DB's WAL flag: switch the SNAPSHOT to a rollback journal so it stays one
        # self-contained file (no -wal/-shm left beside it — they were orphaned when old snapshots were pruned)
        try:
            mode = chk.execute("PRAGMA journal_mode=DELETE").fetchone()[0]
        except sqlite3.DatabaseError:
            mode = ""                  # a damaged snapshot: the integrity check below reports it
        integrity = chk.execute("PRAGMA integrity_check").fetchone()[0]
        ntables = chk.execute("SELECT count(*) FROM sqlite_master WHERE type='table'").fetchone()[0]
        has_lead = chk.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='lead'").fetchone()
        nleads = chk.execute("SELECT count(*) FROM lead").fetchone()[0] if has_lead else "n/a"
    finally:
        chk.close()
    if mode == "delete":               # now one file: a leftover -wal/-shm is stale (macOS's SQLite keeps the -shm)
        for sfx in ("-wal", "-shm"):
            _remove(dest + sfx)
    print(f"restore-check: integrity_check={integrity}, tables={ntables}, lead_rows={nleads}")
    if integrity != "ok":
        raise SystemExit(f"BACKUP INTEGRITY FAILED: {integrity} - do NOT proceed with the migration")
    _prune()


if __name__ == "__main__":
    run()
