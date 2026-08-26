"""Phase 9 — provider-neutral AI abstraction.

The copilot works DETERMINISTICALLY without any provider (see ai_command). This module is the OPTIONAL LLM
layer: honest status, a deterministic MockProvider (tests + offline canary), a NotConfigured default, and
env-gated real-provider stubs (no invented endpoints; a real client is a documented production-config item and
is NEVER called in tests). It also owns usage recording, budgets and the emergency Pause-All switch.

No API key is ever rendered or logged. Credentials come from the environment only.
"""
import json
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal

from sqlmodel import select

from . import config
from .models import AIUsageRecord

# The single HTTP choke point for ALL provider calls. Tests replace this with a mock so NO real network I/O ever
# happens in development/tests. It is only ever invoked when a real provider is explicitly configured + enabled.
_TRANSPORT = None


class ProviderError(RuntimeError):
    pass


class ProviderCancelled(RuntimeError):
    pass


def _default_transport(url, headers, body, timeout):
    req = urllib.request.Request(url, data=body, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:   # noqa: S310 — url is a fixed provider host
            return resp.status, json.loads(resp.read().decode() or "{}")
    except urllib.error.HTTPError as e:  # noqa: BLE001
        try:
            return e.code, json.loads(e.read().decode() or "{}")
        except Exception:  # noqa: BLE001
            return e.code, {}


def _post(url, headers, payload, timeout):
    fn = _TRANSPORT or _default_transport
    return fn(url, headers, json.dumps(payload).encode(), timeout)


def est_cost(model, in_tokens, out_tokens) -> str:
    """Rough USD cost estimate (Decimal-as-text) for budget/limit checks — not billing."""
    pin, pout = config.AI_MODEL_PRICES.get(model, config.AI_PRICE_DEFAULT)
    c = (Decimal(str(pin)) * Decimal(in_tokens) + Decimal(str(pout)) * Decimal(out_tokens)) / Decimal(1000)
    return str(c.quantize(Decimal("0.000001")))

# emergency Pause-All — CROSS-PROCESS. The flag is a sentinel file on the shared control dir (co-located with the
# DB volume in prod), so every gunicorn worker AND the separate worker container observe the same state. The
# in-process `_PAUSED` is only a fast-path fallback used if the filesystem is unreadable. Honored by every entry point.
_PAUSED = False


def _control_dir() -> str:
    import os
    d = config.AI_CONTROL_DIR
    if not d:
        from .config import DATABASE_URL, IS_LOCAL
        if not IS_LOCAL and DATABASE_URL.startswith("sqlite:///"):
            d = os.path.dirname(DATABASE_URL.replace("sqlite:///", "", 1)) or "/app/var"
        else:
            import tempfile
            d = tempfile.gettempdir()
    try:
        os.makedirs(d, exist_ok=True)
    except OSError:
        pass
    return d


def _flag_path(name: str) -> str:
    import os
    return os.path.join(_control_dir(), name)


def pause_all(paused: bool = True):
    """Set the emergency Pause-All flag for ALL processes. Writes the shared sentinel + the local fast-path."""
    global _PAUSED
    _PAUSED = bool(paused)
    import os
    path = _flag_path("ai_paused")
    try:
        if paused:
            with open(path, "w") as fh:
                fh.write("1")
        elif os.path.exists(path):
            os.remove(path)
    except OSError:
        pass          # filesystem unavailable → the in-process flag still applies within this process


def is_paused() -> bool:
    """True if AI is paused. The shared sentinel file is authoritative (cross-process); fall back to the
    in-process flag only if the filesystem can't be read."""
    import os
    try:
        return os.path.exists(_flag_path("ai_paused")) or _PAUSED
    except OSError:
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


class AnthropicProvider:
    """Real Anthropic Messages API adapter. Structured tool calls, strict timeout, sanitized payloads. All I/O
    goes through the mockable `_TRANSPORT`, so no live call happens in dev/tests."""
    configured = True
    name = "anthropic"

    def __init__(self, model):
        self.model = model

    def complete(self, *, messages, tools=None, max_tokens=None, timeout=None):
        if is_paused():
            raise ProviderPaused("AI is paused")
        base = (config.AI_API_BASE or "https://api.anthropic.com").rstrip("/")
        system, conv = "", []
        for m in messages:
            if m["role"] == "system":
                system = m["content"]
            else:
                conv.append({"role": "assistant" if m["role"] == "assistant" else "user",
                             "content": m["content"]})
        payload = {"model": self.model, "max_tokens": max_tokens or config.AI_MAX_OUTPUT_TOKENS,
                   "system": system, "messages": conv}
        if tools:
            payload["tools"] = tools
        headers = {"content-type": "application/json", "x-api-key": config.AI_API_KEY,
                   "anthropic-version": "2023-06-01"}
        status, data = _post(base + "/v1/messages", headers, payload, timeout or config.AI_TIMEOUT_S)
        if status != 200:
            raise ProviderError(f"anthropic {status}: {str(data)[:150]}")
        text, tool_calls = "", []
        for block in data.get("content", []):
            if block.get("type") == "text":
                text += block.get("text", "")
            elif block.get("type") == "tool_use":
                tool_calls.append({"tool": block.get("name"), "params": block.get("input", {}),
                                   "id": block.get("id", "")})
        u = data.get("usage", {})
        return ProviderResult(text=text, tool_calls=tool_calls, input_tokens=u.get("input_tokens", 0),
                              output_tokens=u.get("output_tokens", 0), model=self.model, provider=self.name)


class OpenAIProvider:
    """Real OpenAI Chat Completions adapter (structured tools). Same mockable transport; no live call in tests."""
    configured = True
    name = "openai"

    def __init__(self, model):
        self.model = model

    def complete(self, *, messages, tools=None, max_tokens=None, timeout=None):
        if is_paused():
            raise ProviderPaused("AI is paused")
        base = (config.AI_API_BASE or "https://api.openai.com").rstrip("/")
        payload = {"model": self.model, "max_tokens": max_tokens or config.AI_MAX_OUTPUT_TOKENS,
                   "messages": [{"role": m["role"], "content": m["content"]} for m in messages]}
        if tools:
            payload["tools"] = tools
            payload["tool_choice"] = "auto"
        headers = {"content-type": "application/json", "authorization": f"Bearer {config.AI_API_KEY}"}
        status, data = _post(base + "/v1/chat/completions", headers, payload, timeout or config.AI_TIMEOUT_S)
        if status != 200:
            raise ProviderError(f"openai {status}: {str(data)[:150]}")
        choice = (data.get("choices") or [{}])[0].get("message", {})
        text = choice.get("content") or ""
        tool_calls = []
        for tc in choice.get("tool_calls", []) or []:
            fn = tc.get("function", {})
            try:
                params = json.loads(fn.get("arguments") or "{}")
            except Exception:  # noqa: BLE001
                params = {}
            tool_calls.append({"tool": fn.get("name"), "params": params, "id": tc.get("id", "")})
        u = data.get("usage", {})
        return ProviderResult(text=text, tool_calls=tool_calls, input_tokens=u.get("prompt_tokens", 0),
                              output_tokens=u.get("completion_tokens", 0), model=self.model, provider=self.name)


def get_provider():
    """Resolve the configured provider. '' → NotConfigured; 'mock' → MockProvider; 'anthropic'/'openai' (with
    AI_ENABLED + a key + an allowlisted model) → the real adapter. A non-allowlisted model is treated as not
    configured, so an off-list model can never be called."""
    p = (config.AI_PROVIDER or "").lower()
    if p == "mock":
        return MockProvider()
    if p in ("anthropic", "openai") and config.AI_ENABLED and config.AI_API_KEY:
        model = config.AI_MODEL or ""
        if config.AI_MODEL_ALLOWLIST and model not in config.AI_MODEL_ALLOWLIST:
            return NotConfiguredProvider()          # model not allowlisted → refuse (never call off-list)
        return AnthropicProvider(model) if p == "anthropic" else OpenAIProvider(model)
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
