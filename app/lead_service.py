"""Lead lifecycle shared by the web form and the ingestion worker:
dedup, tracking code, and the match -> auto-quote -> alert chain.
"""
import hashlib
import logging
import re
from datetime import datetime

from sqlalchemy import func
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


def find_lead_by_contact(session: Session, email: str = "", phone: str = ""):
    """Find the Lead an inbound message belongs to, by sender email (then phone). Used by the IMAP
    poller to THREAD a buyer reply onto its lead — NOT create_lead (which drops dups, never attaches).
    Returns the most recent matching Lead, or None (unmatched inbound is skipped, not auto-created)."""
    email = (email or "").strip().lower()
    if email:
        lead = session.exec(
            select(Lead).where(func.lower(Lead.email) == email).order_by(Lead.id.desc())).first()
        if lead:
            return lead
    digits = re.sub(r"[^\d]", "", phone or "")
    if len(digits) >= 7:
        tail = digits[-9:]      # match on the last 9 digits (ignore country-code / formatting)
        for lead in session.exec(select(Lead).where(Lead.phone != "").order_by(Lead.id.desc())).all():
            if re.sub(r"[^\d]", "", lead.phone or "")[-9:] == tail:
                return lead
    return None


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


def run_matching(session: Session, lead: Lead, auto_quote: bool = True):
    """Match a lead against the active catalog and persist matches (idempotent — clears any
    prior matches for this lead first, so it doubles as a re-match). When auto_quote is True
    (the live web-form/ingest path) it also alerts and auto-drafts a quote for the best match;
    backfills/re-matches pass auto_quote=False to avoid mass quote/alert spam."""
    for m in session.exec(select(Match).where(Match.lead_id == lead.id)).all():
        session.delete(m)
    saved = []
    for product in session.exec(select(Product).where(Product.active == True)).all():  # noqa: E712
        score, reasons = score_lead_product(lead, product)
        if score >= MATCH_THRESHOLD:
            session.add(Match(lead_id=lead.id, product_id=product.id, score=score, reasons=reasons))
            saved.append((product, score, reasons))
    session.commit()
    if saved:
        saved.sort(key=lambda t: t[1], reverse=True)
    if saved and auto_quote:
        notify_lead_matches(lead, saved[:5])
        best = saved[0][0]
        if (best.exw_price or 0) > 0 and (best.weight_kg_per_unit or 0) > 0:  # don't quote unpriced
            try:
                quote = create_quote(session, lead, best)
                notify_quote_ready(quote, lead, best)
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
