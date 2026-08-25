"""Phase 8 — the central METRIC REGISTRY.

ONE source of truth for every dashboard/analytics number. Each metric carries its exact definition, formula,
the states it INCLUDES and EXCLUDES, unit, tenant scope and an optional minimum sample size — so a KPI can never
be shown with an ambiguous label (a "prospect" is never called a "buyer"). Every value is computed live from the
operational tables via indexed COUNT/GROUP-BY aggregates; money is returned PER CURRENCY and never summed across
currencies. All timestamps are naive UTC (matching the rest of the app).

The registry is DATA, not magic: `compute(key, session, ...)` runs the metric's deterministic function. Nothing
here mutates data.
"""
from dataclasses import dataclass, field
from datetime import datetime
from typing import Callable, Optional

from sqlmodel import func, select

from .models import (Deal, Lead, OperationalException, PaymentMilestone, Quote, ServiceRequest, Settlement,
                     Shipment, WorkItem)

METRIC_VERSION = "m1"


@dataclass
class MetricDefinition:
    key: str
    label: str
    definition: str            # plain-language exact meaning
    formula: str               # how it is computed
    included: str              # states counted IN
    excluded: str              # states explicitly counted OUT
    unit: str                  # count | currency | percent | days
    source_tables: str
    fn: Callable               # (session, *, since, until, owner_id) -> int | dict
    scope: str = "global"      # global | tenant
    min_sample: int = 0
    time_basis: str = ""       # which timestamp the window filters on ("" = point-in-time state)


# --------------------------------------------------------------------- helpers
def _win(stmt, col, since, until):
    if since is not None:
        stmt = stmt.where(col >= since)
    if until is not None:
        stmt = stmt.where(col <= until)
    return stmt


def _count(session, model, *conds, col=None, since=None, until=None, owner=None, owner_col=None):
    stmt = select(func.count()).select_from(model)
    for c in conds:
        stmt = stmt.where(c)
    if owner is not None and owner_col is not None:
        stmt = stmt.where(owner_col == owner)
    if col is not None:
        stmt = _win(stmt, col, since, until)
    return session.exec(stmt).one()


# --------------------------------------------------------------------- metric functions (deterministic)
def _researched_prospects(session, *, since=None, until=None, owner_id=None):
    return _count(session, Lead, col=Lead.created_at, since=since, until=until,
                  owner=owner_id, owner_col=Lead.owner_id)


def _contactable_prospects(session, *, since=None, until=None, owner_id=None):
    have = ((Lead.email != None) & (Lead.email != "")) | ((Lead.phone != None) & (Lead.phone != ""))  # noqa: E711
    return _count(session, Lead, have, col=Lead.created_at, since=since, until=until,
                  owner=owner_id, owner_col=Lead.owner_id)


def _contacted(session, *, since=None, until=None, owner_id=None):
    # our first outbound touch was recorded — NOT the same as "delivered" or "opened"
    return _count(session, Lead, Lead.first_response_at != None, col=Lead.first_response_at,  # noqa: E711
                  since=since, until=until, owner=owner_id, owner_col=Lead.owner_id)


def _human_replies(session, *, since=None, until=None, owner_id=None):
    return _count(session, Lead, Lead.buyer_replied_at != None, Lead.reply_outcome != "auto_reply",  # noqa: E711
                  col=Lead.buyer_replied_at, since=since, until=until, owner=owner_id, owner_col=Lead.owner_id)


def _positive_replies(session, *, since=None, until=None, owner_id=None):
    return _count(session, Lead, Lead.reply_outcome == "positive", col=Lead.buyer_replied_at,
                  since=since, until=until, owner=owner_id, owner_col=Lead.owner_id)


def _negative_replies(session, *, since=None, until=None, owner_id=None):
    return _count(session, Lead, Lead.reply_outcome == "negative", col=Lead.buyer_replied_at,
                  since=since, until=until, owner=owner_id, owner_col=Lead.owner_id)


def _qualified_buyers(session, *, since=None, until=None, owner_id=None):
    return _count(session, Lead, Lead.engagement_class == "qualified", col=Lead.created_at,
                  since=since, until=until, owner=owner_id, owner_col=Lead.owner_id)


def _buyer_requirements(session, *, since=None, until=None, owner_id=None):
    # an engaged buyer who expressed a concrete requirement (a product + a quantity or a target price).
    # deliberately NOT "every lead" — a scraped list of companies is not a set of requirements.
    engaged = Lead.engagement_class.in_(("engaged", "qualified", "customer"))
    has_req = (Lead.product != None) & (Lead.product != "") & (  # noqa: E711
        ((Lead.quantity != None) & (Lead.quantity > 0)) | ((Lead.target_price != None) & (Lead.target_price > 0)))
    return _count(session, Lead, engaged, has_req, col=Lead.created_at, since=since, until=until,
                  owner=owner_id, owner_col=Lead.owner_id)


