"""Phase 9 — the AI copilot orchestrator.

Deterministic-FIRST: a keyword intent router maps a question to registered read-only tools, then composes an
evidence-based, cited answer — with OR without an AI provider. When a provider is configured (and within budget,
not paused) it may phrase the answer, but the FACTS and CITATIONS always come from the tools, never the model.
Prompt-injection is detected on user input and any retrieved content is treated as untrusted data (never
instructions). The loop is bounded (max tool steps) and never runs a write/approval tool automatically.
"""
import re
from datetime import datetime, timedelta

from sqlmodel import select

from . import ai_citations as CIT
from . import ai_encryption as ENC
from . import ai_prompts as PROMPTS
from . import ai_provider as PROV
from . import ai_tools as TOOLS
from . import config
from .models import AIConversation, AIMessage
from .pipeline import audit

SUGGESTED = [
    "What needs my attention today?",
    "Which products have verified demand this month?",
    "Which opportunities lack matching supply?",
    "Which lead sources produce positive replies?",
    "Show quotes expiring this week.",
    "Summarize Deal G4-...",
    "Which shipments have stale tracking?",
    "Which sellers can supply this opportunity?",
    "Explain why this opportunity has a high score.",
    "Prepare a weekly intelligence brief.",
    "Start a buyer-research proposal for this product and market.",
]

# prompt-injection signatures — matched on USER input and on any retrieved/untrusted content
_INJECTION = [
    re.compile(r"ignore (all )?(previous|prior|above) instructions", re.I),
    re.compile(r"(reveal|show|print|repeat).{0,20}(system|hidden) (prompt|instructions|message)", re.I),
    re.compile(r"disregard (the )?(system|your) (prompt|rules|instructions)", re.I),
    re.compile(r"you are now|new instructions:|act as (a )?(dan|developer mode)", re.I),
    re.compile(r"\b(send|email) (this|the) (email|message)\b", re.I),
    re.compile(r"\b(run|execute) (sql|shell|code|command)\b", re.I),
    re.compile(r"read (the )?(env|environment|secrets|credentials|api ?key)", re.I),
    re.compile(r"contact all buyers|email all (buyers|sellers)", re.I),
    re.compile(r"disable suppression|bypass suppression|turn off confidentiality", re.I),
]


def detect_injection(text: str) -> list:
    return [p.pattern for p in _INJECTION if p.search(text or "")]


# cooperative cancellation for a running provider turn (checked between bounded tool steps). CROSS-PROCESS: the
# cancel request may land on a different gunicorn worker than the one running the turn, so the flag is a shared
# sentinel file (co-located with the DB volume). `_CANCELLED` is an in-process fast-path fallback only.
_CANCELLED = set()


def _cancel_flag(conversation_id) -> str:
    return PROV._flag_path(f"ai_cancel_{int(conversation_id)}")


def cancel(conversation_id):
    _CANCELLED.add(int(conversation_id))
    try:
        with open(_cancel_flag(conversation_id), "w") as fh:
            fh.write("1")
    except OSError:
        pass


def clear_cancel(conversation_id):
    _CANCELLED.discard(int(conversation_id))
    try:
        import os
        p = _cancel_flag(conversation_id)
        if os.path.exists(p):
            os.remove(p)
    except OSError:
        pass


def is_cancelled(conversation_id) -> bool:
    try:
        import os
        if os.path.exists(_cancel_flag(conversation_id)):
            return True
    except OSError:
        pass
    return int(conversation_id) in _CANCELLED


