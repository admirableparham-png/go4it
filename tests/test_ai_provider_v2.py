"""Phase 9 v2 — real provider adapters (mocked integration) + admin buyer-PII behavior.

NO live network ever happens: every provider call goes through the mockable `ai_provider._TRANSPORT`, which we
replace with a canned transport. Covers: Anthropic + OpenAI adapters (structured tool calls, usage, non-200 →
ProviderError, pause), the model allowlist, cost estimation, a BOUNDED read/tool/answer loop that halts, and the
admin-PII contract (admin display gets PII; provider payloads are minimized; the PII tool is never exposed to a
provider; seller-facing drafts are blocked; non-admin + cross-tenant access fail)."""
import json

import pytest
from sqlalchemy import text
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine, select

import app.config as CFG
from app import ai_command as CMD
from app import ai_drafts as DRAFTS
from app import ai_provider as PROV
from app import ai_tools as TOOLS
from app.ai_encryption import contains_pii, minimize_pii
from app.auth import hash_password
from app.models import AIUsageRecord, Company, Contact, User

_IDX = (
    "CREATE UNIQUE INDEX IF NOT EXISTS uq_workitem_idem_open ON workitem(idempotency_key) "
    "WHERE idempotency_key != '' AND status IN ('open','in_progress','waiting')",
    "CREATE UNIQUE INDEX IF NOT EXISTS uq_aiproposal_idem ON aiactionproposal(idempotency_key) "
    "WHERE idempotency_key != ''",
    "CREATE UNIQUE INDEX IF NOT EXISTS uq_aipromptversion ON aipromptversion(version) WHERE version != ''",
)


@pytest.fixture
def db():
    e = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    SQLModel.metadata.create_all(e)
    with e.connect() as c:
        for ddl in _IDX:
            c.execute(text(ddl))
        c.commit()
    with Session(e) as s:
        s.add(User(email="admin@t.local", name="A", role="admin", active=True, password_hash=hash_password("pw")))
        s.add(User(email="a@t.local", name="SA", role="agent", active=True, password_hash=hash_password("pw")))
        s.add(User(email="b@t.local", name="SB", role="agent", active=True, password_hash=hash_password("pw")))
        s.commit()
        seller_a = s.exec(select(User).where(User.email == "a@t.local")).one()
        seller_b = s.exec(select(User).where(User.email == "b@t.local")).one()
        # a managed buyer company for seller A, with contact PII
        ca = Company(name="ACME Importers", tenant_id=seller_a.id, country="GE", primary_role="buyer")
        s.add(ca); s.commit()
        s.add(Contact(company_id=ca.id, tenant_id=seller_a.id, name="Nino Buyer",
                      title="Head of Procurement", email="nino@acme-importers.ge", phone="+995 555 12 34 56"))
        # a different tenant's buyer company (seller B) — must never leak to A
        cb = Company(name="Rival Traders", tenant_id=seller_b.id, country="AE", primary_role="buyer")
        s.add(cb); s.commit()
        s.add(Contact(company_id=cb.id, tenant_id=seller_b.id, name="Omar Rival",
                      email="omar@rival.ae", phone="+971 50 000 0000"))
        s.commit()
    return e


def _u(s, email):
    return s.exec(select(User).where(User.email == email)).one()


def _company(s, name):
    return s.exec(select(Company).where(Company.name == name)).one()


# --------------------------------------------------------------------- a canned, no-network transport
class _Transport:
    """Replaces ai_provider._TRANSPORT. Records every outbound call and returns queued (status, json) responses."""

    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def __call__(self, url, headers, body, timeout):
        self.calls.append({"url": url, "headers": dict(headers), "payload": json.loads(body.decode())})
        if self.responses:
            return self.responses.pop(0)
        # default: a terminal text answer (anthropic + openai both tolerate the missing shape they don't read)
        return (200, {"content": [{"type": "text", "text": "done"}],
                      "usage": {"input_tokens": 1, "output_tokens": 1},
                      "choices": [{"message": {"content": "done", "tool_calls": []}}]})


