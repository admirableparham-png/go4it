"""Phase 8 — admin-only in-app intelligence alerts.

Alerts are idempotent on (alert_key, condition_version): the same unchanged condition never spawns a duplicate;
a material change (a new condition_version) may create a fresh one. Cadence (daily/weekly/monthly/seasonal/
custom) drives WHEN a scheduled alert is eligible to fire, honouring the alert's timezone for the local-hour
decision while every STORED timestamp stays naive UTC. No alert ever sends an automatic external email or
triggers outreach — they live only in the admin UI.
"""
from datetime import datetime

from sqlalchemy.exc import IntegrityError
from sqlmodel import select

from .models import IntelAlert
from .pipeline import audit

ALERT_TYPES = ("new_high_demand", "rising_category", "market_spike", "requirement_no_supply",
               "match_no_outreach", "seasonal_window", "expiring_opportunity", "stale_data",
               "source_failure", "data_anomaly")
CADENCES = ("daily", "weekly", "monthly", "seasonal", "custom")
STATUSES = ("new", "reviewed", "snoozed", "dismissed")
FIRE_HOUR = 8   # scheduled alerts fire at 08:00 local time


def raise_alert(session, *, alert_type, alert_key, condition_version, severity="info", title="", body="",
                related_opportunity_id=None, owner_id=None, cadence="custom", schedule_tz="UTC", actor=None,
                now=None):
    """Create an alert idempotently. Returns (alert, created). A duplicate (alert_key, condition_version) is a
    no-op that returns the existing row."""
    now = now or datetime.utcnow()
    existing = session.exec(select(IntelAlert).where(
        IntelAlert.alert_key == alert_key, IntelAlert.condition_version == condition_version)).first()
    if existing:
        return existing, False
    a = IntelAlert(alert_type=alert_type, alert_key=alert_key, condition_version=condition_version,
                   severity=severity, title=title, body=body, related_opportunity_id=related_opportunity_id,
                   owner_id=owner_id, cadence=cadence if cadence in CADENCES else "custom",
                   schedule_tz=schedule_tz or "UTC", status="new", created_at=now, updated_at=now)
    session.add(a)
    try:
        session.flush()
    except IntegrityError:
        session.rollback()
        again = session.exec(select(IntelAlert).where(
            IntelAlert.alert_key == alert_key, IntelAlert.condition_version == condition_version)).first()
        return again, False
    audit(session, actor, "intel_alert", a.id, "alert_raised",
          {"type": alert_type, "severity": severity}, tenant_id=owner_id)
    return a, True


def set_status(session, alert: IntelAlert, status: str, *, snooze_until=None, actor=None, now=None):
    now = now or datetime.utcnow()
    if status not in STATUSES:
        return False, "unknown status"
    alert.status = status
    alert.snooze_until = snooze_until if status == "snoozed" else None
    alert.updated_at = now
    session.add(alert)
    audit(session, actor, "intel_alert", alert.id, "alert_status_change", {"status": status},
          tenant_id=alert.owner_id)
    return True, ""


def active_alerts(session, *, now=None):
    """Alerts an admin should see now: 'new' plus snoozed ones whose snooze has elapsed. Dismissed/reviewed are
    hidden."""
    now = now or datetime.utcnow()
    rows = session.exec(select(IntelAlert).where(IntelAlert.status.in_(("new", "snoozed")))
                        .order_by(IntelAlert.id.desc())).all()
    return [a for a in rows if a.status == "new" or (a.snooze_until and now >= a.snooze_until)]


# --------------------------------------------------------------------- timezone-aware scheduling
def _local(now_utc, tz):
    """Local wall-clock for a naive-UTC timestamp in the named tz. Falls back to UTC if the tz is unavailable
    (never raises — scheduling must not crash on a missing tzdata)."""
    try:
        from datetime import timezone
        from zoneinfo import ZoneInfo
        return now_utc.replace(tzinfo=timezone.utc).astimezone(ZoneInfo(tz))
    except Exception:  # noqa: BLE001
        return now_utc


def schedule_fires(cadence, schedule_tz, now_utc, *, fire_hour=FIRE_HOUR):
    """Is `now_utc` inside the fire window for `cadence` in `schedule_tz`? Timezone-aware: 08:00 in Tehran and
    08:00 in Tbilisi are different UTC instants. 'custom'/'seasonal' are event-driven, not clock-scheduled."""
    if cadence in ("custom", "seasonal"):
        return True
    loc = _local(now_utc, schedule_tz)
    if loc.hour != fire_hour:
        return False
    if cadence == "daily":
        return True
    if cadence == "weekly":
        return loc.weekday() == 0        # Monday
    if cadence == "monthly":
        return loc.day == 1
    return False
