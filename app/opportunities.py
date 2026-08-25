"""Phase 8 — Opportunity service: connect demand to Go4it supply, with a transparent versioned score.

An Opportunity groups demand signals for a (product, market) and matches them to canonical Products/Suppliers.
Every match is EXPLAINED and its missing requirements shown; supplier capability is never invented; buyer
identity is never exposed to a seller; nothing here auto-contacts a buyer or seller. Missing supply raises an
admin Work Queue action.
"""
from datetime import datetime

from sqlmodel import func, select

from . import matching
from . import opportunity_scoring as SCORING
from .config import MATCH_THRESHOLD
from .models import (DemandSignal, Opportunity, OpportunityMatch, OpportunitySignal, Product)
from .pipeline import audit

STATUSES = ("new", "needs_research", "needs_supply", "ready_for_review", "approved", "monitoring",
            "pursuing", "converted", "rejected", "expired", "archived")
TERMINAL = ("converted", "rejected", "archived")
# allowed forward/again transitions (admin-driven; no auto-advance)
TRANSITIONS = {
    "new": ("needs_research", "needs_supply", "ready_for_review", "rejected", "archived"),
    "needs_research": ("needs_supply", "ready_for_review", "rejected", "archived"),
    "needs_supply": ("ready_for_review", "rejected", "archived"),
    "ready_for_review": ("approved", "monitoring", "rejected", "archived"),
    "approved": ("monitoring", "pursuing", "archived"),
    "monitoring": ("pursuing", "expired", "archived"),
    "pursuing": ("converted", "monitoring", "archived"),
    "converted": ("archived",),
    "rejected": ("archived",),
    "expired": ("archived", "monitoring"),
    "archived": (),
}


def _ref(row_id, now=None):
    now = now or datetime.utcnow()
    return f"OPP-{now:%Y%m}-{row_id:04d}"


def _title(product, market):
    return f"{product or 'Unspecified'}" + (f" → {market}" if market else "")


def ensure_from_signal(session, signal: DemandSignal, *, actor=None, now=None):
    """Attach a demand signal to an open Opportunity for its (product, dest_country), creating one if none
    exists. Idempotent: the same signal never links twice. Returns (opportunity, created)."""
    now = now or datetime.utcnow()
    product = (signal.product or "").strip()
    market = (signal.dest_country or "").strip()
    opp = session.exec(select(Opportunity).where(
        Opportunity.product == product, Opportunity.dest_market == market,
        Opportunity.status.notin_(TERMINAL))).first()
    created = False
    if not opp:
        opp = Opportunity(product=product, category=signal.category or "", hs_code=signal.hs_code or "",
                          dest_market=market, title=_title(product, market), tenant_id=signal.tenant_id,
                          status="new", created_by=getattr(actor, "id", None), created_at=now, updated_at=now)
        session.add(opp)
        session.flush()
        opp.reference = _ref(opp.id, now)
        session.add(opp)
        audit(session, actor, "opportunity", opp.id, "opportunity_created",
              {"product": product, "market": market}, tenant_id=opp.tenant_id)
        created = True
    # link the signal (idempotent)
    if not session.exec(select(OpportunitySignal).where(
            OpportunitySignal.opportunity_id == opp.id,
            OpportunitySignal.demand_signal_id == signal.id)).first():
        session.add(OpportunitySignal(opportunity_id=opp.id, demand_signal_id=signal.id, created_at=now))
        session.flush()
    match_supply(session, opp, actor=actor, now=now)
    rescore(session, opp, actor=actor, now=now)
    return opp, created


def _signals(session, opp):
    ids = [r.demand_signal_id for r in session.exec(
        select(OpportunitySignal).where(OpportunitySignal.opportunity_id == opp.id)).all()]
    if not ids:
        return []
    return session.exec(select(DemandSignal).where(DemandSignal.id.in_(ids))).all()


def match_supply(session, opp: Opportunity, *, actor=None, now=None, limit=100):
    """Match the opportunity to canonical Products. Each match is EXPLAINED with its reasons + missing
    requirements. Never invents capability. If nothing clears the match threshold → a high_demand_no_supply
    Work Queue action (admins verify supply; no seller is contacted)."""
    now = now or datetime.utcnow()
    want = " ".join(x for x in (opp.product, opp.category, opp.hs_code) if x)
    products = session.exec(select(Product).where(Product.status == "active").limit(500)).all() \
        if _has_status_col() else session.exec(select(Product).limit(500)).all()
    scored = []
    for p in products:
        sim = matching.text_similarity(want, " ".join(x for x in (p.name, p.category, p.spec) if x))
        if opp.category and p.category and opp.category.strip().lower() == p.category.strip().lower():
            sim = min(100.0, sim + 12)
        if opp.hs_code and p.hs_code and opp.hs_code[:4] == p.hs_code[:4]:
            sim = min(100.0, sim + 10)
        if sim >= MATCH_THRESHOLD:
            scored.append((round(sim, 1), p))
    scored.sort(key=lambda x: -x[0])
    # clear prior matches for a clean recompute (matches are derived, not history)
    for m in session.exec(select(OpportunityMatch).where(OpportunityMatch.opportunity_id == opp.id)).all():
        session.delete(m)
    session.flush()
    for sim, p in scored[:limit]:
        missing = _missing_requirements(opp, p)
        reasons = f"name/spec similarity {int(sim)}%"
        if opp.category and p.category and opp.category.strip().lower() == p.category.strip().lower():
            reasons += ", category match"
        if opp.hs_code and p.hs_code and opp.hs_code[:4] == p.hs_code[:4]:
            reasons += ", HS heading match"
        session.add(OpportunityMatch(opportunity_id=opp.id, product_id=p.id, company_id=p.supplier_id,
                                     match_score=int(sim), explanation=reasons,
                                     missing_requirements="; ".join(missing),
                                     verified=(p.verification_status == "verified" and not missing),
                                     created_at=now))
    session.flush()
    if not scored:
        _no_supply_task(session, opp)
    return scored