def _provider_loop(session, conversation, user_text, user, prov, now):
    """A BOUNDED conversational read/tool/answer cycle: the model may call registered READ-ONLY tools (structured
    calls, validated); results are fed back MINIMIZED (buyer PII redacted before leaving the platform); iterations
    are capped; a wall-clock deadline + cooperative cancel stop it. Write/PII/unknown tools are never executed
    here. Facts + citations come from the tools, not the model."""
    from . import ai_tools as TOOLS
    system = PROMPTS.SYSTEM_PROMPT
    msgs = [{"role": "system", "content": system},
            {"role": "user", "content": ENC.minimize_pii(user_text)}]   # PII minimized before any external send
    tools = TOOLS.provider_tools(getattr(prov, "name", "anthropic"))
    citations, tool_count, text = [], 0, ""
    deadline = now + timedelta(seconds=config.AI_TIMEOUT_S * (config.AI_MAX_TOOL_STEPS + 1))
    partial = False
    for _step in range(config.AI_MAX_TOOL_STEPS + 1):
        if is_cancelled(conversation.id) or datetime.utcnow() > deadline:
            partial = True
            break
        res = prov.complete(messages=msgs, tools=tools, max_tokens=config.AI_MAX_OUTPUT_TOKENS,
                            timeout=config.AI_TIMEOUT_S)
        PROV.record_usage(session, conversation_id=conversation.id, provider=res.provider, model=res.model,
                          owner_id=conversation.owner_id, tenant_id=conversation.tenant_id,
                          input_tokens=res.input_tokens, output_tokens=res.output_tokens,
                          tool_calls=len(res.tool_calls),
                          est_cost=PROV.est_cost(res.model, res.input_tokens, res.output_tokens))
        if res.tool_calls:
            msgs.append({"role": "assistant", "content": res.text or "(requesting tools)"})
            for tc in res.tool_calls[: config.AI_MAX_TOOL_STEPS]:
                out = TOOLS.run_tool(session, tc.get("tool"), tc.get("params", {}), user,
                                     conversation_id=conversation.id)
                tool_count += 1
                if out.get("ok"):
                    citations.extend(out.get("citations", []))
                    payload = ENC.minimize_pii(str(out.get("result"))[:2000])   # never send raw PII to a model
                    msgs.append({"role": "user", "content": f"tool {tc.get('tool')} result: {payload}"})
                else:
                    msgs.append({"role": "user", "content": f"tool {tc.get('tool')} refused: {out.get('error')}"})
            continue
        text = res.text or ""
        break
    return {"text": text, "citations": citations, "tool_count": tool_count,
            "provider": getattr(prov, "name", ""), "model": getattr(prov, "model", ""), "partial": partial}


def _provider_failure_task(session, conversation, err):
    try:
        from . import work_queue as WQ
        WQ.create_work_item_safe(
            session, tenant_id=conversation.tenant_id, type="ai_provider_failure", source="automatic",
            title="AI provider failure", description=(err or "")[:300],
            related_conversation_id=conversation.id,
            idempotency_key=f"ai_provider_failure:conv:{conversation.id}",
            condition_version=datetime.utcnow().strftime("%Y%m%d%H"))
    except Exception:  # noqa: BLE001
        pass


def neutralize(text: str) -> str:
    """Render untrusted retrieved content safely as quoted data (never executed as instructions)."""
    return (text or "").replace("`", "'")[:2000]


# --------------------------------------------------------------------- message storage (encrypted)
def store_message(session, conversation, *, role, content, status="complete", provider="", model="",
                  prompt_version="", citation_count=0, tool_count=0, partial=False, error="", now=None):
    now = now or datetime.utcnow()
    clean = ENC.redact_secrets(content or "")           # strip secrets BEFORE encryption/persistence
    msg = AIMessage(conversation_id=conversation.id, role=role, content_enc=ENC.ai_encrypt(clean),
                    status=status, provider=provider, model=model, prompt_version=prompt_version,
                    citation_count=citation_count, tool_count=tool_count, partial=partial, error=error,
                    sensitivity=conversation.sensitivity, created_at=now,
                    completed_at=now if status in ("complete", "failed", "cancelled") else None)
    session.add(msg)
    session.flush()
    return msg


def message_text(msg) -> str:
    """Decrypt a stored message for display (admin-only callers)."""
    return ENC.ai_decrypt(msg.content_enc)


