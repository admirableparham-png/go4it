"""Phase 8 (B) — alert idempotency + tz-aware scheduling, and report generation (per-currency separation,
no PII, audit, private storage)."""
from datetime import datetime, timedelta

from sqlmodel import Session, select

from app import alerts as AL
from app import reports as R
from app.models import AnalyticsReport, AuditLog, IntelAlert, Settlement, User


def _admin(s):
    return s.exec(select(User).where(User.email == "admin@t.local")).one()


def test_alert_idempotent_on_key_and_version(ops_engine):
    with Session(ops_engine) as s:
        a1, c1 = AL.raise_alert(s, alert_type="new_high_demand", alert_key="opp:1",
                                condition_version="s70", title="Demand up"); s.commit()
        a2, c2 = AL.raise_alert(s, alert_type="new_high_demand", alert_key="opp:1",
                                condition_version="s70", title="Demand up (again)"); s.commit()
        assert c1 is True and c2 is False and a1.id == a2.id      # unchanged condition → no duplicate
        # a material change (new condition_version) MAY create a fresh alert
        a3, c3 = AL.raise_alert(s, alert_type="new_high_demand", alert_key="opp:1",
                                condition_version="s90", title="Demand higher"); s.commit()
        assert c3 is True and a3.id != a1.id
        assert len(s.exec(select(IntelAlert)).all()) == 2


def test_alert_scheduling_is_timezone_aware():
    monday_08z = datetime(2026, 1, 5, 8, 0)   # Monday 08:00 UTC
    assert AL.schedule_fires("daily", "UTC", monday_08z) is True
    assert AL.schedule_fires("daily", "Asia/Tehran", monday_08z) is False   # not 08:00 local in Tehran
    assert AL.schedule_fires("weekly", "UTC", monday_08z) is True           # Monday
    assert AL.schedule_fires("monthly", "UTC", monday_08z) is False         # not day 1
    assert AL.schedule_fires("custom", "UTC", monday_08z) is True           # event-driven, always eligible


def test_alert_snooze_and_active(ops_engine):
    with Session(ops_engine) as s:
        a, _ = AL.raise_alert(s, alert_type="stale_data", alert_key="src:x", condition_version="Stale",
                              title="Stale"); s.commit()
        AL.set_status(s, a, "snoozed", snooze_until=datetime.utcnow() + timedelta(days=1)); s.commit()
        assert AL.active_alerts(s) == []                          # snoozed → hidden until it elapses
        AL.set_status(s, a, "dismissed"); s.commit()
        assert a.status == "dismissed"


def test_report_csv_per_currency_and_no_pii(ops_engine, tmp_path):
    with Session(ops_engine) as s:
        admin = _admin(s)
        s.add(Settlement(deal_id=1, revenue="1000", currency="USD", settlement_date=datetime.utcnow()))
        s.add(Settlement(deal_id=2, revenue="500", currency="EUR", settlement_date=datetime.utcnow()))
        s.commit()
        rpt, err = R.generate(s, report_type="weekly_exec", files_dir=tmp_path, fmt="csv", actor=admin)
        s.commit()
        assert err == "" and rpt.status == "generated" and rpt.reference.startswith("RPT-")
        body = (tmp_path / rpt.file_path).read_text()
        # currencies are on separate rows, never summed into one figure
        assert "settled_value[USD]" in body and "settled_value[EUR]" in body and "1500" not in body
        # aggregate only — no lead/buyer identity columns
        assert "buyer_company" not in body and "@" not in body
        # generation was audited
        assert s.exec(select(AuditLog).where(AuditLog.action == "report_generated")).first() is not None


def test_report_pdf_degrades_without_chromium(ops_engine, tmp_path, monkeypatch):
    # simulate no sandbox: render_pdf returns (False, err) -> report marked failed + a Work Queue task, no crash
    from app import pdf_render as PDF
    monkeypatch.setattr(PDF, "render_pdf", lambda html, out, timeout_ms=20000: (False, "no chromium"))
    with Session(ops_engine) as s:
        admin = _admin(s)
        rpt, err = R.generate(s, report_type="funnel", files_dir=tmp_path, fmt="pdf", actor=admin); s.commit()
        assert rpt.status == "failed" and err
        from app.models import WorkItem
        assert s.exec(select(WorkItem).where(WorkItem.type == "report_generation_failed")).first() is not None
