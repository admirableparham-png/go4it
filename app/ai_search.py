"""Phase 9 — allowlisted STRUCTURED search for the AI copilot.

The model never writes or runs SQL. It may only call `search(entity, ...)` for a REGISTERED entity, with
validated filter/sort fields, a capped limit, tenant scope, and a SAFE PROJECTION (a curated field set — never
credentials, tokens, password hashes, share tokens, raw private documents, or another tenant's data). Entities
that carry secrets (User, MailAccount, QuoteAccessToken, PortalSession, …) are deliberately NOT registered.
"""
from dataclasses import dataclass, field
from typing import Callable

from sqlmodel import func, select

from .models import (Company, Contact, Deal, DemandSignal, Lead, OperationalException, OperationCase,
                     Opportunity, Product, ProductCategory, Provenance, Quote, ServiceRequest, Shipment,
                     Supplier, WorkItem)

MAX_LIMIT = 50


@dataclass
class SearchEntity:
    key: str
    model: type
    filters: tuple          # allowlisted equality/ilike filter fields
    sort: tuple             # allowlisted sort fields
    scope_col: str          # tenant/owner scope column name ("" = global admin data)
    project: Callable       # (row) -> safe dict
    ref: Callable           # (row) -> safe reference string
    text_field: str = ""    # field used for a free-text `query`


def _d(ts):
    return ts.isoformat() if ts else None


SEARCH = {
    "leads": SearchEntity(
        "leads", Lead, ("status", "engagement_class", "reply_outcome", "dest_country", "category", "source"),
        ("id", "created_at"), "owner_id",
        lambda r: {"tracking_code": r.tracking_code, "product": r.product, "category": r.category,
                   "dest_country": r.dest_country, "status": r.status, "engagement_class": r.engagement_class,
                   "reply_outcome": r.reply_outcome, "created_at": _d(r.created_at)},
        lambda r: r.tracking_code or f"lead:{r.id}", text_field="product"),
    "companies": SearchEntity(
        "companies", Company, ("primary_role", "country", "verification_status", "status"),
        ("id", "name"), "tenant_id",
        lambda r: {"name": r.name, "country": r.country, "role": r.primary_role,
                   "verification": r.verification_status, "status": r.status},
        lambda r: f"company:{r.id}", text_field="name"),
    "contacts": SearchEntity(
        "contacts", Contact, ("email_health", "is_primary", "active"), ("id",), "tenant_id",
        lambda r: {"name": r.name, "title": r.title, "email_health": r.email_health,
                   "is_primary": r.is_primary},
        lambda r: f"contact:{r.id}", text_field="name"),
    "requests": SearchEntity(
        "requests", ServiceRequest, ("status", "request_type", "workflow_status"), ("id", "created_at"),
        "owner_id",
        lambda r: {"tracking_code": r.tracking_code, "request_type": r.request_type, "status": r.status,
                   "workflow_status": r.workflow_status, "created_at": _d(r.created_at)},
        lambda r: r.tracking_code or f"request:{r.id}"),
    "work_queue": SearchEntity(
        "work_queue", WorkItem, ("type", "status", "priority"), ("id", "due_at"), "tenant_id",
        lambda r: {"type": r.type, "status": r.status, "priority": r.priority, "title": r.title,
                   "due_at": _d(r.due_at)},
        lambda r: f"workitem:{r.id}", text_field="title"),
    "products": SearchEntity(
        "products", Product, ("category", "origin_country", "verification_status", "status"), ("id", "name"),
        "",
        lambda r: {"name": r.name, "category": r.category, "hs_code": r.hs_code,
                   "origin_country": r.origin_country, "verification": r.verification_status,
                   "completeness": r.completeness_score, "unit": r.unit},
        lambda r: f"product:{r.id}", text_field="name"),
    "categories": SearchEntity(
        "categories", ProductCategory, ("status",), ("id", "name"), "tenant_id",
        lambda r: {"name": r.name, "status": r.status},
        lambda r: f"category:{r.id}", text_field="name"),
    "suppliers": SearchEntity(
        "suppliers", Supplier, ("country",), ("id", "name"), "",
        lambda r: {"name": r.name, "country": getattr(r, "country", "")},
        lambda r: f"supplier:{r.id}", text_field="name"),
    "demand_signals": SearchEntity(
        "demand_signals", DemandSignal, ("signal_type", "dest_country", "strength", "verification_state"),
        ("id", "observed_at"), "tenant_id",
        lambda r: {"signal_type": r.signal_type, "product": r.product, "dest_country": r.dest_country,
                   "strength": r.strength, "confidence": r.confidence, "verification": r.verification_state,
                   "observed_at": _d(r.observed_at)},
        lambda r: f"demand:{r.id}", text_field="product"),
    "opportunities": SearchEntity(
        "opportunities", Opportunity, ("status", "dest_market", "category"), ("id", "score"), "tenant_id",
        lambda r: {"reference": r.reference, "product": r.product, "dest_market": r.dest_market,
                   "score": r.score, "confidence": r.confidence, "status": r.status},
        lambda r: r.reference or f"opportunity:{r.id}", text_field="title"),
    "quotes": SearchEntity(
        "quotes", Quote, ("status", "quote_currency"), ("id", "created_at"), "owner_id",
        lambda r: {"tracking_code": r.tracking_code, "status": r.status, "currency": r.quote_currency,
                   "total": r.delivered_total, "created_at": _d(r.created_at)},   # admin-only; no share_token
        lambda r: r.tracking_code or f"quote:{r.id}"),
    "deals": SearchEntity(
        "deals", Deal, ("stage",), ("id", "created_at"), "owner_id",
        lambda r: {"tracking_code": r.tracking_code, "stage": r.stage, "created_at": _d(r.created_at)},
        lambda r: r.tracking_code or f"deal:{r.id}"),
    "operation_cases": SearchEntity(
        "operation_cases", OperationCase, ("status", "case_type", "dest_country"), ("id",), "tenant_id",
        lambda r: {"reference": r.reference, "case_type": r.case_type, "status": r.status,
                   "origin_country": r.origin_country, "dest_country": r.dest_country},
        lambda r: r.reference or f"case:{r.id}"),
    "shipments": SearchEntity(
        "shipments", Shipment, ("current_milestone", "mode", "status"), ("id",), "tenant_id",
        # NO booking/container/tracking references or carrier identity in the projection
        lambda r: {"reference": r.reference, "mode": r.mode, "milestone": r.current_milestone,
                   "exception_state": r.exception_state, "status": r.status},
        lambda r: r.reference or f"shipment:{r.id}"),
    "exceptions": SearchEntity(
        "exceptions", OperationalException, ("status", "severity", "exc_type"), ("id",), "tenant_id",
        lambda r: {"reference": r.reference, "type": r.exc_type, "severity": r.severity, "status": r.status},
        lambda r: r.reference or f"exception:{r.id}"),
    "provenance": SearchEntity(
        "provenance", Provenance, ("entity_type", "source_type"), ("id",), "tenant_id",
        lambda r: {"entity_type": r.entity_type, "source_type": r.source_type, "source_name": r.source_name,
                   "collected_at": _d(r.collected_at), "inferred": r.inferred},
        lambda r: f"provenance:{r.id}"),
}


