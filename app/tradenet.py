"""Trade Network (Phase 2) read-side: response-rate stats, data-quality metrics, source quality, and the
company-detail assembly. All ADMIN-ONLY (callers gate with is_admin); nothing here is seller-reachable.
Denominators are always labelled (response rate = human replies / delivered, else / attempted)."""
from datetime import datetime, timedelta

from sqlalchemy import func
from sqlmodel import select

from . import company_service as CS
from .models import (Company, CompanyRole, Contact, Deal, DuplicateCandidate, Lead, Outreach,
                     Provenance, Quote, ServiceRequest, Supplier)

STALE_DAYS = 180
ENGAGED_CLASSES = ("engaged", "qualified", "customer")


def source_label(raw_source):
    st, name, _ = CS.map_source(raw_source, "")
    return CS.SOURCE_LABELS.get(st, name or "Unknown source"), st


def _outbound_maps(session, lead_ids=None):
    """(attempted_by_lead, sent_by_lead) — one query each. attempted = any outbound; sent = status 'sent'."""
    q_att = select(Outreach.lead_id, func.count()).where(Outreach.direction == "out").group_by(Outreach.lead_id)
    q_sent = select(Outreach.lead_id, func.count()).where(
        Outreach.direction == "out", Outreach.status == "sent").group_by(Outreach.lead_id)
    att = {lid: n for lid, n in session.exec(q_att).all()}
    sent = {lid: n for lid, n in session.exec(q_sent).all()}
    return att, sent


def response_stats(session, leads, att=None, sent=None):
    """Response performance over a lead list, with labelled denominators. Auto-replies never count as human
    replies."""
    if att is None or sent is None:
        att, sent = _outbound_maps(session)
    attempted = sum(1 for l in leads if att.get(l.id) or l.first_response_at)
    delivered = sum(1 for l in leads if sent.get(l.id))
    human = sum(1 for l in leads if l.reply_outcome in ("positive", "negative", "neutral"))
    positive = sum(1 for l in leads if l.reply_outcome == "positive")
    negative = sum(1 for l in leads if l.reply_outcome == "negative")
    bounces = sum(1 for l in leads if l.reply_outcome == "bounced")
    qualified = sum(1 for l in leads if l.engagement_class in ("qualified", "customer"))
    denom, label = (delivered, "delivered outreach") if delivered else (attempted, "attempted outreach")
    pct = lambda n: round(100 * n / denom) if denom else 0  # noqa: E731
    return {"attempted": attempted, "delivered": delivered, "replies": human, "human_replies": human,
            "positive": positive, "negative": negative, "bounces": bounces, "qualified": qualified,
            "denominator": denom, "denominator_label": label,
            "response_rate": pct(human), "positive_rate": pct(positive), "qualification_rate": pct(qualified)}


def source_quality(session):
    """Per-source performance — judged by contactability/response/qualification/conversion/freshness, NEVER
    by record volume. Grouped by the READABLE source_type; raw slug kept for admin investigation."""
    att, sent = _outbound_maps(session)
    leads = session.exec(select(Lead).where(Lead.buyer_company != "")).all()
    rows = {}
    for l in leads:
        label, st = source_label(l.source)
        r = rows.setdefault(st, {"source_type": st, "label": label, "raw_examples": set(), "records": 0,
                                 "contactable": 0, "attempts": 0, "human_replies": 0, "positive": 0,
                                 "qualified": 0, "bounces": 0, "deals_won": 0, "last_collected": None})
        r["records"] += 1
        if len(r["raw_examples"]) < 3:
            r["raw_examples"].add((l.source or "")[:40])
        if l.email or l.phone:
            r["contactable"] += 1
        r["attempts"] += att.get(l.id, 0)
        if l.reply_outcome in ("positive", "negative", "neutral"):
            r["human_replies"] += 1
        if l.reply_outcome == "positive":
            r["positive"] += 1
        if l.engagement_class in ("qualified", "customer"):
            r["qualified"] += 1
        if l.reply_outcome == "bounced":
            r["bounces"] += 1
        if l.engagement_class == "customer":
            r["deals_won"] += 1
        d = l.posted_at or l.created_at
        if d and (r["last_collected"] is None or d > r["last_collected"]):
            r["last_collected"] = d
    out = []
    for r in rows.values():
        rec = r["records"] or 1
        r["contactable_pct"] = round(100 * r["contactable"] / rec)
        r["reply_rate"] = round(100 * r["human_replies"] / rec)
        r["qual_rate"] = round(100 * r["qualified"] / rec)
        r["raw_examples"] = ", ".join(sorted(r["raw_examples"]))
        out.append(r)
    out.sort(key=lambda r: -r["records"])
    return out