def _quotes_sent(session, *, since=None, until=None, owner_id=None):
    reached = Quote.status.in_(("sent", "viewed", "accepted", "rejected", "change_requested"))
    return _count(session, Quote, reached, col=Quote.created_at, since=since, until=until,
                  owner=owner_id, owner_col=Quote.owner_id)


def _quotes_viewed(session, *, since=None, until=None, owner_id=None):
    return _count(session, Quote, Quote.viewed_at != None, col=Quote.viewed_at,  # noqa: E711
                  since=since, until=until, owner=owner_id, owner_col=Quote.owner_id)


def _quotes_accepted(session, *, since=None, until=None, owner_id=None):
    return _count(session, Quote, Quote.status == "accepted", col=Quote.accepted_at,
                  since=since, until=until, owner=owner_id, owner_col=Quote.owner_id)


def _quotes_awaiting(session, *, since=None, until=None, owner_id=None):
    # sent/viewed quotes with no buyer decision yet — point-in-time
    return _count(session, Quote, Quote.status.in_(("sent", "viewed")),
                  owner=owner_id, owner_col=Quote.owner_id)


def _deals_opened(session, *, since=None, until=None, owner_id=None):
    return _count(session, Deal, col=Deal.created_at, since=since, until=until,
                  owner=owner_id, owner_col=Deal.owner_id)


def _deals_delivered(session, *, since=None, until=None, owner_id=None):
    return _count(session, Deal, Deal.stage.in_(("delivered", "settled", "closed")), col=Deal.created_at,
                  since=since, until=until, owner=owner_id, owner_col=Deal.owner_id)


def _deals_settled(session, *, since=None, until=None, owner_id=None):
    return _count(session, Deal, Deal.stage.in_(("settled", "closed")), col=Deal.created_at,
                  since=since, until=until, owner=owner_id, owner_col=Deal.owner_id)


def _active_deals(session, *, since=None, until=None, owner_id=None):
    return _count(session, Deal, Deal.closed_at == None, owner=owner_id, owner_col=Deal.owner_id)  # noqa: E711


def _active_requests(session, *, since=None, until=None, owner_id=None):
    return _count(session, ServiceRequest, ServiceRequest.status.in_(("submitted", "approved", "running")),
                  owner=owner_id, owner_col=ServiceRequest.owner_id)


def _open_work_queue(session, *, since=None, until=None, owner_id=None):
    return _count(session, WorkItem, WorkItem.status.in_(("open", "in_progress", "waiting")))


def _overdue_work_items(session, *, since=None, until=None, owner_id=None):
    now = datetime.utcnow()
    return _count(session, WorkItem, WorkItem.status.in_(("open", "in_progress", "waiting")),
                  WorkItem.due_at != None, WorkItem.due_at < now)  # noqa: E711


def _operational_exceptions(session, *, since=None, until=None, owner_id=None):
    from .ops_exceptions import OPEN_STATUSES
    return _count(session, OperationalException, OperationalException.status.in_(OPEN_STATUSES))


def _active_shipments(session, *, since=None, until=None, owner_id=None):
    return _count(session, Shipment, Shipment.status == "active",
                  Shipment.current_milestone != "delivered")


def _pipeline_value_by_ccy(session, *, since=None, until=None, owner_id=None):
    """Best quote per active lead, kept PER CURRENCY (never summed across currencies)."""
    stmt = select(Lead.id).where(Lead.status.in_(("quoted", "negotiating")))
    if owner_id is not None:
        stmt = stmt.where(Lead.owner_id == owner_id)
    active_ids = list(session.exec(stmt).all())
    out = {}
    if not active_ids:
        return out
    rows = session.exec(select(Quote.quote_currency, func.max(Quote.delivered_total))
                        .where(Quote.lead_id.in_(active_ids)).group_by(Quote.quote_currency, Quote.lead_id)).all()
    for ccy, val in rows:
        out[ccy or "—"] = round(out.get(ccy or "—", 0.0) + (val or 0.0), 2)
    return out


def _settled_value_by_ccy(session, *, since=None, until=None, owner_id=None):
    from .pricing import _d, _q
    rows = session.exec(_win(select(Settlement.currency, Settlement.revenue), Settlement.settlement_date,
                             since, until)).all()
    out = {}
    for ccy, rev in rows:
        out[ccy or "—"] = str(_q(_d(out.get(ccy or "—", "0")) + _d(rev)))
    return out


