"""Phase 9 — the admin intelligence brief.

A daily/weekly, admin-only, IN-APP brief built from the Phase-8 metric registry. Every section cites its
evidence. It is never auto-emailed (delivery is in-app only).
"""
from datetime import datetime, timedelta

from sqlmodel import func, select

from . import ai_citations as CIT
from . import data_sources as DATASRC
from . import metrics as M
from .models import Opportunity, WorkItem


def _kpi(session, key, since, until):
    return {"key": key, "label": M.definition(key)["label"],
            "value": M.compute(key, session, since=since, until=until),
            "citation": CIT.metric_citation(key)}


def generate_brief(session, *, period="daily", now=None):
    """Return a cited brief. period ∈ daily|weekly. In-app only — the caller never emails it automatically."""
    now = now or datetime.utcnow()
    since = now - timedelta(days=1 if period == "daily" else 7)
    sections = []

    sections.append({"title": "Needs attention", "items": [
        _kpi(session, "overdue_work_items", None, None),
        _kpi(session, "open_work_queue", None, None),
        _kpi(session, "operational_exceptions", None, None),
        _kpi(session, "quotes_awaiting", None, None)]})

    sections.append({"title": "Demand & pipeline", "items": [
        _kpi(session, "positive_replies", since, now),
        _kpi(session, "quotes_accepted", since, now),
        _kpi(session, "active_deals", None, None)]})

    # high-confidence opportunities + missing supply (evidence-backed, no buyer identity)
    hi = session.exec(select(Opportunity).where(Opportunity.score >= 70,
                                                Opportunity.status.notin_(("archived", "rejected", "converted")))
                      .order_by(Opportunity.score.desc()).limit(5)).all()
    no_supply = session.exec(select(func.count()).select_from(WorkItem).where(
        WorkItem.type == "high_demand_no_supply", WorkItem.status.in_(("open", "in_progress", "waiting")))).one()
    sections.append({"title": "Opportunities", "items": [
        {"key": "high_confidence", "label": "High-confidence opportunities", "value": len(hi),
         "detail": [{"reference": o.reference, "score": o.score} for o in hi],
         "citations": [{"record_type": "opportunity", "record_ref": o.reference, "record_id": o.id,
                        "record_at": o.created_at, "source": "opportunity", "provenance_class": "derived",
                        "freshness": "", "link": f"/intelligence/opportunities/{o.id}"} for o in hi]},
        {"key": "missing_supply", "label": "Demand without matching supply", "value": no_supply,
         "citation": CIT.metric_citation("positive_replies")}]})

    stale = [s["label"] for s in DATASRC.source_health(session) if s["freshness"] in ("Stale", "Failed")]
    sections.append({"title": "Data sources", "items": [
        {"key": "stale_sources", "label": "Stale/failed sources", "value": len(stale), "detail": stale}]})

    recommendations = []
    if session.exec(select(func.count()).select_from(WorkItem).where(
            WorkItem.status.in_(("open", "in_progress", "waiting")), WorkItem.due_at != None,  # noqa: E711
            WorkItem.due_at < now)).one():
        recommendations.append("Clear overdue work-queue items.")
    if no_supply:
        recommendations.append("Source or verify supply for high-demand opportunities.")
    if stale:
        recommendations.append("Refresh stale/failed data sources.")

    return {"period": period, "generated_at": now.isoformat(), "sections": sections,
            "recommendations": recommendations, "metric_version": M.METRIC_VERSION,
            "delivery": "in_app_only"}