def _missing_requirements(opp, p) -> list:
    """What is missing/unverified before this product could firmly serve the opportunity (never assumed)."""
    missing = []
    if not p.hs_code:
        missing.append("product HS code")
    if not p.origin_country:
        missing.append("product origin")
    if not p.exw_price:
        missing.append("pricing")
    if p.verification_status != "verified":
        missing.append("supplier/product verification")
    if (p.completeness_score or 0) < 60:
        missing.append("catalog completeness")
    return missing


def rescore(session, opp: Opportunity, *, actor=None, now=None):
    """Recompute the transparent score + confidence + breakdown from the current signals + matches. Records the
    scoring_version so a later weight change never rewrites this result."""
    now = now or datetime.utcnow()
    import json
    signals = _signals(session, opp)
    matches = session.exec(select(OpportunityMatch).where(OpportunityMatch.opportunity_id == opp.id)).all()
    final, confidence, breakdown = SCORING.score(signals, matches, now=now)
    opp.score = final
    opp.confidence = confidence
    opp.score_version = SCORING.scoring_version()
    opp.score_breakdown = json.dumps(breakdown)[:4000]
    opp.signal_count = len(signals)
    opp.freshness_at = max((s.observed_at or s.created_at for s in signals), default=None)
    # missing info drives the recommended next action + status hint (admin still decides)
    miss = []
    if not matches:
        miss.append("matched supply")
    if not any(s.strength == "strong" for s in signals):
        miss.append("stronger commercial evidence (accepted quote / Deal)")
    opp.missing_info = json.dumps(miss)
    opp.recommended_action = ("Verify or source supply" if not matches
                              else "Review and approve" if final >= 60 else "Gather more demand evidence")
    opp.updated_at = now
    session.add(opp)
    return final, confidence, breakdown


def set_status(session, opp: Opportunity, to_status: str, *, reason="", actor=None, now=None):
    """Admin status change. Validates against the workflow map. Returns (ok, error)."""
    now = now or datetime.utcnow()
    if to_status not in STATUSES:
        return False, "unknown status"
    if to_status != opp.status and to_status not in TRANSITIONS.get(opp.status, ()):
        return False, f"cannot move from {opp.status} to {to_status}"
    frm = opp.status
    opp.status = to_status
    opp.updated_at = now
    session.add(opp)
    audit(session, actor, "opportunity", opp.id, "opportunity_status_change",
          {"from": frm, "to": to_status, "reason": (reason or "")[:300]}, tenant_id=opp.tenant_id)
    return True, ""


def assign(session, opp: Opportunity, owner_id, *, actor=None):
    opp.owner_id = owner_id
    session.add(opp)
    audit(session, actor, "opportunity", opp.id, "opportunity_assigned", {"owner_id": owner_id},
          tenant_id=opp.tenant_id)
    return opp


def _no_supply_task(session, opp):
    try:
        from . import work_queue as WQ
        WQ.create_work_item_safe(
            session, tenant_id=opp.tenant_id, type="high_demand_no_supply", source="automatic",
            title=f"Opportunity {opp.reference or opp.id}: no matched supply",
            description=f"Demand for '{opp.product}' → {opp.dest_market} has no matched Go4it product. "
                        "Verify or source supply (no seller is contacted automatically).",
            related_opportunity_id=opp.id, idempotency_key=f"high_demand_no_supply:opp:{opp.id}",
            condition_version="no_supply")
    except Exception:  # noqa: BLE001
        pass


def detail(session, opp: Opportunity) -> dict:
    """Assemble the opportunity detail: why it exists (signals), the score breakdown, matched supply + missing
    requirements. Buyer identity is NOT included."""
    import json
    signals = _signals(session, opp)
    matches = session.exec(select(OpportunityMatch).where(OpportunityMatch.opportunity_id == opp.id)).all()
    try:
        breakdown = json.loads(opp.score_breakdown or "[]")
    except Exception:  # noqa: BLE001
        breakdown = []
    try:
        missing = json.loads(opp.missing_info or "[]")
    except Exception:  # noqa: BLE001
        missing = []
    return {"opportunity": opp, "signals": signals, "matches": matches, "breakdown": breakdown,
            "missing_info": missing, "scoring_version": opp.score_version}


_HAS_STATUS = None


def _has_status_col():
    global _HAS_STATUS
    if _HAS_STATUS is None:
        _HAS_STATUS = hasattr(Product, "status")
    return _HAS_STATUS