def _enable(monkeypatch, provider, transport, *, model="claude-3-5-haiku"):
    monkeypatch.setattr(CFG, "AI_PROVIDER", provider, raising=False)
    monkeypatch.setattr(CFG, "AI_ENABLED", True, raising=False)
    monkeypatch.setattr(CFG, "AI_API_KEY", "test-key-not-real", raising=False)
    monkeypatch.setattr(CFG, "AI_MODEL", model, raising=False)
    monkeypatch.setattr(CFG, "AI_MODEL_ALLOWLIST", [model], raising=False)
    monkeypatch.setattr(PROV, "_TRANSPORT", transport, raising=False)


_ANTHROPIC_TOOL = (200, {"content": [{"type": "tool_use", "id": "t1", "name": "get_metric",
                                      "input": {"key": "open_work_queue"}}],
                        "usage": {"input_tokens": 10, "output_tokens": 5}})
_ANTHROPIC_TEXT = (200, {"content": [{"type": "text", "text": "You have items to review."}],
                        "usage": {"input_tokens": 8, "output_tokens": 4}})
_OPENAI_TOOL = (200, {"choices": [{"message": {"content": None, "tool_calls": [
    {"id": "c1", "function": {"name": "get_metric", "arguments": "{\"key\": \"open_work_queue\"}"}}]}}],
    "usage": {"prompt_tokens": 10, "completion_tokens": 5}})
_OPENAI_TEXT = (200, {"choices": [{"message": {"content": "You have items to review.", "tool_calls": []}}],
                     "usage": {"prompt_tokens": 8, "completion_tokens": 4}})


# --------------------------------------------------------------------- adapter unit (mocked integration)
def test_anthropic_adapter_parses_text_and_tools(monkeypatch):
    tr = _Transport([_ANTHROPIC_TOOL])
    _enable(monkeypatch, "anthropic", tr)
    prov = PROV.get_provider()
    assert prov.name == "anthropic"
    res = prov.complete(messages=[{"role": "user", "content": "hi"}],
                        tools=TOOLS.provider_tools("anthropic"), max_tokens=100, timeout=5)
    assert res.tool_calls == [{"tool": "get_metric", "params": {"key": "open_work_queue"}, "id": "t1"}]
    assert res.input_tokens == 10 and res.output_tokens == 5
    # sanitized payload: real host + versioned header, no secrets leaked back to us
    assert tr.calls[0]["url"].endswith("/v1/messages")
    assert tr.calls[0]["headers"]["anthropic-version"] == "2023-06-01"


def test_openai_adapter_parses_text_and_tools(monkeypatch):
    tr = _Transport([_OPENAI_TOOL])
    _enable(monkeypatch, "openai", tr, model="gpt-4o-mini")
    prov = PROV.get_provider()
    assert prov.name == "openai"
    res = prov.complete(messages=[{"role": "user", "content": "hi"}],
                        tools=TOOLS.provider_tools("openai"), max_tokens=100, timeout=5)
    assert res.tool_calls == [{"tool": "get_metric", "params": {"key": "open_work_queue"}, "id": "c1"}]
    assert res.input_tokens == 10 and res.output_tokens == 5
    assert tr.calls[0]["url"].endswith("/v1/chat/completions")


def test_allowlist_blocks_offlist_model(monkeypatch):
    _enable(monkeypatch, "anthropic", _Transport([]), model="claude-3-5-haiku")
    monkeypatch.setattr(CFG, "AI_MODEL", "claude-3-opus-EXPENSIVE", raising=False)  # not in allowlist
    prov = PROV.get_provider()
    assert prov.configured is False        # off-list model can never be called


def test_provider_requires_enabled_and_key(monkeypatch):
    _enable(monkeypatch, "anthropic", _Transport([]))
    monkeypatch.setattr(CFG, "AI_API_KEY", "", raising=False)
    assert PROV.get_provider().configured is False
    monkeypatch.setattr(CFG, "AI_API_KEY", "k", raising=False)
    monkeypatch.setattr(CFG, "AI_ENABLED", False, raising=False)
    assert PROV.get_provider().configured is False


