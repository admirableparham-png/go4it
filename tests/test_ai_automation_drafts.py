"""Phase 9 (B) — automation (internal-only actions, idempotency, dry-run, auto-pause, pause-all), draft
confidentiality (seller PII / buyer cost blocked, no placeholders), and the cited in-app brief."""
from datetime import datetime, timedelta

from sqlmodel import Session, select

from app import ai_brief
from app import ai_drafts
from app import automation as AUTO
from app.models import AutomationRun, User, WorkItem


def _admin(s):
    return s.exec(select(User).where(User.email == "admin@t.local")).one()


def test_automation_only_allows_internal_actions(ops_engine):
    with Session(ops_engine) as s:
        u = _admin(s)
        # a forbidden/external action is refused at rule creation
        for act in ("send_email", "start_campaign", "issue_quote", "advance_deal"):
            rule, err = AUTO.create_rule(s, name="x", trigger_type="work_queue_overdue", action_type=act,
                                         actor=u)
            assert rule is None and err
        ok, _ = AUTO.create_rule(s, name="ok", trigger_type="work_queue_overdue",
                                 action_type="create_work_item", actor=u)
        assert ok is not None


def test_automation_idempotent_and_dryrun_does_not_block(ops_engine):
    with Session(ops_engine) as s:
        u = _admin(s)
        s.add(WorkItem(type="x", status="open", due_at=datetime.utcnow() - timedelta(days=2))); s.commit()
        rule, _ = AUTO.create_rule(s, name="overdue", trigger_type="work_queue_overdue",
                                   action_type="create_work_item", actor=u); s.commit()
        drun, ddid = AUTO.run_rule(s, rule, dry_run=True, actor=u); s.commit()
        assert ddid and drun.status == "dry_run"
        run, did = AUTO.run_rule(s, rule, actor=u); s.commit()          # dry-run did NOT block the real run
        assert did and run.status == "ok"
        _run2, did2 = AUTO.run_rule(s, rule, actor=u)                   # same condition instance → no-op
        assert did2 is False


def test_automation_auto_pause_after_failures(ops_engine, monkeypatch):
    with Session(ops_engine) as s:
        u = _admin(s)
        rule, _ = AUTO.create_rule(s, name="boom", trigger_type="work_queue_overdue",
                                   action_type="create_work_item", actor=u)
        s.add(WorkItem(type="x", status="open", due_at=datetime.utcnow() - timedelta(days=2))); s.commit()
        # force the action to fail, and vary the condition_version each run so it re-fires
        monkeypatch.setattr(AUTO, "_do_action", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
        seq = iter(range(100))
        monkeypatch.setattr(AUTO, "_condition", lambda s2, r, now: (True, f"cv{next(seq)}", {}))
        for _ in range(AUTO.AUTO_PAUSE_AFTER):
            AUTO.run_rule(s, rule, actor=u); s.commit()
        s.refresh(rule)
        assert rule.enabled is False                                    # auto-paused
        assert s.exec(select(WorkItem).where(WorkItem.type == "automation_auto_paused")).first() is not None


def test_pause_all_stops_automation(ops_engine):
    with Session(ops_engine) as s:
        u = _admin(s)
        s.add(WorkItem(type="x", status="open", due_at=datetime.utcnow() - timedelta(days=2))); s.commit()
        rule, _ = AUTO.create_rule(s, name="r", trigger_type="work_queue_overdue",
                                   action_type="create_work_item", actor=u); s.commit()
        AUTO.pause_all(True)
        try:
            _run, did = AUTO.run_rule(s, rule, actor=u)
            assert did is False
        finally:
            AUTO.pause_all(False)


def test_seller_draft_blocks_buyer_pii(ops_engine):
    with Session(ops_engine) as s:
        u = _admin(s)
        try:
            ai_drafts.finalize_draft(s, "create_draft_seller_update",
                                     {"summary": "please email the buyer at cfo@acme.com"}, actor=u)
            assert False, "should block"
        except ValueError as ex:
            assert "PII" in str(ex)
        # a clean seller draft is allowed
        r = ai_drafts.finalize_draft(s, "create_draft_seller_update",
                                     {"summary": "Your shipment cleared export."}, actor=u)
        assert r["draft_ready"] and r["sent"] is False and r["published"] is False


def test_buyer_draft_blocks_cost_and_placeholder(ops_engine):
    with Session(ops_engine) as s:
        u = _admin(s)
        try:
            ai_drafts.finalize_draft(s, "create_draft_email", {"body": "our margin is 20%"}, actor=u)
            assert False
        except ValueError as ex:
            assert "cost/margin" in str(ex)
        try:
            ai_drafts.finalize_draft(s, "create_draft_email", {"body": "Dear {{name}}, hello"}, actor=u)
            assert False
        except ValueError as ex:
            assert "placeholder" in str(ex)


def test_brief_is_cited_and_in_app_only(ops_engine):
    with Session(ops_engine) as s:
        b = ai_brief.generate_brief(s, period="weekly")
        assert b["delivery"] == "in_app_only" and b["sections"]
        # at least one section item carries a citation (no auto-email; in-app)
        has_cite = any(it.get("citation") or it.get("citations") for sec in b["sections"] for it in sec["items"])
        assert has_cite
