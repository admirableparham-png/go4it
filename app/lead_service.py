"""Lead lifecycle shared by the web form and the ingestion worker:
dedup, tracking code, and the match -> auto-quote -> alert chain.
"""
import hashlib
import logging
from datetime import datetime

from sqlmodel import Session, select

from .config import MATCH_THRESHOLD
from .matching import score_lead_product
from .models import Lead, Match, Product
from .quote_service import create_quote
from .telegram import notify_lead_matches, notify_quote_ready

logger = logging.getLogger("go4it")


def content_hash(lead: Lead) -> str:
    """Stable hash of a lead's identifying content, for dedup."""
    parts = [lead.product, lead.category, lead.spec, lead.quantity, lead.unit,
             lead.target_price, lead.currency, lead.dest_country,
             lead.buyer_company, lead.contact_name, lead.email]
    key = "|".join(str(p).strip().lower() for p in parts)
    return hashlib.sha1(key.encode("utf-8")).hexdigest()


def find_duplicate(session: Session, lead: Lead):
    """Return an existing lead that this one duplicates, or None.

    Two-layer: (source, external_id) if the source gave a stable id, else the
    content hash. Makes re-ingesting the same export a no-op.
    """
    if lead.external_id:
        dup = session.exec(
            select(Lead).where(Lead.source == lead.source,
                               Lead.external_id == lead.external_id)
        ).first()
        if dup:
            return dup
    return session.exec(select(Lead).where(Lead.content_hash == lead.content_hash)).first()


def run_matching(session: Session, lead: Lead):
    """Match a lead against the active catalog, persist matches, alert, and
    auto-draft a quote for the best match."""
    saved = []
    for product in session.exec(select(Product).where(Product.active == True)).all():  # noqa: E712
        score, reasons = score_lead_product(lead, product)
        if score >= MATCH_THRESHOLD:
            session.add(Match(lead_id=lead.id, product_id=product.id, score=score, reasons=reasons))
            saved.append((product, score, reasons))
    session.commit()
    if saved:
        saved.sort(key=lambda t: t[1], reverse=True)
        notify_lead_matches(lead, saved[:5])
        try:
            quote = create_quote(session, lead, saved[0][0])
            notify_quote_ready(quote, lead, saved[0][0])
        except Exception:
            logger.warning("auto-quote failed for lead %s", lead.id, exc_info=True)
    return saved


def create_lead(session: Session, lead: Lead, run: bool = True):
    """Persist a new lead (deduped, with a tracking code) and run the match
    chain. Returns the created Lead, or None if it was a duplicate."""
    lead.content_hash = content_hash(lead)
    if find_duplicate(session, lead) is not None:
        return None
    session.add(lead)
    session.commit()
    session.refresh(lead)
    if not lead.tracking_code:
        lead.tracking_code = f"G4-{datetime.utcnow():%Y%m}-{lead.id:04d}"
        session.add(lead)
        session.commit()
        session.refresh(lead)
    if run:
        run_matching(session, lead)
    return lead