def test_non_200_raises_provider_error(monkeypatch):
    tr = _Transport([(500, {"error": "boom"})])
    _enable(monkeypatch, "anthropic", tr)
    with pytest.raises(PROV.ProviderError):
        PROV.get_provider().complete(messages=[{"role": "user", "content": "x"}])


def test_pause_all_blocks_provider(monkeypatch):
    _enable(monkeypatch, "anthropic", _Transport([_ANTHROPIC_TEXT]))
    PROV.pause_all(True)
    try:
        with pytest.raises(PROV.ProviderPaused):
            PROV.get_provider().complete(messages=[{"role": "user", "content": "x"}])
    finally:
        PROV.pause_all(False)


def test_est_cost_is_deterministic():
    from decimal import Decimal
    # haiku: (0.0008*1000 + 0.004*1000)/1000 = 0.0048
    assert Decimal(PROV.est_cost("claude-3-5-haiku", 1000, 1000)) == Decimal("0.0048")
    assert Decimal(PROV.est_cost("mock-1", 100, 100)) == Decimal("0")


def test_default_transport_is_none_no_live_calls():
    # the module never ships a live transport; tests must inject one explicitly
    assert PROV._TRANSPORT is None


# --------------------------------------------------------------------- the BOUNDED read/tool/answer loop
def test_bounded_loop_runs_tool_then_answers(monkeypatch, db):
    tr = _Transport([_ANTHROPIC_TOOL, _ANTHROPIC_TEXT])
    _enable(monkeypatch, "anthropic", tr)
    with Session(db) as s:
        admin = _u(s, "admin@t.local")
        conv = CMD.new_conversation(s, admin); s.commit()
        res = CMD.answer(s, conv, "What needs my attention?", admin); s.commit()
        assert res["text"] == "You have items to review."
        assert res["tools"] == [] and res["refused"] is False  # provider path (deterministic 'results' empty)
        assert len(tr.calls) == 2                              # one tool round + one answer round
        # usage recorded for BOTH provider calls, tagged with the model
        usage = s.exec(select(AIUsageRecord).where(AIUsageRecord.conversation_id == conv.id)).all()
        assert len(usage) == 2 and all(u.provider == "anthropic" for u in usage)


def test_loop_is_bounded_when_model_never_stops(monkeypatch, db):
    monkeypatch.setattr(CFG, "AI_MAX_TOOL_STEPS", 2, raising=False)
    tr = _Transport([_ANTHROPIC_TOOL] * 10)   # model keeps asking for tools forever
    _enable(monkeypatch, "anthropic", tr)
    with Session(db) as s:
        admin = _u(s, "admin@t.local")
        conv = CMD.new_conversation(s, admin); s.commit()
        res = CMD.answer(s, conv, "loop please", admin); s.commit()
        assert len(tr.calls) <= CFG.AI_MAX_TOOL_STEPS + 1     # HARD cap: never an infinite loop
        assert res["text"]                                     # honest fallback, still answers


def test_cancel_short_circuits_provider(monkeypatch, db):
    tr = _Transport([_ANTHROPIC_TEXT])
    _enable(monkeypatch, "anthropic", tr)
    with Session(db) as s:
        admin = _u(s, "admin@t.local")
        conv = CMD.new_conversation(s, admin); s.commit()
        CMD.cancel(conv.id)                                   # cancelled before the turn
        try:
            res = CMD.answer(s, conv, "What needs my attention today?", admin); s.commit()
        finally:
            CMD.clear_cancel(conv.id)
        assert len(tr.calls) == 0                             # provider never called
        assert res["text"] and res["refused"] is False        # deterministic answer still served


def test_provider_failure_falls_back_and_raises_workitem(monkeypatch, db):
    from app.models import WorkItem
    tr = _Transport([(503, {"error": "unavailable"})])
    _enable(monkeypatch, "anthropic", tr)
    with Session(db) as s:
        admin = _u(s, "admin@t.local")
        conv = CMD.new_conversation(s, admin); s.commit()
        res = CMD.answer(s, conv, "What needs my attention today?", admin); s.commit()
        assert res["text"] and res["refused"] is False        # deterministic fallback served the user
        wi = s.exec(select(WorkItem).where(WorkItem.type == "ai_provider_failure")).first()
        assert wi is not None and wi.related_conversation_id == conv.id


