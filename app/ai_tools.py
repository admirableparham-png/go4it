"""Phase 9 — the central AI TOOL REGISTRY.

The model may select ONLY registered tools. Every tool declares its purpose, input schema, risk level, read/
draft/write mode, whether it requires approval, its result limit and audit policy. There is deliberately NO tool
for shell/code/SQL, arbitrary URLs, file access, environment/credentials, auth/role changes, payments/remittance,
contract signing, or accepting a quote as a buyer. Read-only tools run without a separate approval (still authz +
limits + audited, no full-payload logging). Write/draft tools (Checkpoint B) require an explicit action approval
and are never auto-executed by the copilot loop.
"""
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Callable

from sqlmodel import func, select

from . import ai_citations as CIT
from . import ai_search as SEARCH
from . import data_sources as DATASRC
from . import metrics as M
from . import opportunities as OPP
from .models import AIToolInvocation, Deal, DemandSignal, Lead, Opportunity, Quote, ServiceRequest, WorkItem

# risk ladder — high to low permissiveness of consequence
RISK = ("read_only", "draft", "internal_reversible", "external_comm", "commercial", "prohibited")


@dataclass
class ToolDef:
    name: str
    purpose: str
    mode: str                # read | draft | write
    risk: str
    params: dict             # {name: "required"|"optional"}
    run: Callable            # (session, params, user) -> {"result":..., "citations":[...]}
    requires_approval: bool = False
    result_limit: int = 50
    external: bool = False


def _need(params, *keys):
    for k in keys:
        if params.get(k) in (None, ""):
            raise ValueError(f"missing required parameter '{k}'")


# --------------------------------------------------------------------- read-only tool implementations
def _t_search(session, params, user):
    _need(params, "entity")
    res = SEARCH.search(session, params["entity"], query=params.get("query", ""),
                        filters=params.get("filters"), limit=params.get("limit", 20),
                        page=params.get("page", 1), user=user)
    cites = [CIT.record_citation(res["entity"], r) for r in res["rows"][:10]]
    return {"result": res, "citations": cites}


def _t_get_metric(session, params, user):
    _need(params, "key")
    key = params["key"]
    if key not in M.METRICS:
        return {"result": {"error": f"unknown metric '{key}'"}, "citations": []}
    days = params.get("days")
    since = None
    rng = "all-time"
    if days:
        since = datetime.utcnow() - timedelta(days=int(days))
        rng = f"{int(days)}d"
    value = M.compute(key, session, since=since)
    d = M.definition(key)
    return {"result": {"key": key, "label": d["label"], "value": value, "unit": d["unit"],
                       "definition": d["definition"], "range": rng, "metric_version": d["metric_version"],
                       "per_currency": isinstance(value, dict)},
            "citations": [CIT.metric_citation(key, time_range=rng)]}


def _t_source_health(session, params, user):
    health = DATASRC.source_health(session)
    cites = [{"record_type": "source", "record_ref": f"source:{h['key']}", "record_id": None,
              "record_at": h["last_success"], "source": h["label"], "freshness": h["freshness"],
              "provenance_class": "observed", "link": ""} for h in health]
    return {"result": {"sources": health}, "citations": cites}


def _t_summarize_opportunity(session, params, user):
    _need(params, "id")
    opp = session.get(Opportunity, int(params["id"]))
    if not opp:
        return {"result": {"error": "not found"}, "citations": []}
    d = OPP.detail(session, opp)   # buyer-identity-free by construction
    summary = {"reference": opp.reference, "product": opp.product, "market": opp.dest_market,
               "score": opp.score, "confidence": opp.confidence, "status": opp.status,
               "missing_info": d["missing_info"], "signal_count": len(d["signals"]),
               "match_count": len(d["matches"]), "scoring_version": d["scoring_version"]}
    return {"result": summary, "citations": [{"record_type": "opportunity",
            "record_ref": opp.reference or f"opportunity:{opp.id}", "record_id": opp.id,
            "record_at": opp.created_at, "source": "opportunity", "freshness": "", "provenance_class": "derived",
            "link": f"/intelligence/opportunities/{opp.id}"}]}