# --------------------------------------------------------------------- intent router (deterministic)
_TRACK = re.compile(r"\b(G4-[A-Za-z0-9\-]+|OPP-\d{6}-\d{4})\b")


def _plan(text: str) -> list:
    """Map a question to an ordered list of (tool, params). Deterministic + honest; unknown → empty plan."""
    t = (text or "").lower()
    plan = []
    if any(w in t for w in ("attention", "today", "urgent", "what's happening", "whats happening")):
        plan += [("overdue_work_items", {}), ("get_metric", {"key": "open_work_queue"}),
                 ("get_metric", {"key": "operational_exceptions"}),
                 ("get_metric", {"key": "quotes_awaiting"})]
    if "demand" in t or ("products" in t and "demand" in t):
        plan += [("compare_markets", {}), ("get_metric", {"key": "positive_replies", "days": 30}),
                 ("search_records", {"entity": "demand_signals", "limit": 10})]
    if "opportunit" in t and ("supply" in t or "lack" in t or "missing" in t):
        plan += [("search_records", {"entity": "opportunities", "limit": 10})]
    if ("source" in t or "lead source" in t) and ("positive" in t or "reply" in t or "quality" in t):
        plan += [("get_metric", {"key": "positive_replies"}), ("source_health", {})]
    if "quote" in t and ("expir" in t or "awaiting" in t or "week" in t):
        plan += [("get_metric", {"key": "quotes_awaiting"}),
                 ("search_records", {"entity": "quotes", "filters": {"status": "sent"}, "limit": 10})]
    if "shipment" in t and ("stale" in t or "tracking" in t):
        plan += [("search_records", {"entity": "shipments", "filters": {"current_milestone": "in_transit"},
                                      "limit": 10})]
    m = _TRACK.search(text or "")
    if m and "deal" in t:
        plan += [("_deal_by_code", {"code": m.group(1)})]
    if "explain" in t and ("score" in t or "opportunit" in t):
        plan += [("_explain_by_ref", {"ref": m.group(1) if m else ""})]
    if "seller" in t and "opportunit" in t:
        plan += [("_opp_matches_by_ref", {"ref": m.group(1) if m else ""})]
    if not plan and ("metric" in t or "explain" in t):
        # metric-name lookup
        from . import metrics as M
        for k in M.METRICS:
            if k.replace("_", " ") in t:
                plan.append(("get_metric", {"key": k}))
                break
    return plan[: config.AI_MAX_TOOL_STEPS]


def _resolve_special(session, tool, params, user):
    """Handle router-only pseudo-tools that resolve a reference to a real tool call."""
    from .models import Deal, Opportunity
    if tool == "_deal_by_code":
        d = session.exec(select(Deal).where(Deal.tracking_code == params["code"])).first()
        if not d:
            return {"ok": False, "error": f"no deal {params['code']}", "result": None, "citations": []}
        return TOOLS.run_tool(session, "summarize_deal", {"id": d.id}, user)
    if tool in ("_explain_by_ref", "_opp_matches_by_ref"):
        ref = params.get("ref") or ""
        o = session.exec(select(Opportunity).where(Opportunity.reference == ref)).first() if ref else None
        if not o:
            return {"ok": False, "error": "specify an opportunity reference (OPP-…)", "result": None,
                    "citations": []}
        return TOOLS.run_tool(session, "explain_opportunity_score" if tool == "_explain_by_ref"
                              else "summarize_opportunity", {"id": o.id}, user)
    return {"ok": False, "error": "unknown", "result": None, "citations": []}


