"""Phase 8 — analytics aggregates, the commercial funnel, and the immutable snapshot cache.

Everything here is READ-ONLY over the operational tables (a GET never mutates business data). Aggregates use
indexed COUNT/GROUP-BY and bounded windows — never a whole-table payload. Money stays PER CURRENCY. The funnel
is computed from real states/events; a negative reply is an engaged record but NEVER enters a positive-interest
stage. AnalyticsSnapshots are immutable (history + a fallback when live compute fails) and are written by the
worker/report paths, not by dashboard GETs.
"""
import json
from datetime import datetime, timedelta

from sqlmodel import func, select

from . import metrics as M
from .models import AnalyticsSnapshot, Deal, Lead, Shipment, WorkItem

# The ONE commercial funnel. Each stage maps to a deterministic metric. Counts are of UNIQUE LEADS/quotes/deals
# that REACHED the stage within the window — not a single cohort tracked forward, and not message counts.
FUNNEL = [
    ("research_prospect", "Research prospect", "researched_prospects"),
    ("contactable", "Contactable", "contactable_prospects"),
    ("contacted", "Contacted", "contacted"),
    ("human_reply", "Human reply", "human_replies"),
    ("positive_interest", "Positive interest", "positive_replies"),
    ("quote_sent", "Quote sent", "quotes_sent"),
    ("quote_accepted", "Quote accepted", "quotes_accepted"),
    ("deal", "Deal", "deals_opened"),
    ("delivered", "Delivered", "deals_delivered"),
    ("settled", "Settled", "deals_settled"),
]
FUNNEL_MIN_SAMPLE = 5   # below this, a conversion % is not shown (insufficient data)


def funnel(session, *, since=None, until=None, owner_id=None):
    """Return the funnel stages with counts + stage-to-stage conversion. Negative replies are excluded from the
    positive-interest stage by construction (it counts reply_outcome='positive' only). Conversions are suppressed
    where the previous stage is below the minimum sample."""
    rows = []
    prev = None
    for key, label, metric in FUNNEL:
        count = M.compute(metric, session, since=since, until=until, owner_id=owner_id)
        conv = None
        insufficient = False
        if prev is not None:
            if prev < FUNNEL_MIN_SAMPLE:
                insufficient = True
            elif prev > 0:
                conv = round(count / prev * 100, 1)
        rows.append({"key": key, "label": label, "count": count, "conversion": conv,
                     "insufficient": insufficient})
        prev = count
    return {"stages": rows,
            "basis": "Counts of unique leads/quotes/deals that reached each stage within the selected window "
                     "(not a single cohort tracked forward). Counts are per lead/quote/deal, not per message.",
            "cohort_window": _range_label(since, until)}


def replies_by_outcome(session, *, since=None, until=None, owner_id=None):
    """Human replies split by admin-confirmed outcome. Opens/deliveries/bounces are not replies and are absent."""
    stmt = select(Lead.reply_outcome, func.count()).where(Lead.buyer_replied_at != None)  # noqa: E711
    if owner_id is not None:
        stmt = stmt.where(Lead.owner_id == owner_id)
    if since is not None:
        stmt = stmt.where(Lead.buyer_replied_at >= since)
    if until is not None:
        stmt = stmt.where(Lead.buyer_replied_at <= until)
    out = {"positive": 0, "negative": 0, "neutral": 0, "auto_reply": 0, "bounced": 0}
    for outcome, n in session.exec(stmt.group_by(Lead.reply_outcome)).all():
        out[outcome or "neutral"] = out.get(outcome or "neutral", 0) + n
    return out


def deals_by_stage(session, *, owner_id=None):
    stmt = select(Deal.stage, func.count())
    if owner_id is not None:
        stmt = stmt.where(Deal.owner_id == owner_id)
    return {k: v for k, v in session.exec(stmt.group_by(Deal.stage)).all()}


def shipments_by_stage(session):
    return {k: v for k, v in session.exec(
        select(Shipment.current_milestone, func.count()).group_by(Shipment.current_milestone)).all()}


def work_queue_by(session, dim="priority"):
    col = WorkItem.priority if dim == "priority" else WorkItem.type
    stmt = select(col, func.count()).where(WorkItem.status.in_(("open", "in_progress", "waiting")))
    return {k: v for k, v in session.exec(stmt.group_by(col)).all()}


