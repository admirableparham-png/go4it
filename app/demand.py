"""Phase 8 — DemandSignal service.

Real demand ONLY. A signal is created exclusively from deterministic positive evidence: an admin-confirmed
positive reply, an expressed buyer requirement, an RFQ, an accepted quote, a Deal, or a verified tender. It is
NEVER created from a scraped lead, an email open/delivery, a bounce, a negative/auto reply, generic directory
membership, or a stale tender. Deduplication (`dedup_key`, partial-unique) guarantees one underlying event is
counted once even when it surfaces in several places.
"""
from datetime import datetime, timedelta

from sqlalchemy.exc import IntegrityError
from sqlmodel import func, select

from .models import DemandSignal, Deal, Lead, Quote, QuoteVersion
from .pipeline import audit

SIGNAL_TYPES = ("positive_reply", "buyer_requirement", "rfq", "quote_request", "accepted_quote",
                "repeat_interest", "deal", "verified_tender", "customs_trend", "inbound_account_request",
                "admin_market_observation")

# evidence tiers — strength + confidence come from the KIND of evidence, never from lead volume
_STRENGTH = {"deal": "strong", "accepted_quote": "strong", "verified_tender": "strong",
             "positive_reply": "moderate", "rfq": "moderate", "buyer_requirement": "moderate",
             "repeat_interest": "moderate", "inbound_account_request": "moderate", "customs_trend": "moderate",
             "quote_request": "weak", "admin_market_observation": "weak"}
_CONFIDENCE = {"deal": 95, "accepted_quote": 90, "verified_tender": 80, "rfq": 70, "repeat_interest": 70,
               "positive_reply": 65, "buyer_requirement": 60, "inbound_account_request": 60,
               "customs_trend": 55, "quote_request": 45, "admin_market_observation": 40}

# counting methods (documented so a report never mis-states what it counted)
COUNTING = {
    "unique_event": "one row per underlying source event (deduped by dedup_key).",
    "unique_buyer": "distinct company_id across signals.",
    "unique_requirement": "distinct (company_id, product, dest_country).",
    "unique_company": "distinct company_id.",
    "unique_product_market": "distinct (product, dest_country).",
}


def _create(session, *, signal_type, dedup_key, product="", category="", hs_code="", dest_country="",
            quantity="", unit="", company_id=None, lead_id=None, source="", source_event="", observed_at=None,
            expires_at=None, verification_state="observed", tenant_id=None, inferred=False, actor=None,
            now=None):
    """Idempotent insert guarded by uq_demandsignal_dedup. Returns (signal, created)."""
    now = now or datetime.utcnow()
    if signal_type not in SIGNAL_TYPES:
        return None, False
    existing = session.exec(select(DemandSignal).where(DemandSignal.dedup_key == dedup_key)).first()
    if existing:
        return existing, False
    sig = DemandSignal(signal_type=signal_type, product=product, category=category, hs_code=hs_code,
                       dest_country=dest_country, quantity=quantity, unit=unit, company_id=company_id,
                       lead_id=lead_id, source=source, source_event=source_event,
                       observed_at=observed_at or now, expires_at=expires_at,
                       confidence=_CONFIDENCE.get(signal_type, 40), strength=_STRENGTH.get(signal_type, "weak"),
                       verification_state=verification_state, tenant_id=tenant_id, dedup_key=dedup_key,
                       inferred=inferred, created_by=getattr(actor, "id", None), created_at=now)
    session.add(sig)
    try:
        session.flush()
    except IntegrityError:
        session.rollback()
        again = session.exec(select(DemandSignal).where(DemandSignal.dedup_key == dedup_key)).first()
        return again, False
    audit(session, actor, "demand_signal", sig.id, "demand_signal_created",
          {"type": signal_type, "product": product, "strength": sig.strength}, tenant_id=tenant_id)
    return sig, True


# --------------------------------------------------------------------- deterministic constructors
def from_positive_reply(session, lead: Lead, *, actor=None, inferred=False, now=None):
    """A confirmed POSITIVE reply → a moderate demand signal. REFUSES a negative/auto/none reply or a scraped
    lead — those are never demand."""
    if lead.reply_outcome != "positive":
        return None, False           # negative / neutral / auto_reply / none / bounced are NOT demand
    return _create(session, signal_type="positive_reply",
                   dedup_key=f"positive_reply:lead:{lead.id}", product=lead.product or "",
                   category=lead.category or "", dest_country=lead.dest_country or "",
                   quantity=str(lead.quantity) if lead.quantity else "", unit=lead.unit or "",
                   company_id=lead.company_id, lead_id=lead.id, source=lead.source or "",
                   source_event=f"lead_reply:{lead.id}", observed_at=lead.buyer_replied_at,
                   verification_state="verified", tenant_id=lead.seller_id, inferred=inferred, actor=actor,
                   now=now)


