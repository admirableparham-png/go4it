"""Phase 9 — provider-neutral AI abstraction.

The copilot works DETERMINISTICALLY without any provider (see ai_command). This module is the OPTIONAL LLM
layer: honest status, a deterministic MockProvider (tests + offline canary), a NotConfigured default, and
env-gated real-provider stubs (no invented endpoints; a real client is a documented production-config item and
is NEVER called in tests). It also owns usage recording, budgets and the emergency Pause-All switch.

No API key is ever rendered or logged. Credentials come from the environment only.
"""
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from sqlmodel import select

from . import config
from .models import AIUsageRecord

# emergency Pause-All (runtime, in-process). A real deployment would persist this; honored by every AI entry point.
_PAUSED = False


def pause_all(paused: bool = True):
    global _PAUSED
    _PAUSED = bool(paused)


def is_paused() -> bool:
    return _PAUSED


class ProviderNotConfigured(RuntimeError):
    pass


class ProviderPaused(RuntimeError):
    pass


@dataclass
class ProviderResult:
    text: str = ""
    tool_calls: list = field(default_factory=list)   # [{"tool": name, "params": {...}}]
    input_tokens: int = 0
    output_tokens: int = 0
    model: str = ""
    provider: str = ""


class NotConfiguredProvider:
    configured = False
    name = ""
    model = ""

    def complete(self, *a, **k):
        raise ProviderNotConfigured("AI provider not configured")


class MockProvider:
    """A deterministic, offline provider for tests + the offline canary. It performs NO network I/O and returns a
    canned structured result. It is NEVER a paid/live model."""
    configured = True
    name = "mock"
    model = "mock-1"

    def complete(self, *, messages, tools=None, max_tokens=None, timeout=None):
        if is_paused():
            raise ProviderPaused("AI is paused")
        last = messages[-1]["content"] if messages else ""
        text = f"[mock] {last[:200]}"
        return ProviderResult(text=text, tool_calls=[], input_tokens=len(last) // 4,
                              output_tokens=len(text) // 4, model=self.model, provider=self.name)


class _LiveProviderStub:
    """A real provider (anthropic/openai/…). Wiring a live client is a documented PRODUCTION-CONFIG item; this
    build never calls a paid endpoint (and never during tests). `complete` fails closed until a client is wired."""
    configured = True

    def __init__(self, name, model):
        self.name = name
        self.model = model

    def complete(self, *, messages, tools=None, max_tokens=None, timeout=None):
        raise ProviderNotConfigured(
            f"live provider '{self.name}' is not wired in this build — configure it via the production AI canary")


def get_provider():
    """Resolve the configured provider. '' → NotConfigured; 'mock' → MockProvider; a real name (with AI_ENABLED +
    a key + an allowlisted model) → a live stub. Never returns a provider that would silently call a paid model."""
    p = (config.AI_PROVIDER or "").lower()
    if p == "mock":
        return MockProvider()
    if p in ("anthropic", "openai") and config.AI_ENABLED and config.AI_API_KEY:
        model = config.AI_MODEL or ""
        if config.AI_MODEL_ALLOWLIST and model not in config.AI_MODEL_ALLOWLIST:
            return NotConfiguredProvider()          # model not allowlisted → treat as not configured
        return _LiveProviderStub(p, model)
    return NotConfiguredProvider()


def provider_status() -> dict:
    prov = get_provider()
    return {
        "configured": bool(getattr(prov, "configured", False)),
        "provider": getattr(prov, "name", ""),
        "model": getattr(prov, "model", ""),
        "allowlist": list(config.AI_MODEL_ALLOWLIST),
        "paused": is_paused(),
        "message": "" if getattr(prov, "configured", False) else "AI provider not configured",
    }


def live_allowed(user) -> bool:
    """Only an allowlisted admin email may use a REAL provider (the live-canary gate)."""
    email = (getattr(user, "email", "") or "").lower()
    return bool(config.AI_LIVE_ALLOWLIST) and email in config.AI_LIVE_ALLOWLIST


# --------------------------------------------------------------------- usage + budgets
def record_usage(session, *, conversation_id=None, message_id=None, provider="", model="", input_tokens=0,
                 output_tokens=0, latency_ms=0, tool_calls=0, est_cost="0", tenant_id=None, owner_id=None,
                 success=True, cache_hit=False):
    rec = AIUsageRecord(conversation_id=conversation_id, message_id=message_id, provider=provider, model=model,
                        input_tokens=input_tokens, output_tokens=output_tokens, latency_ms=latency_ms,
                        tool_calls=tool_calls, est_cost=str(est_cost), tenant_id=tenant_id, owner_id=owner_id,
                        success=success, cache_hit=cache_hit)
    session.add(rec)
    return rec


def daily_tokens(session, owner_id, *, now=None) -> int:
    now = now or datetime.utcnow()
    since = now - timedelta(days=1)
    rows = session.exec(select(AIUsageRecord).where(AIUsageRecord.owner_id == owner_id,
                                                    AIUsageRecord.created_at >= since)).all()
    return sum((r.input_tokens or 0) + (r.output_tokens or 0) for r in rows)


def budget_status(session, *, owner_id=None, tenant_id=None, now=None) -> dict:
    """Whether the admin/tenant is within budget. Deterministic answers stay available even when over budget —
    only the LLM layer is gated."""
    used = daily_tokens(session, owner_id, now=now) if owner_id is not None else 0
    limit = config.AI_DAILY_TOKEN_LIMIT
    within = used < limit
    return {"within": within, "used_tokens": used, "daily_limit": limit,
            "message": "" if within else "Daily AI token limit reached — deterministic answers still available."}


def timed():
    """Small helper to measure latency in ms."""
    return time.monotonic()


def elapsed_ms(start) -> int:
    return int((time.monotonic() - start) * 1000)