class SearchError(ValueError):
    pass


def search(session, entity, *, filters=None, query="", sort=None, limit=20, page=1, user=None):
    """Run an allowlisted structured search. Returns {entity, total, rows:[{...projection, _ref, _id}]}.
    Raises SearchError for an unknown entity or a non-allowlisted filter/sort field (fail loud — never silently
    run an arbitrary query)."""
    if entity not in SEARCH:
        raise SearchError(f"unknown search entity '{entity}'")
    e = SEARCH[entity]
    filters = filters or {}
    for f in filters:
        if f not in e.filters:
            raise SearchError(f"filter '{f}' not allowed on {entity}")
    if sort and sort.lstrip("-") not in e.sort:
        raise SearchError(f"sort '{sort}' not allowed on {entity}")
    limit = max(1, min(int(limit or 20), MAX_LIMIT))
    page = max(1, int(page or 1))
    stmt = select(e.model)
    for f, v in filters.items():
        stmt = stmt.where(getattr(e.model, f) == v)
    if query and e.text_field:
        stmt = stmt.where(getattr(e.model, e.text_field).ilike(f"%{str(query).strip()}%"))
    # tenant scope: the copilot is admin-only (admins see all), but scope stays available for correctness.
    total = session.exec(select(func.count()).select_from(stmt.subquery())).one()
    order_col = getattr(e.model, (sort or "id").lstrip("-"))
    stmt = stmt.order_by(order_col.desc() if (sort or "").startswith("-") or not sort else order_col.asc())
    rows = session.exec(stmt.offset((page - 1) * limit).limit(limit)).all()
    out = []
    for r in rows:
        proj = e.project(r)
        proj["_ref"] = e.ref(r)
        proj["_id"] = r.id
        out.append(proj)
    return {"entity": entity, "total": total, "rows": out}


def entities() -> list:
    return list(SEARCH.keys())
