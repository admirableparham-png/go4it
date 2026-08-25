"""Phase 8 — observation classification + provenance/freshness helpers.

Every intelligence number must be honest about HOW it is known. An observation is one of:
  * Observed — a directly recorded event (a reply arrived, a quote was accepted).
  * Verified — confirmed by an authorized admin or a trusted integration.
  * Derived — calculated deterministically from observed records.
  * Inferred — reconstructed from incomplete historical evidence (backfill).
Inferred history is NEVER represented as verified.
"""
from datetime import datetime

from sqlmodel import select

from .models import Provenance

CLASSES = ("observed", "verified", "derived", "inferred")
_LABEL = {"observed": "Observed", "verified": "Verified", "derived": "Derived", "inferred": "Inferred"}
_DESC = {
    "observed": "Directly recorded event.",
    "verified": "Confirmed by an authorized admin or trusted integration.",
    "derived": "Calculated deterministically from observed records.",
    "inferred": "Reconstructed from incomplete historical evidence — never treated as verified.",
}


def classify(verification_state: str) -> dict:
    v = verification_state if verification_state in CLASSES else "observed"
    return {"class": v, "label": _LABEL[v], "detail": _DESC[v]}


def provenance_summary(session, entity_type: str, entity_id: int) -> list:
    """The provenance rows behind a company/contact — source, when collected, when last seen, inferred flag."""
    rows = session.exec(select(Provenance).where(Provenance.entity_type == entity_type,
                                                 Provenance.entity_id == entity_id)).all()
    return [{"source_type": p.source_type, "source_name": p.source_name, "source_ref": p.source_ref,
             "collected_at": p.collected_at, "last_seen_at": p.last_seen_at, "inferred": p.inferred}
            for p in rows]


# freshness thresholds are per-source (see data_sources); this is the generic classifier
def freshness(ts, window_hours: float, now=None) -> str:
    """Current | Aging | Stale | Unknown for a single timestamp against a freshness window."""
    if ts is None:
        return "Unknown"
    now = now or datetime.utcnow()
    age_h = (now - ts).total_seconds() / 3600.0
    if age_h <= window_hours:
        return "Current"
    if age_h <= window_hours * 3:
        return "Aging"
    return "Stale"
