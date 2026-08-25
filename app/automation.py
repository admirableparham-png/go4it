"""Phase 9 — deterministic safe automation.

Rules are DETERMINISTIC (no AI at runtime; the AI may only RECOMMEND a rule as a proposal). Actions are limited
to internal, reversible outputs: create a WorkItem, create an internal alert, or prepare a draft summary.
Automation NEVER sends email, starts campaigns, issues quotes/contracts, advances Deals, moves funds, or
publishes without existing safe approval. Runs are idempotent (one per rule+condition_version), bounded by count
and wall-clock, per-rule isolated, previewable (dry-run), auto-paused after repeated failure, and globally
stoppable (Pause All).
"""
from datetime import datetime, timedelta

from sqlmodel import func, select

from .models import AutomationRule, AutomationRun, DemandSignal, Opportunity, WorkItem
from .pipeline import audit

TRIGGERS = ("work_queue_overdue", "source_stale", "new_opportunity", "demand_no_supply", "quote_expiring",
            "shipment_tracking_stale", "payment_overdue", "exception_opened", "scheduled_brief")
ACTIONS = ("create_work_item", "create_alert", "draft_summary", "assign_review")   # internal only
AUTO_PAUSE_AFTER = 3

_PAUSED = False


def pause_all(paused: bool = True):
    global _PAUSED
    _PAUSED = bool(paused)


def is_paused() -> bool:
    return _PAUSED


def create_rule(session, *, name, trigger_type, action_type, conditions=None, action_params=None,
                cadence="event", schedule_tz="UTC", tenant_id=None, actor=None, now=None):
    now = now or datetime.utcnow()
    if trigger_type not in TRIGGERS:
        return None, f"unknown trigger '{trigger_type}'"
    if action_type not in ACTIONS:
        return None, f"unknown/forbidden action '{action_type}'"   # only internal actions allowed
    import json
    rule = AutomationRule(name=name[:120], trigger_type=trigger_type, action_type=action_type,
                          conditions=json.dumps(conditions or {}), action_params=json.dumps(action_params or {}),
                          cadence=cadence, schedule_tz=schedule_tz, tenant_id=tenant_id,
                          created_by=getattr(actor, "id", None), created_at=now, updated_at=now)
    session.add(rule)
    session.flush()
    audit(session, actor, "automation_rule", rule.id, "rule_created",
          {"trigger": trigger_type, "action": action_type}, tenant_id=tenant_id)
    return rule, ""


def set_enabled(session, rule, enabled: bool, *, actor=None):
    rule.enabled = bool(enabled)
    rule.updated_at = datetime.utcnow()
    session.add(rule)
    audit(session, actor, "automation_rule", rule.id, "rule_enabled" if enabled else "rule_disabled", {},
          tenant_id=rule.tenant_id)
    return rule


# --------------------------------------------------------------------- condition evaluation (deterministic)
def _condition(session, rule, now):
    """(met, condition_version, detail). condition_version buckets the current condition instance so a repeat is
    idempotent and a material change re-fires."""
    t = rule.trigger_type
    if t == "work_queue_overdue":
        n = session.exec(select(func.count()).select_from(WorkItem).where(
            WorkItem.status.in_(("open", "in_progress", "waiting")), WorkItem.due_at != None,  # noqa: E711
            WorkItem.due_at < now)).one()
        return (n > 0, f"overdue:{n}:{now:%Y%m%d}", {"overdue": n})
    if t == "source_stale":
        from . import data_sources as DS
        stale = [s["label"] for s in DS.source_health(session) if s["freshness"] in ("Stale", "Failed")]
        return (bool(stale), f"stale:{len(stale)}:{now:%Y%m%d}", {"stale": stale})
    if t == "new_opportunity":
        import json
        thresh = (json.loads(rule.conditions or "{}") or {}).get("min_score", 70)
        opps = session.exec(select(Opportunity).where(Opportunity.score >= thresh,
                                                      Opportunity.status == "new")).all()
        return (bool(opps), f"newopp:{len(opps)}:{now:%Y%m%d}", {"opportunities": [o.reference for o in opps]})
    if t == "demand_no_supply":
        n = session.exec(select(func.count()).select_from(WorkItem).where(
            WorkItem.type == "high_demand_no_supply",
            WorkItem.status.in_(("open", "in_progress", "waiting")))).one()
        return (n > 0, f"nosupply:{n}:{now:%Y%m%d}", {"count": n})
    if t == "scheduled_brief":
        return (True, f"brief:{now:%Y%m%d}" if rule.cadence == "daily" else f"brief:{now:%Y%W}",
                {"cadence": rule.cadence})
    # generic count-based triggers over work-queue types
    typemap = {"quote_expiring": "quote_expired", "shipment_tracking_stale": "tracking_stale",
               "payment_overdue": "payment_overdue", "exception_opened": "cargo_damage_shortage"}
    if t in typemap:
        n = session.exec(select(func.count()).select_from(WorkItem).where(
            WorkItem.type == typemap[t], WorkItem.status.in_(("open", "in_progress", "waiting")))).one()
        return (n > 0, f"{t}:{n}:{now:%Y%m%d}", {"count": n})
    return (False, "", {})