def dashboard_kpis(session, *, owner_id=None, since=None, until=None):
    """The dashboard KPI set — computed from the metric registry so labels/definitions are consistent."""
    keys = ["active_requests", "open_work_queue", "overdue_work_items", "researched_prospects",
            "contactable_prospects", "human_replies", "positive_replies", "quotes_awaiting",
            "quotes_accepted", "active_deals", "active_shipments", "operational_exceptions"]
    kpis = {}
    for k in keys:
        try:
            kpis[k] = {"value": M.compute(k, session, since=since, until=until, owner_id=owner_id),
                       "def": M.definition(k)}
        except Exception:  # noqa: BLE001 — a single metric failure never blocks the dashboard
            kpis[k] = {"value": None, "def": M.definition(k), "error": True}
    kpis["settled_value"] = {"value": M.compute("settled_value", session, since=since, until=until,
                                                owner_id=owner_id), "def": M.definition("settled_value")}
    return kpis


# --------------------------------------------------------------------- period comparison
def previous_period(since, until):
    """The immediately-preceding equal-length window, or (None, None) if the window is open-ended."""
    if since is None or until is None:
        return None, None
    span = until - since
    return since - span, since


def compare(session, metric_key, *, since, until, owner_id=None):
    """(current, previous, delta_pct) for a metric — only meaningful when both windows have data. Returns
    previous=None when the window is open-ended so the caller can hide the comparison."""
    cur = M.compute(metric_key, session, since=since, until=until, owner_id=owner_id)
    p_since, p_until = previous_period(since, until)
    if p_since is None or not isinstance(cur, int):
        return cur, None, None
    prev = M.compute(metric_key, session, since=p_since, until=p_until, owner_id=owner_id)
    delta = round((cur - prev) / prev * 100, 1) if prev else None
    return cur, prev, delta


# --------------------------------------------------------------------- immutable snapshot cache
def _range_label(since, until):
    if since is None and until is None:
        return "all-time"
    return f"{since:%Y-%m-%d}..{until:%Y-%m-%d}" if since and until else "open"


def cache_key(kind, *, tenant_id=None, time_range="", filters=None):
    """A tenant-scoped cache key with NO sensitive identifiers (only tenant/owner scope, range, and coarse
    filters like country/category/product — never a buyer/company id)."""
    f = "&".join(f"{k}={v}" for k, v in sorted((filters or {}).items()) if v not in (None, ""))
    return f"{kind}|t={tenant_id if tenant_id is not None else 'global'}|r={time_range}|{f}"


def write_snapshot(session, *, kind, key, result, tenant_id=None, time_range="", filters=None,
                   source_freshness=None, scoring_version="", derived=True, inferred=False, now=None):
    """Persist an IMMUTABLE snapshot (history + fallback). Best-effort: a write failure never propagates."""
    now = now or datetime.utcnow()
    try:
        snap = AnalyticsSnapshot(
            kind=kind, cache_key=key, metric_version=M.METRIC_VERSION, scoring_version=scoring_version,
            time_range=time_range, filters=json.dumps(filters or {})[:2000],
            source_freshness=json.dumps(source_freshness or {})[:2000], result=json.dumps(result)[:100000],
            tenant_id=tenant_id, derived=derived, inferred=inferred, generated_at=now)
        session.add(snap)
        session.flush()
        return snap
    except Exception:  # noqa: BLE001
        return None


def latest_snapshot(session, kind, key, *, tenant_id=None):
    snap = session.exec(select(AnalyticsSnapshot).where(
        AnalyticsSnapshot.kind == kind, AnalyticsSnapshot.cache_key == key)
        .order_by(AnalyticsSnapshot.id.desc())).first()
    return snap


def cached(session, *, kind, key, compute_fn, ttl_minutes=30, tenant_id=None, time_range="", filters=None,
           now=None):
    """Serve `kind`/`key` from a fresh snapshot if one exists within TTL; else compute live and (best-effort)
    persist a snapshot. If live compute RAISES, fall back to the most recent snapshot with stale=True so the
    dashboard never goes dark. Returns {result, generated_at, stale, cached}."""
    now = now or datetime.utcnow()
    snap = latest_snapshot(session, kind, key, tenant_id=tenant_id)
    if snap and (now - snap.generated_at) <= timedelta(minutes=ttl_minutes):
        return {"result": json.loads(snap.result or "null"), "generated_at": snap.generated_at,
                "stale": False, "cached": True}
    try:
        result = compute_fn()
        write_snapshot(session, kind=kind, key=key, result=result, tenant_id=tenant_id,
                       time_range=time_range, filters=filters, now=now)
        return {"result": result, "generated_at": now, "stale": False, "cached": False}
    except Exception:  # noqa: BLE001
        if snap:
            return {"result": json.loads(snap.result or "null"), "generated_at": snap.generated_at,
                    "stale": True, "cached": True}
        raise