def _t_explain_score(session, params, user):
    _need(params, "id")
    opp = session.get(Opportunity, int(params["id"]))
    if not opp:
        return {"result": {"error": "not found"}, "citations": []}
    d = OPP.detail(session, opp)
    return {"result": {"reference": opp.reference, "score": opp.score, "confidence": opp.confidence,
                       "scoring_version": d["scoring_version"], "breakdown": d["breakdown"]},
            "citations": [{"record_type": "opportunity", "record_ref": opp.reference, "record_id": opp.id,
                           "record_at": opp.created_at, "source": "opportunity_scoring",
                           "provenance_class": "derived", "freshness": "",
                           "link": f"/intelligence/opportunities/{opp.id}"}]}


def _t_summarize_quote(session, params, user):
    _need(params, "id")
    q = session.get(Quote, int(params["id"]))
    if not q:
        return {"result": {"error": "not found"}, "citations": []}
    return {"result": {"tracking_code": q.tracking_code, "status": q.status, "currency": q.quote_currency,
                       "total": q.delivered_total, "viewed": q.viewed_at is not None,
                       "accepted": q.status == "accepted"},
            "citations": [{"record_type": "quote", "record_ref": q.tracking_code or f"quote:{q.id}",
                           "record_id": q.id, "record_at": q.created_at, "source": "quote",
                           "provenance_class": "observed", "freshness": "", "link": f"/quotes/{q.id}"}]}


def _t_summarize_deal(session, params, user):
    _need(params, "id")
    dl = session.get(Deal, int(params["id"]))
    if not dl:
        return {"result": {"error": "not found"}, "citations": []}
    lead = session.get(Lead, dl.lead_id) if dl.lead_id else None
    return {"result": {"tracking_code": dl.tracking_code, "stage": dl.stage,
                       "product": lead.product if lead else "", "planned_margin": dl.planned_margin,
                       "realized_margin": dl.realized_margin, "closed": dl.closed_at is not None},
            "citations": [{"record_type": "deal", "record_ref": dl.tracking_code or f"deal:{dl.id}",
                           "record_id": dl.id, "record_at": dl.created_at, "source": "deal",
                           "provenance_class": "observed", "freshness": "", "link": f"/deals/{dl.id}"}]}


def _t_summarize_request(session, params, user):
    _need(params, "id")
    sr = session.get(ServiceRequest, int(params["id"]))
    if not sr:
        return {"result": {"error": "not found"}, "citations": []}
    from . import pipeline
    try:
        fn = pipeline.request_funnel(session, sr)
    except Exception:  # noqa: BLE001
        fn = {}
    return {"result": {"tracking_code": sr.tracking_code, "request_type": sr.request_type, "status": sr.status,
                       "workflow_status": sr.workflow_status, "funnel": fn},
            "citations": [{"record_type": "request", "record_ref": sr.tracking_code or f"request:{sr.id}",
                           "record_id": sr.id, "record_at": sr.created_at, "source": "request",
                           "provenance_class": "observed", "freshness": "", "link": "/admin/requests"}]}


def _t_overdue_work_items(session, params, user):
    now = datetime.utcnow()
    rows = session.exec(select(WorkItem).where(
        WorkItem.status.in_(("open", "in_progress", "waiting")), WorkItem.due_at != None,  # noqa: E711
        WorkItem.due_at < now).order_by(WorkItem.due_at.asc()).limit(50)).all()
    items = [{"type": w.type, "title": w.title, "priority": w.priority, "due_at": w.due_at.isoformat()
              if w.due_at else None, "_ref": f"workitem:{w.id}", "_id": w.id} for w in rows]
    return {"result": {"overdue": items, "count": len(items)},
            "citations": [CIT.record_citation("work_queue", i) for i in items[:10]]}


def _t_compare_markets(session, params, user):
    rows = session.exec(select(DemandSignal.dest_country, func.count()).where(DemandSignal.dest_country != "")
                        .group_by(DemandSignal.dest_country).order_by(func.count().desc()).limit(10)).all()
    markets = [{"market": k, "demand_signals": v} for k, v in rows]
    return {"result": {"markets": markets, "basis": "demand signals by destination market (real evidence only)"},
            "citations": [CIT.metric_citation("positive_replies")]}


