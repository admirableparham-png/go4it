"""Phase 11 — optional automatic backups from the worker: off unless BACKUP_INTERVAL is set, write the same verified
online snapshot as scripts/backup_db.py, and a failure is alerted but never stops the worker."""
import sqlite3

import app.worker as worker
from scripts import backup_db


def test_off_by_default():
    assert worker.BACKUP_INTERVAL == 0


def test_writes_a_verified_snapshot(tmp_path, monkeypatch, capsys):
    db = tmp_path / "live.db"
    con = sqlite3.connect(db)
    con.execute("CREATE TABLE lead (id INTEGER PRIMARY KEY)")
    con.execute("INSERT INTO lead VALUES (1)")
    con.commit(); con.close()
    monkeypatch.setattr(backup_db, "DATABASE_URL", f"sqlite:///{db}")
    monkeypatch.setattr(backup_db, "OUT", str(tmp_path / "backups"))
    assert worker.run_backup() == {"ok": True}
    snaps = list((tmp_path / "backups").glob("data-*.db"))
    assert len(snaps) == 1 and "integrity_check=ok" in capsys.readouterr().out


def test_a_failed_backup_alerts_and_never_raises(monkeypatch):
    def bad():
        raise SystemExit("BACKUP INTEGRITY FAILED: malformed")
    alerts = []
    monkeypatch.setattr(backup_db, "run", bad)
    monkeypatch.setattr(worker, "send_message", lambda m: alerts.append(m))
    out = worker.run_backup()
    assert out["ok"] is False and "INTEGRITY" in out["error"] and alerts