def data_quality_metrics(session):
    """Metrics + actionable queues. Every metric carries a `link` to the filtered record list."""
    cnt = lambda q: session.exec(q).one()  # noqa: E731
    total_companies = cnt(select(func.count(Company.id)).where(Company.status == "active"))
    total_contacts = cnt(select(func.count(Contact.id)))
    # companies with at least one usable contact
    contactable_company_ids = {c.company_id for c in session.exec(
        select(Contact).where((Contact.email_normalized != "") | (Contact.phone_normalized != ""))).all()}
    contactable = len(contactable_company_ids)
    missing_contact = total_companies - contactable
    invalid_emails = cnt(select(func.count(Contact.id)).where(Contact.email_health == "invalid"))
    bounces = cnt(select(func.count(Contact.id)).where(Contact.email_health == "bounced"))
    unknown_src_companies = len({p.entity_id for p in session.exec(
        select(Provenance).where(Provenance.entity_type == "company", Provenance.source_type == "unknown")).all()})
    stale_cut = datetime.utcnow() - timedelta(days=STALE_DAYS)
    stale = cnt(select(func.count(Provenance.id)).where(
        Provenance.entity_type == "company", Provenance.last_seen_at < stale_cut))
    unverified = cnt(select(func.count(Company.id)).where(
        Company.status == "active", Company.verification_status == "unverified"))
    potential_dupes = cnt(select(func.count(DuplicateCandidate.id)).where(DuplicateCandidate.status == "open"))
    unlinked_leads = cnt(select(func.count(Lead.id)).where(
        Lead.company_id.is_(None), Lead.buyer_company != ""))
    unlinked_suppliers = cnt(select(func.count(Supplier.id)).where(Supplier.company_id.is_(None)))
    return {
        "total_companies": total_companies, "total_contacts": total_contacts,
        "contactable": contactable, "missing_contact": missing_contact,
        "invalid_emails": invalid_emails, "bounces": bounces,
        "unknown_sources": unknown_src_companies, "stale": stale, "unverified": unverified,
        "potential_duplicates": potential_dupes,
        "unlinked": unlinked_leads + unlinked_suppliers,
        # actionable queues (label, count, link)
        "queues": [
            ("Needs enrichment (no contact)", missing_contact, "/data-quality?queue=no_contact"),
            ("Needs source review (unknown)", unknown_src_companies, "/data-quality?queue=unknown_source"),
            ("Needs verification", unverified, "/data-quality?queue=unverified"),
            ("Needs duplicate review", potential_dupes, "/duplicates"),
            ("Needs contact replacement (invalid/bounced)", invalid_emails + bounces, "/data-quality?queue=bad_email"),
            ("Needs company linking", unlinked_leads + unlinked_suppliers, "/data-quality?queue=unlinked"),
        ],
    }


def company_detail(session, company):
    """Assemble the admin-only company detail context."""
    cid = company.id
    roles = [r.role for r in session.exec(select(CompanyRole).where(CompanyRole.company_id == cid)).all()]
    contacts = session.exec(select(Contact).where(Contact.company_id == cid)).all()
    provenance = session.exec(select(Provenance).where(
        Provenance.entity_type == "company", Provenance.entity_id == cid)
        .order_by(Provenance.last_seen_at.desc())).all()
    leads = session.exec(select(Lead).where(Lead.company_id == cid).order_by(Lead.id.desc())).all()
    lead_ids = [l.id for l in leads]
    quotes = session.exec(select(Quote).where(Quote.lead_id.in_(lead_ids))).all() if lead_ids else []
    deals = session.exec(select(Deal).where(Deal.lead_id.in_(lead_ids))).all() if lead_ids else []
    req_ids = {l.request_id for l in leads if l.request_id}
    requests = session.exec(select(ServiceRequest).where(ServiceRequest.id.in_(req_ids))).all() if req_ids else []
    outreach = session.exec(select(Outreach).where(Outreach.lead_id.in_(lead_ids))
                            .order_by(Outreach.created_at.desc())) if lead_ids else []
    outreach = list(outreach) if lead_ids else []
    dups = session.exec(select(DuplicateCandidate).where(
        (DuplicateCandidate.left_id == cid) | (DuplicateCandidate.right_id == cid))).all()
    from .models import AuditLog
    audit = session.exec(select(AuditLog).where(
        AuditLog.entity_type == "company", AuditLog.entity_id == cid).order_by(AuditLog.id.desc())).all()
    return {"company": company, "roles": roles, "contacts": contacts, "provenance": provenance,
            "leads": leads, "quotes": quotes, "deals": deals, "requests": requests, "outreach": outreach,
            "duplicates": dups, "audit": audit, "source_labels": CS.SOURCE_LABELS}
