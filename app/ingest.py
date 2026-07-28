"""Ingestion service: pull RawLeads from a source, normalize, dedup, persist,
and run the match -> quote -> alert chain. Records an IngestionRun for
observability (no silent caps)."""
import logging
from datetime import datetime

from sqlmodel import Session

from .db import engine
from .lead_service import create_lead
from .models import IngestionRun, Lead

logger = logging.getLogger("go4it")


def raw_to_lead(raw, source_name: str) -> Lead:
    return Lead(
        source=source_name,
        external_id=raw.external_id,
        product=raw.product,
        category=raw.category,
        spec=raw.spec,
        quantity=max(raw.quantity, 0.0),
        unit=raw.unit,
        target_price=max(raw.target_price, 0.0),
        currency=raw.currency or "USD",
        dest_country=(raw.dest_country or "").strip().upper(),
        dest_city=raw.dest_city,
        buyer_company=raw.buyer_company,
        contact_name=raw.contact_name,
        email=raw.email,
        phone=raw.phone,
    )


def ingest_source(source) -> dict:
    """Fetch from a LeadSource and persist new leads. Returns counts."""
    with Session(engine) as session:
        run = IngestionRun(source=source.name, started_at=datetime.utcnow(), status="running")
        session.add(run)
        session.commit()
        session.refresh(run)

        seen = new = duplicate = 0
        try:
            for raw in source.fetch():
                seen += 1
                created = create_lead(session, raw_to_lead(raw, source.name))
                if created is None:
                    duplicate += 1
                else:
                    new += 1
            run.status = "ok"
        except Exception as exc:
            logger.exception("ingestion failed for source %s", source.name)
            run.status = "error"
            run.error = str(exc)[:500]

        run.finished_at = datetime.utcnow()
        run.leads_seen = seen
        run.leads_new = new
        run.leads_duplicate = duplicate
        session.add(run)
        session.commit()
        result = {"source": source.name, "seen": seen, "new": new, "duplicate": duplicate,
                  "status": run.status}
    logger.info("ingest %s", result)
    return result
