"""Phase 8 — data-source registry + live freshness/health.

Answers "which data sources are fresh and reliable?" honestly. A source's health is computed from the real
`IngestionRun` history and `Provenance` timestamps — data is NEVER labelled "live"/"Current" unless it was
actually refreshed inside its freshness window through a real path. A source with no configured integration reads
"Not configured", not "Stale".
"""
import os
from dataclasses import dataclass, field
from datetime import datetime

from sqlmodel import func, select

from .models import IngestionRun, Provenance

FRESHNESS = ("Current", "Aging", "Stale", "Failed", "Not configured", "Unknown")


@dataclass
class DataSource:
    key: str
    label: str
    category: str              # research | inbound | import | directory | manual | trade_stats
    freshness_window_h: float  # how long a refresh stays "Current"
    owner: str = "admin"
    configured_env: str = ""   # if set, the source is "Not configured" unless this env var is present
    ingest_sources: tuple = field(default_factory=tuple)   # IngestionRun.source values that feed it
    provenance_types: tuple = field(default_factory=tuple)  # Provenance.source_type values it writes


# The registry — extend as new sources are wired. `configured_env=""` means always-available (manual/import).
SOURCES = [
    DataSource("email_inbound", "Email inbound (replies)", "inbound", 24, configured_env="IMAP_HOST",
               ingest_sources=("email-inbound",)),
    DataSource("research_agents", "Research agents (trade stats)", "trade_stats", 24 * 30,
               configured_env="COMTRADE_API_KEY", provenance_types=("research_agent",)),
    DataSource("command_harvest", "Command harvests", "research", 24 * 7,
               provenance_types=("command",)),
    DataSource("csv_import", "CSV / file imports", "import", 24 * 90, provenance_types=("csv_import",)),
    DataSource("seller_requests", "Seller requests", "manual", 24 * 30, provenance_types=("seller_request",)),
    DataSource("directory", "Directories / marketplaces", "directory", 24 * 60,
               provenance_types=("directory", "marketplace")),
    DataSource("tender_rfq", "Tenders / RFQs", "import", 24 * 30, provenance_types=("tender_rfq",)),
    DataSource("customs", "Customs / import trends", "trade_stats", 24 * 60, provenance_types=("customs",)),
]
BY_KEY = {s.key: s for s in SOURCES}


def _configured(src: DataSource) -> bool:
    return not src.configured_env or bool(os.environ.get(src.configured_env, "").strip())


def _label(src, configured, last_success, last_failure, now):
    if not configured:
        return "Not configured"
    if last_failure and (not last_success or last_failure > last_success):
        return "Failed"
    if last_success is None:
        return "Unknown"
    age_h = (now - last_success).total_seconds() / 3600.0
    if age_h <= src.freshness_window_h:
        return "Current"
    if age_h <= src.freshness_window_h * 3:
        return "Aging"
    return "Stale"


def source_health(session, now=None) -> list:
    """Per-source health: last success/failure, records/duplicates/rejected, freshness label, configured flag,
    owner, error summary. Never invents freshness."""
    now = now or datetime.utcnow()
    out = []
    for src in SOURCES:
        configured = _configured(src)
        last_success = last_failure = None
        received = duplicates = new = 0
        err = ""
        if src.ingest_sources:
            runs = session.exec(select(IngestionRun).where(IngestionRun.source.in_(src.ingest_sources))
                                .order_by(IngestionRun.id.desc()).limit(200)).all()
            for r in runs:
                received += r.leads_seen or 0
                duplicates += r.leads_duplicate or 0
                new += r.leads_new or 0
                if r.status == "ok" and r.finished_at and (last_success is None or r.finished_at > last_success):
                    last_success = r.finished_at
                if r.status == "error" and r.finished_at and (last_failure is None or r.finished_at > last_failure):
                    last_failure = r.finished_at
                    err = err or (r.error or "")[:200]
            if last_success is None:
                # a long outage pushed every success out of the 200-run window: anchor on the REAL last success, so
                # the outage stays one episode (its Work Queue alert isn't raised again as 'never')
                last_success = session.exec(select(IngestionRun.finished_at).where(
                    IngestionRun.source.in_(src.ingest_sources), IngestionRun.status == "ok",
                    IngestionRun.finished_at.is_not(None)).order_by(IngestionRun.id.desc()).limit(1)).first()
        if src.provenance_types:
            # provenance last_seen is the freshness anchor for sources that don't run through IngestionRun
            pv = session.exec(select(func.max(Provenance.last_seen_at), func.count())
                              .where(Provenance.source_type.in_(src.provenance_types))).one()
            pmax, pcount = pv
            received += pcount or 0
            if pmax and (last_success is None or pmax > last_success):
                last_success = pmax
        rejected = max(0, received - new - duplicates) if src.ingest_sources else 0
        out.append({
            "key": src.key, "label": src.label, "category": src.category, "owner": src.owner,
            "configured": configured, "freshness_window_h": src.freshness_window_h,
            "last_success": last_success, "last_failure": last_failure,
            "records_received": received, "duplicates": duplicates, "rejected": rejected,
            "error": err, "freshness": _label(src, configured, last_success, last_failure, now),
        })
    return out
