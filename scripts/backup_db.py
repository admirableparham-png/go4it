"""Timestamped, online-consistent SQLite backup.

    ./.venv/bin/python scripts/backup_db.py     (or: make backup)

Uses sqlite3's online .backup API (safe even with WAL + a live writer), writes to ./backups/
(gitignored), and keeps the last KEEP copies. save.sh calls this before each daily commit so the
operational data (leads/quotes/deals) always has a recovery point independent of git. For real
safety, sync ./backups to an offsite location (Dropbox/rclone/S3).
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

OUT = os.path.join(BASE, "backups")
KEEP = 14


def _db_path():
    """Resolve the SQLite file from DATABASE_URL (None for Postgres -> use pg_dump instead)."""
    if not DATABASE_URL.startswith("sqlite"):
        return None
    raw = DATABASE_URL.split("sqlite:///", 1)[-1]      # rel: 'data.db' | abs: '/data/x.db'
    return raw if os.path.isabs(raw) else os.path.join(BASE, raw)


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
    src = sqlite3.connect(DB)
    dst = sqlite3.connect(dest)
    try:
        with dst:
            src.backup(dst)            # online-consistent snapshot
    finally:
        src.close()
        dst.close()
    print(f"backup -> {dest} ({os.path.getsize(dest):,} bytes)")
    for old in sorted(glob.glob(os.path.join(OUT, "data-*.db")))[:-KEEP]:
        os.remove(old)
        print(f"pruned {old}")


if __name__ == "__main__":
    run()
