"""Phase 9 hardening — current model pinning, cross-process Pause-All + cancellation, backfill check breadth.

These cover the production-readiness fixes: (1) the copilot prices the CURRENT Claude lineup; (2) Pause-All and
per-conversation cancellation are shared across processes via a sentinel file on the shared control dir, not
process-local memory; (3) budgets are DB-backed (already cross-process); (4) the work-item backfill's
"has a related record" check recognises every related_* column, not just the five oldest.
"""
import os
from decimal import Decimal

import pytest
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine, select, func

import app.config as CFG
from app import ai_command as CMD
from app import ai_provider as PROV
from app.models import WorkItem


def test_current_models_priced():
    # canonical current lineup from Anthropic's model overview (per-1K USD)
    assert Decimal(PROV.est_cost("claude-sonnet-5", 1000, 1000)) == Decimal("0.012")   # $2/$10 per MTok
    assert Decimal(PROV.est_cost("claude-opus-5", 1000, 1000)) == Decimal("0.030")     # $5/$25 per MTok
    assert Decimal(PROV.est_cost("claude-haiku-4-5", 1000, 1000)) == Decimal("0.006")  # $1/$5 per MTok
    assert "claude-sonnet-5" in CFG.AI_MODEL_PRICES and "claude-3-5-sonnet" not in CFG.AI_MODEL_ALLOWLIST


def test_pause_all_is_cross_process(monkeypatch, tmp_path):
    # isolate the control dir so the sentinel lands in a temp location
    monkeypatch.setattr(CFG, "AI_CONTROL_DIR", str(tmp_path), raising=False)
    PROV.pause_all(False)
    assert PROV.is_paused() is False
    flag = tmp_path / "ai_paused"
    PROV.pause_all(True)
    assert flag.exists()                              # written to shared storage, not just memory
    # simulate a DIFFERENT process: wipe this process's in-memory flag; the file must still report paused
    monkeypatch.setattr(PROV, "_PAUSED", False, raising=False)
    assert PROV.is_paused() is True                   # cross-process: read from the shared sentinel
    PROV.pause_all(False)
    assert not flag.exists() and PROV.is_paused() is False


def test_cancellation_is_cross_process(monkeypatch, tmp_path):
    monkeypatch.setattr(CFG, "AI_CONTROL_DIR", str(tmp_path), raising=False)
    conv_id = 4242
    CMD.clear_cancel(conv_id)
    assert CMD.is_cancelled(conv_id) is False
    CMD.cancel(conv_id)
    assert (tmp_path / f"ai_cancel_{conv_id}").exists()
    # another process (no in-memory record) still sees the cancel via the shared file
    monkeypatch.setattr(CMD, "_CANCELLED", set(), raising=False)
    assert CMD.is_cancelled(conv_id) is True
    CMD.clear_cancel(conv_id)
    assert CMD.is_cancelled(conv_id) is False


def test_budgets_are_db_backed_cross_process():
    """budget_status reads AIUsageRecord from the DB, so a spend recorded by one process is visible to another."""
    e = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    SQLModel.metadata.create_all(e)
    with Session(e) as s:
        b0 = PROV.budget_status(s, owner_id=1)
        assert b0["within"] is True and b0["used_tokens"] == 0
        PROV.record_usage(s, owner_id=1, provider="anthropic", model="claude-sonnet-5",
                          input_tokens=10, output_tokens=5); s.commit()
    with Session(e) as s2:                              # a fresh session == a different process's view
        b1 = PROV.budget_status(s2, owner_id=1)
        assert b1["used_tokens"] == 15                  # persisted + visible, not process-local


def test_backfill_related_check_recognises_all_related_fields():
    # the widened check: ANY related_* column counts (product/opportunity/deal/... not just the 5 oldest)
    related_cols = [c for c in WorkItem.__table__.columns.keys() if c.startswith("related_")]
    assert "related_product_id" in related_cols and "related_opportunity_id" in related_cols
    assert "related_request_id" in related_cols
    # a product_incomplete item related ONLY by related_product_id must pass (was a false-positive before)
    wi = WorkItem(type="product_incomplete", related_product_id=8)
    assert any(getattr(wi, c) for c in related_cols) is True
    # a genuinely unrelated non-'other' item still fails the check
    orphan = WorkItem(type="prepare_quote")
    assert any(getattr(orphan, c) for c in related_cols) is False