# --------------------------------------------------------------------- admin buyer-PII behavior
def test_admin_can_retrieve_contact_pii(db):
    with Session(db) as s:
        admin = _u(s, "admin@t.local")
        cid = _company(s, "ACME Importers").id
        out = TOOLS.run_tool(s, "lookup_contact", {"company_id": cid}, admin)
        assert out["ok"] and out["contains_pii"] is True
        contacts = out["result"]["contacts"]
        assert contacts[0]["email"] == "nino@acme-importers.ge"      # admin legitimately sees PII
        assert contacts[0]["phone"].startswith("+995")


def test_pii_tool_excluded_from_provider(monkeypatch):
    for provider in ("anthropic", "openai"):
        names = [t.get("name") or t.get("function", {}).get("name") for t in TOOLS.provider_tools(provider)]
        assert "lookup_contact" not in names        # a model can never request buyer PII


def test_provider_payload_minimizes_pii(monkeypatch, db):
    tr = _Transport([_ANTHROPIC_TEXT])
    _enable(monkeypatch, "anthropic", tr)
    with Session(db) as s:
        admin = _u(s, "admin@t.local")
        conv = CMD.new_conversation(s, admin); s.commit()
        CMD.answer(s, conv, "Email nino@acme-importers.ge and visit https://acme-importers.ge now", admin)
        s.commit()
        blob = json.dumps(tr.calls[0]["payload"])
        assert "nino@acme-importers.ge" not in blob and "acme-importers.ge" not in blob
        assert "[email]" in blob and "[url]" in blob     # PII redacted before leaving the platform


def test_pii_telemetry_is_redacted(db):
    from app.models import AIToolInvocation
    with Session(db) as s:
        admin = _u(s, "admin@t.local")
        cid = _company(s, "ACME Importers").id
        TOOLS.run_tool(s, "lookup_contact", {"company_id": cid}, admin, conversation_id=None); s.commit()
        inv = s.exec(select(AIToolInvocation).where(AIToolInvocation.tool_name == "lookup_contact")).first()
        assert inv is not None
        assert "nino@acme-importers.ge" not in (inv.result_summary or "")   # telemetry carries no PII
        assert "+995" not in (inv.result_summary or "")


def test_seller_facing_draft_blocks_buyer_pii(db):
    with Session(db) as s:
        admin = _u(s, "admin@t.local")
        with pytest.raises(ValueError):
            DRAFTS.finalize_draft(s, "create_draft_seller_update",
                                  {"summary": "Buyer Nino at nino@acme-importers.ge wants 20t",
                                   "next_action": "call +995 555 12 34 56"}, actor=admin)


def test_non_admin_denied_contact_lookup(db):
    with Session(db) as s:
        seller = _u(s, "a@t.local")     # role agent — the copilot is admin-only; the tool refuses too
        cid = _company(s, "ACME Importers").id
        out = TOOLS.run_tool(s, "lookup_contact", {"company_id": cid}, seller)
        assert out["ok"] and "error" in out["result"]
        assert "contacts" not in out["result"]      # a seller never receives buyer PII


def test_cross_tenant_contact_lookup_fails(db):
    with Session(db) as s:
        seller_a = _u(s, "a@t.local")
        rival_id = _company(s, "Rival Traders").id       # belongs to seller B
        out = TOOLS.run_tool(s, "lookup_contact", {"company_id": rival_id}, seller_a)
        assert "contacts" not in out["result"]           # cross-tenant probe denied


def test_cross_tenant_search_scoped(db):
    from app import ai_search as SEARCH
    with Session(db) as s:
        seller_a = _u(s, "a@t.local")
        res = SEARCH.search(s, "companies", query="Rival", user=seller_a)
        names = [r.get("name") for r in res["rows"]]
        assert "Rival Traders" not in names              # seller A cannot see seller B's company


def test_minimize_pii_helpers():
    assert contains_pii("reach me at x@y.com") is True
    assert minimize_pii("call +1 415 555 2671 or x@y.com") == "call [phone] or [email]"
    assert contains_pii("no identifiers here") is False