def from_accepted_quote(session, quote: Quote, *, actor=None, inferred=False, now=None):
    """An accepted quote → a STRONG derived signal (dedup per accepted version so re-runs don't double-count)."""
    if quote.status != "accepted" and quote.buyer_response != "accepted":
        return None, False
    lead = session.get(Lead, quote.lead_id) if quote.lead_id else None
    ver_id = quote.current_version_id
    return _create(session, signal_type="accepted_quote",
                   dedup_key=f"accepted_quote:qv:{ver_id or quote.id}",
                   product=(lead.product if lead else "") or "", category=(lead.category if lead else "") or "",
                   dest_country=(lead.dest_country if lead else "") or "",
                   company_id=(lead.company_id if lead else None), lead_id=quote.lead_id,
                   source="quote", source_event=f"quote_accept:qv:{ver_id or quote.id}",
                   observed_at=quote.accepted_at, verification_state="derived",
                   tenant_id=(lead.seller_id if lead else None), inferred=inferred, actor=actor, now=now)


def from_deal(session, deal: Deal, *, actor=None, inferred=False, now=None):
    """A Deal → the strongest derived signal (a real transaction). Deduped per deal."""
    lead = session.get(Lead, deal.lead_id) if deal.lead_id else None
    return _create(session, signal_type="deal", dedup_key=f"deal:{deal.id}",
                   product=(lead.product if lead else "") or "", category=(lead.category if lead else "") or "",
                   dest_country=(lead.dest_country if lead else "") or "",
                   company_id=(lead.company_id if lead else None), lead_id=deal.lead_id,
                   source="deal", source_event=f"deal:{deal.id}", observed_at=deal.created_at,
                   verification_state="derived", tenant_id=deal.owner_id, inferred=inferred, actor=actor,
                   now=now)


def record_admin_observation(session, *, product, dest_country="", category="", note="", actor=None, now=None):
    """An admin-confirmed market observation — a WEAK signal, clearly low-confidence."""
    now = now or datetime.utcnow()
    return _create(session, signal_type="admin_market_observation",
                   dedup_key=f"admin_obs:{product}:{dest_country}:{int(now.timestamp())}",
                   product=product, category=category, dest_country=dest_country, source="admin",
                   source_event=note[:120], verification_state="verified", actor=actor, now=now)


# --------------------------------------------------------------------- counting + seasonality
def count(session, method="unique_event", *, since=None, until=None):
    """Count demand by an EXPLICIT method (documented in COUNTING). Never conflates events with buyers."""
    stmt = select(DemandSignal)
    if since is not None:
        stmt = stmt.where(DemandSignal.observed_at >= since)
    if until is not None:
        stmt = stmt.where(DemandSignal.observed_at <= until)
    rows = session.exec(stmt).all()
    if method == "unique_event":
        return len(rows)
    if method == "unique_buyer" or method == "unique_company":
        return len({r.company_id for r in rows if r.company_id})
    if method == "unique_requirement":
        return len({(r.company_id, r.product, r.dest_country) for r in rows})
    if method == "unique_product_market":
        return len({(r.product, r.dest_country) for r in rows if r.product})
    return len(rows)


SEASONALITY_MIN_PERIODS = 3   # need at least this many comparable historical periods before claiming a season


def seasonality(session, product, *, period="month", now=None):
    """Return a seasonality read for a product ONLY when there is enough history. With fewer than
    SEASONALITY_MIN_PERIODS comparable periods it returns {'sufficient': False} and an honest 'Insufficient
    history' — a single season/year never implies a seasonal pattern."""
    now = now or datetime.utcnow()
    rows = session.exec(select(DemandSignal.observed_at).where(DemandSignal.product == product)).all()
    buckets = {}
    for ts in rows:
        if ts:
            key = ts.strftime("%Y-%m") if period == "month" else ts.strftime("%Y")
            buckets[key] = buckets.get(key, 0) + 1
    if len(buckets) < SEASONALITY_MIN_PERIODS:
        return {"sufficient": False, "sample_periods": len(buckets),
                "message": "Insufficient history — need at least "
                           f"{SEASONALITY_MIN_PERIODS} comparable periods."}
    return {"sufficient": True, "sample_periods": len(buckets), "buckets": buckets}