def _compose(question, results):
    """Compose a plain, honest answer from tool results. Facts come only from the tools."""
    if not results:
        return ("I could not map that to Go4it data I can verify. Try one of the suggested questions, or ask "
                "about a specific metric, opportunity, deal, quote, request or shipment.")
    lines = []
    for name, out in results:
        r = out.get("result") or {}
        if name == "overdue_work_items":
            lines.append(f"- Overdue work-queue items: {r.get('count', 0)}.")
        elif name == "get_metric":
            v = r.get("value")
            if r.get("per_currency"):
                v = ", ".join(f"{c} {a}" for c, a in (v or {}).items()) or "none"
            lines.append(f"- {r.get('label', name)}: {v} ({r.get('range', '')}; {r.get('metric_version', '')}).")
        elif name == "compare_markets":
            top = ", ".join(f"{m['market']} ({m['demand_signals']})" for m in r.get("markets", [])[:5]) or "none"
            lines.append(f"- Markets by real demand evidence: {top}.")
        elif name == "source_health":
            stale = [s["label"] for s in r.get("sources", []) if s["freshness"] in ("Stale", "Failed")]
            lines.append("- Data sources: " + ("all fresh." if not stale else "stale/failed: " + ", ".join(stale)))
        elif name == "search_records":
            lines.append(f"- {r.get('entity', 'records')}: {r.get('total', 0)} match(es).")
        elif name in ("summarize_deal", "summarize_quote", "summarize_opportunity", "explain_opportunity_score",
                      "summarize_request"):
            if r.get("error"):
                lines.append(f"- {name}: {r['error']}.")
            else:
                lines.append(f"- {name.replace('_', ' ')}: " + ", ".join(f"{k} {v}" for k, v in r.items()
                             if k in ("tracking_code", "reference", "stage", "status", "score", "total"))[:200])
    if not lines:
        return "I could not verify this from current Go4it data."
    return "Here's what I found (every figure links to its evidence):\n" + "\n".join(lines)


