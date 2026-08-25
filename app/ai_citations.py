"""Phase 9 — citation builders. Every material claim gets a citation to a real record; nothing is fabricated.

A citation is a safe reference (record type, ref, timestamp, source, freshness, provenance class, authorized
link). Metric citations carry the metric registry's definition/version so the copilot can state exactly what it
measured.
"""
from datetime import datetime

from . import metrics as M
from .models import AICitation


def metric_citation(key, *, time_range="", now=None):
    """A citation for a metric answer — cites the registry definition + version (the authoritative source)."""
    d = M.definition(key)
    return {"record_type": "metric", "record_ref": f"metric:{key}", "record_id": None,
            "record_at": now or datetime.utcnow(), "source": d["source_tables"] or "metric registry",
            "freshness": "Current", "provenance_class": "derived", "link": "",
            "meta": {"definition": d["definition"], "unit": d["unit"], "metric_version": d["metric_version"],
                     "range": time_range, "min_sample": d["min_sample"]}}


def record_citation(entity, row, *, link=""):
    """A citation from a structured-search projection row (which carries _ref/_id)."""
    return {"record_type": entity.rstrip("s"), "record_ref": row.get("_ref", ""), "record_id": row.get("_id"),
            "record_at": _row_time(row), "source": row.get("source", entity), "freshness": "",
            "provenance_class": _prov(row), "link": link}


def _row_time(row):
    for k in ("created_at", "observed_at", "collected_at", "record_at"):
        if row.get(k):
            v = row[k]
            return v if isinstance(v, datetime) else None
    return None


def _prov(row):
    v = row.get("verification") or row.get("verification_state") or ""
    return v if v in ("observed", "verified", "derived", "inferred") else "observed"


def persist(session, *, conversation_id, message_id, citations):
    """Write AICitation rows for a message and return the count."""
    n = 0
    for c in citations or []:
        session.add(AICitation(
            message_id=message_id, conversation_id=conversation_id,
            record_type=c.get("record_type", ""), record_ref=c.get("record_ref", ""),
            record_id=c.get("record_id"), record_at=c.get("record_at"), source=c.get("source", ""),
            freshness=c.get("freshness", ""), provenance_class=c.get("provenance_class", ""),
            link=c.get("link", "")))
        n += 1
    return n