# --------------------------------------------------------------------- the registry
TOOLS = {
    "search_records": ToolDef("search_records", "Search internal records (allowlisted entities).", "read",
                              "read_only", {"entity": "required", "query": "optional", "filters": "optional",
                              "limit": "optional"}, _t_search),
    "get_metric": ToolDef("get_metric", "Compute a Phase-8 registry metric with its definition.", "read",
                          "read_only", {"key": "required", "days": "optional"}, _t_get_metric),
    "source_health": ToolDef("source_health", "Data-source freshness/health.", "read", "read_only", {},
                             _t_source_health),
    "summarize_opportunity": ToolDef("summarize_opportunity", "Summarize an opportunity (buyer-identity-free).",
                                     "read", "read_only", {"id": "required"}, _t_summarize_opportunity),
    "explain_opportunity_score": ToolDef("explain_opportunity_score", "Transparent opportunity score breakdown.",
                                         "read", "read_only", {"id": "required"}, _t_explain_score),
    "summarize_quote": ToolDef("summarize_quote", "Summarize a quote.", "read", "read_only", {"id": "required"},
                               _t_summarize_quote),
    "summarize_deal": ToolDef("summarize_deal", "Summarize a deal.", "read", "read_only", {"id": "required"},
                              _t_summarize_deal),
    "summarize_request": ToolDef("summarize_request", "Summarize a service request + its funnel.", "read",
                                 "read_only", {"id": "required"}, _t_summarize_request),
    "overdue_work_items": ToolDef("overdue_work_items", "List overdue open work-queue items.", "read",
                                  "read_only", {}, _t_overdue_work_items),
    "compare_markets": ToolDef("compare_markets", "Compare markets by real demand evidence.", "read",
                               "read_only", {}, _t_compare_markets),
}


def register(tool: ToolDef):
    """Register a tool (used by ai_actions/ai_drafts to add draft/write tools that require approval)."""
    TOOLS[tool.name] = tool


def tool_names():
    return list(TOOLS)


def read_only_tools():
    return [t for t in TOOLS.values() if t.mode == "read"]


def catalog():
    """A safe catalog for the model/UI — never exposes implementation."""
    return [{"name": t.name, "purpose": t.purpose, "mode": t.mode, "risk": t.risk,
             "requires_approval": t.requires_approval, "params": t.params} for t in TOOLS.values()]


class ToolDenied(PermissionError):
    pass


def run_tool(session, name, params, user, *, conversation_id=None, message_id=None):
    """Validate + execute a registered tool, audit it (summaries only), and return
    {ok, result, citations, error}. Refuses unknown tools, and refuses to AUTO-run a write/approval tool here
    (those go through the action-proposal + approval path)."""
    from . import ai_provider
    start = ai_provider.timed()
    if name not in TOOLS:
        _audit_tool(session, conversation_id, message_id, name, "read_only", params, "denied",
                    "unknown tool", 0)
        return {"ok": False, "result": None, "citations": [], "error": f"unknown tool '{name}'"}
    t = TOOLS[name]
    if t.mode != "read" or t.requires_approval:
        _audit_tool(session, conversation_id, message_id, name, t.risk, params, "denied",
                    "requires approval — not auto-runnable", 0)
        return {"ok": False, "result": None, "citations": [],
                "error": f"tool '{name}' requires an action approval and cannot be auto-run"}
    try:
        out = t.run(session, params or {}, user)
        dur = ai_provider.elapsed_ms(start)
        _audit_tool(session, conversation_id, message_id, name, t.risk, params, "ok",
                    _result_summary(out.get("result")), dur)
        return {"ok": True, "result": out.get("result"), "citations": out.get("citations", []), "error": ""}
    except Exception as ex:  # noqa: BLE001
        dur = ai_provider.elapsed_ms(start)
        _audit_tool(session, conversation_id, message_id, name, t.risk, params, "error", str(ex)[:200], dur)
        return {"ok": False, "result": None, "citations": [], "error": str(ex)[:200]}


def _params_summary(params) -> str:
    """A short, secret-free summary of the params (no full payloads/values that could carry sensitive data)."""
    if not params:
        return ""
    return ", ".join(f"{k}={str(v)[:40]}" for k, v in list(params.items())[:6] if k != "filters")


def _result_summary(result) -> str:
    if isinstance(result, dict):
        if "total" in result:
            return f"total={result['total']}"
        if "count" in result:
            return f"count={result['count']}"
        return f"keys={list(result)[:6]}"
    return type(result).__name__


def _audit_tool(session, conversation_id, message_id, name, risk, params, status, summary, dur):
    try:
        session.add(AIToolInvocation(conversation_id=conversation_id or 0, message_id=message_id,
                                     tool_name=name, risk_level=risk, params_summary=_params_summary(params),
                                     status=status, result_summary=(summary or "")[:200], duration_ms=dur))
    except Exception:  # noqa: BLE001
        pass