def _payments_received_by_ccy(session, *, since=None, until=None, owner_id=None):
    from .pricing import _d, _q
    rows = session.exec(select(PaymentMilestone.currency, PaymentMilestone.confirmed_amount)
                        .where(PaymentMilestone.status.in_(("received", "partially_received")))).all()
    out = {}
    for ccy, amt in rows:
        out[ccy or "—"] = str(_q(_d(out.get(ccy or "—", "0")) + _d(amt)))
    return out


# --------------------------------------------------------------------- the registry
_DEFS = [
    MetricDefinition("researched_prospects", "Researched prospects",
                     "Companies/contacts we sourced through research or ingestion. A prospect is NOT a buyer.",
                     "COUNT(Lead)", "all sourced leads", "verified demand, replies", "count", "lead",
                     _researched_prospects, time_basis="Lead.created_at"),
    MetricDefinition("contactable_prospects", "Contactable prospects",
                     "Prospects with at least one email or phone.",
                     "COUNT(Lead WHERE email OR phone)", "leads with a contact channel",
                     "leads with neither email nor phone", "count", "lead", _contactable_prospects,
                     time_basis="Lead.created_at"),
    MetricDefinition("contacted", "Contacted",
                     "Prospects we made a first outbound contact to (our first touch recorded).",
                     "COUNT(Lead WHERE first_response_at IS NOT NULL)", "leads with a recorded first outreach",
                     "email opens (not tracked), deliveries, bounces", "count", "lead", _contacted,
                     time_basis="Lead.first_response_at"),
    MetricDefinition("human_replies", "Human replies",
                     "Prospects who sent a genuine human reply (not an autoresponder).",
                     "COUNT(Lead WHERE buyer_replied_at IS NOT NULL AND reply_outcome != 'auto_reply')",
                     "real inbound human replies", "auto-replies, opens, deliveries", "count", "lead",
                     _human_replies, time_basis="Lead.buyer_replied_at"),
    MetricDefinition("positive_replies", "Positive buyer interest",
                     "Replies an admin CONFIRMED as positive interest.",
                     "COUNT(Lead WHERE reply_outcome = 'positive')", "admin-confirmed positive replies",
                     "negative replies, neutral, auto-replies, bounces", "count", "lead", _positive_replies,
                     time_basis="Lead.buyer_replied_at"),
    MetricDefinition("negative_replies", "Negative replies",
                     "Replies an admin marked negative. A negative reply proves a real contact but is NOT demand.",
                     "COUNT(Lead WHERE reply_outcome = 'negative')", "admin-confirmed negative replies",
                     "positive interest, demand", "count", "lead", _negative_replies,
                     time_basis="Lead.buyer_replied_at"),
    MetricDefinition("qualified_buyers", "Qualified buyers",
                     "Prospects an admin advanced to qualified.",
                     "COUNT(Lead WHERE engagement_class = 'qualified')", "qualified leads",
                     "prospects, contacted-only, negative", "count", "lead", _qualified_buyers,
                     time_basis="Lead.created_at"),
    MetricDefinition("buyer_requirements", "Buyer requirements",
                     "Engaged buyers who expressed a concrete requirement (a product + quantity or target price).",
                     "COUNT(Lead WHERE engaged AND product AND (quantity OR target_price))",
                     "engaged buyers with an expressed requirement",
                     "scraped leads with no requirement, negative replies", "count", "lead", _buyer_requirements,
                     time_basis="Lead.created_at"),
    MetricDefinition("quotes_sent", "Quotes sent",
                     "Quotes that reached the buyer (sent or beyond).",
                     "COUNT(Quote WHERE status IN sent/viewed/accepted/rejected/change_requested)",
                     "quotes delivered to a buyer", "drafts, needs_review, approved-not-sent", "count", "quote",
                     _quotes_sent, scope="tenant", time_basis="Quote.created_at"),
    MetricDefinition("quotes_viewed", "Quotes viewed",
                     "Quotes a buyer opened (viewed_at set via the portal, never on a GET).",
                     "COUNT(Quote WHERE viewed_at IS NOT NULL)", "quotes with a recorded buyer view",
                     "quotes never opened", "count", "quote", _quotes_viewed, scope="tenant",
                     time_basis="Quote.viewed_at"),
    MetricDefinition("quotes_accepted", "Accepted quotes",
                     "Quotes a buyer accepted.",
                     "COUNT(Quote WHERE status = 'accepted')", "accepted quotes",
                     "rejected, expired, change-requested", "count", "quote", _quotes_accepted, scope="tenant",
                     time_basis="Quote.accepted_at"),
    MetricDefinition("quotes_awaiting", "Quotes awaiting action",
                     "Sent/viewed quotes with no buyer decision yet (point-in-time).",
                     "COUNT(Quote WHERE status IN (sent, viewed))", "quotes awaiting a buyer decision",
                     "accepted, rejected, drafts", "count", "quote", _quotes_awaiting, scope="tenant"),
    MetricDefinition("deals_opened", "Deals opened",
                     "Deals created from an accepted quote/won lead.",
                     "COUNT(Deal)", "all deals", "quotes not yet a deal", "count", "deal", _deals_opened,
                     scope="tenant", time_basis="Deal.created_at"),
    MetricDefinition("deals_delivered", "Deals delivered",
                     "Deals that reached delivered or beyond.",
                     "COUNT(Deal WHERE stage IN delivered/settled/closed)", "delivered+ deals",
                     "in-transit and earlier stages", "count", "deal", _deals_delivered, scope="tenant",
                     time_basis="Deal.created_at"),
    MetricDefinition("deals_settled", "Deals settled",
                     "Deals financially settled.",
                     "COUNT(Deal WHERE stage IN settled/closed)", "settled deals", "unsettled deals", "count",
                     "deal", _deals_settled, scope="tenant", time_basis="Deal.created_at"),
    MetricDefinition("active_deals", "Active deals",
                     "Deals not yet closed (point-in-time).",
                     "COUNT(Deal WHERE closed_at IS NULL)", "open deals", "closed deals", "count", "deal",
                     _active_deals, scope="tenant"),
    MetricDefinition("active_requests", "Active requests",
                     "Seller service requests in flight (point-in-time).",
                     "COUNT(ServiceRequest WHERE status IN submitted/approved/running)", "in-flight requests",
                     "done, rejected", "count", "request", _active_requests, scope="tenant"),
    MetricDefinition("open_work_queue", "Open Work Queue",
                     "Non-terminal admin work items (point-in-time).",
                     "COUNT(WorkItem WHERE status IN open/in_progress/waiting)", "open work items",
                     "completed, dismissed", "count", "work_item", _open_work_queue),
    MetricDefinition("overdue_work_items", "Overdue actions",
                     "Open work items past their due date (point-in-time).",
                     "COUNT(WorkItem WHERE open AND due_at < now)", "overdue open items",
                     "future/undated items, completed", "count", "work_item", _overdue_work_items),
    MetricDefinition("operational_exceptions", "Operational exceptions",
                     "Open operational exceptions (point-in-time).",
                     "COUNT(OperationalException WHERE status IN open-states)", "open exceptions",
                     "resolved, dismissed", "count", "exception", _operational_exceptions),
    MetricDefinition("active_shipments", "Active shipments",
                     "Active shipments not yet delivered (point-in-time).",
                     "COUNT(Shipment WHERE status='active' AND milestone != 'delivered')", "in-flight shipments",
                     "delivered, archived, cancelled", "count", "shipment", _active_shipments),
    MetricDefinition("pipeline_value", "Pipeline value (per currency)",
                     "Best quote per active lead, kept per currency (never summed across currencies).",
                     "SUM(MAX(Quote.delivered_total) per active lead) GROUP BY currency",
                     "quoted/negotiating leads' best quote", "won/lost leads", "currency", "quote",
                     _pipeline_value_by_ccy, scope="tenant"),
    MetricDefinition("settled_value", "Settled value (per currency)",
                     "Settlement revenue, per currency (never summed across currencies).",
                     "SUM(Settlement.revenue) GROUP BY currency", "settled deals", "unsettled deals",
                     "currency", "settlement", _settled_value_by_ccy, scope="tenant",
                     time_basis="Settlement.settlement_date"),
    MetricDefinition("payments_received", "Payments received (per currency)",
                     "Confirmed payment amounts, per currency.",
                     "SUM(PaymentMilestone.confirmed_amount WHERE received) GROUP BY currency",
                     "received/partially-received milestones", "planned, awaiting, failed", "currency",
                     "payment", _payments_received_by_ccy),
]

METRICS = {d.key: d for d in _DEFS}


def compute(key, session, *, since=None, until=None, owner_id=None):
    """Compute a single metric by key. Raises KeyError for an unknown key (fail loud — no silent zero)."""
    d = METRICS[key]
    return d.fn(session, since=since, until=until, owner_id=owner_id)


def definition(key) -> dict:
    """The full public definition of a metric (for a KPI tooltip / details view)."""
    d = METRICS[key]
    return {"key": d.key, "label": d.label, "definition": d.definition, "formula": d.formula,
            "included": d.included, "excluded": d.excluded, "unit": d.unit, "scope": d.scope,
            "min_sample": d.min_sample, "source_tables": d.source_tables, "time_basis": d.time_basis,
            "metric_version": METRIC_VERSION}


def all_definitions() -> list:
    return [definition(k) for k in METRICS]