# --------------------------------------------------------------------- the public entry
def answer(session, conversation, user_text, user, *, now=None):
    """Answer a user turn: store the (secret-redacted) user message, refuse on injection, run the deterministic
    plan over read-only tools, compose a cited answer, store + cite the assistant message. Returns a dict with
    the assistant message id, text, citations and any refusal/injection flag. Never mutates business data."""
    now = now or datetime.utcnow()
    PROMPTS.ensure_active(session, actor=user)
    store_message(session, conversation, role="user", content=user_text, now=now)

    # prompt-injection defense — refuse safely, flag for review, do NOT comply
    hits = detect_injection(user_text)
    if hits:
        txt = ("I can't follow instructions that try to override my rules, reveal system configuration, send "
               "messages, run code, or bypass safety controls. I flagged this for review. I can still help with "
               "evidence-based questions about Go4it data.")
        msg = store_message(session, conversation, role="assistant", content=txt,
                            prompt_version=PROMPTS.PROMPT_VERSION, now=now)
        audit(session, user, "ai_conversation", conversation.id, "prompt_injection_detected",
              {"patterns": hits[:5]}, tenant_id=conversation.tenant_id)
        _injection_task(session, conversation, user)
        conversation.updated_at = now
        session.add(conversation)
        return {"message_id": msg.id, "text": txt, "citations": [], "refused": True, "injection": True,
                "tools": []}

    citations, tool_count, results = [], 0, []
    provider_name, model_name, partial = "", "", False
    prov = PROV.get_provider()
    use_provider = getattr(prov, "configured", False) and not PROV.is_paused() \
        and not is_cancelled(conversation.id)
    if use_provider and not PROV.budget_status(session, owner_id=conversation.owner_id, now=now)["within"]:
        use_provider = False        # over budget → deterministic answer (never blocks the admin)
    if use_provider:
        try:
            loop = _provider_loop(session, conversation, user_text, user, prov, now)
            text = loop["text"] or "I could not verify this from current Go4it data."
            citations.extend(loop["citations"])
            tool_count = loop["tool_count"]
            provider_name, model_name, partial = loop["provider"], loop["model"], loop["partial"]
        except (PROV.ProviderError, PROV.ProviderPaused, PROV.ProviderNotConfigured, Exception) as ex:  # noqa: BLE001
            use_provider = False
            _provider_failure_task(session, conversation, str(ex))
    if not use_provider:
        # deterministic plan → bounded read-only tool loop (facts from tools, honest fallback when unmapped)
        plan = _plan(user_text)
        for tool, params in plan:
            if tool.startswith("_"):
                out = _resolve_special(session, tool, params, user)
            else:
                out = TOOLS.run_tool(session, tool, params, user, conversation_id=conversation.id)
            tool_count += 1
            if out.get("ok", True) and out.get("result") is not None:
                results.append((tool if not tool.startswith("_") else _special_display(tool), out))
                citations.extend(out.get("citations", []))
        text = _compose(user_text, results)

    # intent: a weekly/daily intelligence brief (read-only, cited, in-app)
    tl = user_text.lower()
    if "brief" in tl and ("intelligence" in tl or "weekly" in tl or "daily" in tl or "prepare" in tl):
        from . import ai_brief
        b = ai_brief.generate_brief(session, period="weekly" if "week" in tl else "daily")
        recs = "; ".join(b["recommendations"]) or "no urgent recommendations"
        text = (f"Intelligence brief ({b['period']}, in-app only). Sections: "
                + ", ".join(sec["title"] for sec in b["sections"]) + f". Recommended: {recs}.")
        for sec in b["sections"]:
            for it in sec["items"]:
                citations.extend(it.get("citations", []) or ([it["citation"]] if it.get("citation") else []))

    # intent: propose a buyer-research job (never launched without approval)
    proposals = []
    if "research" in tl and ("start" in tl or "proposal" in tl or "find buyers" in tl or "buyer-research" in tl):
        from . import ai_actions
        scope = user_text
        prop, _err = ai_actions.propose(
            session, conversation, action_type="start_research",
            payload={"prompt": scope}, reason="Admin asked to start buyer research.",
            target_summary=f"Buyer-research job for: {scope[:120]}", actor=user, message_id=None)
        if prop:
            proposals.append(prop.id)
            text += ("\n\nI've prepared a Research proposal (interpreted scope shown). It will run through the "
                     "existing Research pipeline ONLY after you approve it — I won't claim results before then.")

    # NOTE: usage is recorded inside _provider_loop (per provider call). The deterministic path records none.
    msg = store_message(session, conversation, role="assistant", content=text, provider=provider_name,
                        model=model_name, prompt_version=PROMPTS.PROMPT_VERSION,
                        citation_count=len(citations), tool_count=tool_count, now=now)
    CIT.persist(session, conversation_id=conversation.id, message_id=msg.id, citations=citations)
    conversation.updated_at = now
    if conversation.title == "New conversation":
        conversation.title = (user_text or "Conversation").strip()[:60]
    session.add(conversation)
    return {"message_id": msg.id, "text": text, "citations": citations, "refused": False, "injection": False,
            "tools": [t for t, _ in results], "proposals": proposals}


def _special_display(tool):
    return {"_deal_by_code": "summarize_deal", "_explain_by_ref": "explain_opportunity_score",
            "_opp_matches_by_ref": "summarize_opportunity"}.get(tool, tool)


def _injection_task(session, conversation, user):
    try:
        from . import work_queue as WQ
        WQ.create_work_item_safe(
            session, tenant_id=conversation.tenant_id, type="prompt_injection_review", source="automatic",
            priority="high", title="Prompt-injection attempt in AI Command",
            description="A copilot message tried to override safety controls; refused. Review the conversation.",
            related_conversation_id=conversation.id,
            idempotency_key=f"prompt_injection_review:conv:{conversation.id}",
            condition_version=datetime.utcnow().strftime("%Y%m%d%H"))
    except Exception:  # noqa: BLE001
        pass


def new_conversation(session, user, *, tenant_id=None, title="New conversation", now=None):
    now = now or datetime.utcnow()
    conv = AIConversation(owner_id=user.id if user else None, tenant_id=tenant_id, title=title,
                          created_at=now, updated_at=now)
    session.add(conv)
    session.flush()
    return conv