def _do_action(session, rule, detail, *, actor, dry_run):
    if dry_run:
        return {"preview": f"would {rule.action_type} for {rule.trigger_type}", "detail": detail}
    import json
    params = json.loads(rule.action_params or "{}")
    if rule.action_type == "create_work_item":
        from . import work_queue as WQ
        wi = WQ.create_work_item_safe(
            session, tenant_id=rule.tenant_id, type=params.get("type", "admin_action_required"),
            title=(params.get("title") or f"Automation: {rule.name}")[:200],
            description=json.dumps(detail)[:2000], related_automation_id=rule.id,
            idempotency_key=f"automation:{rule.id}", condition_version=str(detail))
        return {"work_item_id": getattr(wi, "id", None)}
    if rule.action_type == "create_alert":
        from . import alerts as AL
        a, created = AL.raise_alert(session, alert_type=params.get("alert_type", "data_anomaly"),
                                    alert_key=f"automation:{rule.id}", condition_version=str(detail),
                                    title=(params.get("title") or rule.name)[:200], actor=actor)
        return {"alert_id": getattr(a, "id", None), "created": created}
    if rule.action_type in ("draft_summary", "assign_review"):
        return {"draft": f"{rule.action_type} prepared", "detail": detail, "sent": False, "published": False}
    return {"noop": True}


def run_rule(session, rule, *, now=None, dry_run=False, actor=None):
    """Evaluate + (idempotently) execute one rule. Returns (AutomationRun, ran_bool). Skips when paused/disabled,
    when the condition is unmet, when max_frequency hasn't elapsed, or when this condition_version already ran."""
    now = now or datetime.utcnow()
    if is_paused() or not rule.enabled:
        return None, False
    if rule.last_run and rule.max_frequency_hours and \
            (now - rule.last_run) < timedelta(hours=rule.max_frequency_hours) and not dry_run:
        return None, False
    met, cv, detail = _condition(session, rule, now)
    if not met:
        return None, False
    if not dry_run:
        # idempotency: this exact condition instance already REALLY ran → no-op (dry-runs never block a real run)
        if session.exec(select(AutomationRun).where(AutomationRun.rule_id == rule.id,
                                                    AutomationRun.condition_version == cv,
                                                    AutomationRun.status != "dry_run")).first():
            return None, False
    run = AutomationRun(rule_id=rule.id, condition_version=cv, status="dry_run" if dry_run else "ok",
                        started_at=now)
    try:
        out = _do_action(session, rule, detail, actor=actor, dry_run=dry_run)
        run.output_summary = str(out)[:500]
        run.related_workitem_id = out.get("work_item_id") if isinstance(out, dict) else None
        run.finished_at = now
        if dry_run:
            return run, True                            # a PREVIEW leaves no persisted run (no side effects)
        rule.last_run = now
        rule.failure_count = 0
        session.add(rule)
        session.add(run)
        session.flush()
        return run, True
    except Exception as e:  # noqa: BLE001
        run.status = "failed"
        run.error = str(e)[:300]
        run.finished_at = now
        if dry_run:
            return run, False                           # a preview failure is not persisted
        session.add(run)
        rule.failure_count = (rule.failure_count or 0) + 1
        if rule.failure_count >= AUTO_PAUSE_AFTER:
            rule.enabled = False
            _auto_pause_task(session, rule)
        session.add(rule)
        session.flush()
        return run, False


def run_due(session, *, now=None, budget=200, actor=None):
    """Bounded pass over enabled rules (count + wall-clock). Per-rule isolated: one failure never stops others."""
    now = now or datetime.utcnow()
    deadline = now + timedelta(seconds=5)
    ran = 0
    for rule in session.exec(select(AutomationRule).where(AutomationRule.enabled == True)).all():  # noqa: E712
        if ran >= budget or datetime.utcnow() > deadline:
            break
        try:
            _run, did = run_rule(session, rule, now=now, actor=actor)
            if did:
                ran += 1
        except Exception:  # noqa: BLE001 — never let one rule stop the pass
            continue
    return ran


def _auto_pause_task(session, rule):
    try:
        from . import work_queue as WQ
        WQ.create_work_item_safe(
            session, tenant_id=rule.tenant_id, type="automation_auto_paused", source="automatic", priority="high",
            title=f"Automation '{rule.name}' auto-paused after repeated failures",
            description=f"Rule {rule.id} disabled after {rule.failure_count} failures.",
            related_automation_id=rule.id, idempotency_key=f"automation_auto_paused:rule:{rule.id}",
            condition_version=str(rule.failure_count))
    except Exception:  # noqa: BLE001
        pass
