"""Phase 3 — Requests + Work Queue.

Admin-only coordination layer. Sellers must never reach the Work Queue or internal tasks; a requester-visible
action carries no buyer/supplier PII and is published only through the sanitized SellerUpdate path; work items
never cross a tenant boundary. Automatic creation is non-blocking (it can never fail the operational event)
and idempotent (re-sync creates no duplicate open task). The additive workflow_status + status history enrich
the request surface without changing the legacy status meaning.
"""
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine, select

import app.main as main
from app import request_service as RS
from app import work_queue as WQ
from app.auth import hash_password
from app.models import (AuditLog, DuplicateCandidate, Lead, Outreach, Product, Quote, RequestStatusEvent,
                        SellerUpdate, ServiceRequest, User, WorkItem)


def _indexes(engine):
    with engine.connect() as conn:
        conn.execute(text("CREATE UNIQUE INDEX IF NOT EXISTS uq_workitem_idem_open ON workitem(idempotency_key)"
                          " WHERE idempotency_key != '' AND status IN ('open','in_progress','waiting')"))
        conn.commit()


@pytest.fixture
def ctx(monkeypatch):
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    SQLModel.metadata.create_all(engine)
    _indexes(engine)
    monkeypatch.setattr(main, "engine", engine)
    ids = {}
    with Session(engine) as s:
        for email, role in [("admin@t.local", "admin"), ("kim@t.local", "agent"), ("bo@t.local", "agent")]:
            s.add(User(email=email, name=email.split("@")[0], role=role, active=True,
                       password_hash=hash_password("pw")))
        s.commit()
        ids = {u.email.split("@")[0]: u.id for u in s.exec(select(User)).all()}
    return TestClient(main.app), engine, ids


def _login(client, email):
    assert client.post("/login", data={"email": email, "password": "pw"},
                       follow_redirects=False).status_code == 303


def _submit(client, product="Widgets", rtype="buyer_hunt", market="Iraq"):
    assert client.post("/requests", data={"product": product, "request_type": rtype, "market": market},
                       follow_redirects=False).status_code == 303


def _one_request(engine):
    with Session(engine) as s:
        return s.exec(select(ServiceRequest).order_by(ServiceRequest.id.desc())).first()


# ------------------------------------------------------------- authorization / confidentiality
def test_work_queue_is_admin_only(ctx):
    client, engine, ids = ctx
    _login(client, "kim@t.local")
    assert client.get("/admin/work-queue").status_code == 403
    assert client.get("/admin/work-queue/count").status_code == 403
    assert client.post("/admin/work-queue/create", data={"title": "x", "type": "other"},
                       follow_redirects=False).status_code == 403
    assert client.post("/admin/work-queue/bulk", data={"action": "complete"},
                       follow_redirects=False).status_code == 403
    c2 = TestClient(main.app)
    _login(c2, "admin@t.local")
    assert c2.get("/admin/work-queue").status_code == 200


def test_missing_work_item_action_404_and_seller_403(ctx):
    client, engine, ids = ctx
    _login(client, "admin@t.local")
    assert client.post("/admin/work-queue/9999/action", data={"action": "complete"},
                       follow_redirects=False).status_code == 404
    seller = TestClient(main.app)
    _login(seller, "kim@t.local")
    assert client and seller.post("/admin/work-queue/1/action", data={"action": "complete"},
                                  follow_redirects=False).status_code == 403


def test_manual_task_tenant_derived_from_record_not_user(ctx):
    """A manual work item is stamped with the RELATED record's tenant, so it can never cross a boundary."""
    client, engine, ids = ctx
    # a request owned by seller kim
    kc = TestClient(main.app)
    _login(kc, "kim@t.local")
    _submit(kc)
    sr = _one_request(engine)
    _login(client, "admin@t.local")
    client.post("/admin/work-queue/create", data={"title": "Follow up", "type": "follow_up_seller",
                "related_request_id": str(sr.id)}, follow_redirects=False)
    with Session(engine) as s:
        wi = s.exec(select(WorkItem).where(WorkItem.source == "manual")).first()
        assert wi.tenant_id == ids["kim"]      # the request owner's tenant, never the admin's choice


def test_manual_task_bad_related_record_rejected(ctx):
    client, engine, ids = ctx
    _login(client, "admin@t.local")
    client.post("/admin/work-queue/create", data={"title": "x", "type": "follow_up_seller",
                "related_request_id": "9999"}, follow_redirects=False)
    with Session(engine) as s:
        assert s.exec(select(WorkItem)).first() is None    # no task created for a non-existent request


def test_requester_visible_action_has_no_internal_leak(ctx):
    """Publishing a question makes a sanitized SellerUpdate (visible) + a linked INTERNAL task; the internal
    task's description/title must never reach the seller surface."""
    client, engine, ids = ctx
    kc = TestClient(main.app)
    _login(kc, "kim@t.local")
    _submit(kc)
    sr = _one_request(engine)
    _login(client, "admin@t.local")
    client.post(f"/admin/requests/{sr.id}/publish", data={"public_status": "Sourcing",
                "summary": "Contacting distributors", "seller_question": "Accept EXW terms"},
                follow_redirects=False)
    with Session(engine) as s:
        su = s.exec(select(SellerUpdate)).first()
        wi = s.exec(select(WorkItem).where(WorkItem.type == "requester_action_required")).first()
        assert su is not None and wi is not None
        assert wi.visibility == "requester_visible" and wi.related_seller_update_id == su.id
        internal_desc = wi.description
    html = kc.get("/requests").text
    assert internal_desc not in html                 # internal description never leaks
    assert "Requester action required" not in html   # internal task title never leaks


# ------------------------------------------------------------- request workflow + history
def test_submit_sets_workflow_direction_and_one_review_task(ctx):
    client, engine, ids = ctx
    _login(client, "kim@t.local")
    _submit(client, rtype="buyer_hunt")
    sr = _one_request(engine)
    assert sr.workflow_status == "submitted" and sr.direction == "sell"
    with Session(engine) as s:
        tasks = s.exec(select(WorkItem).where(WorkItem.type == "review_new_request")).all()
        assert len(tasks) == 1 and tasks[0].related_request_id == sr.id


def test_find_supplier_is_buy_side(ctx):
    client, engine, ids = ctx
    _login(client, "kim@t.local")
    _submit(client, rtype="find_supplier")
    assert _one_request(engine).direction == "buy"


def test_advance_workflow_records_history_and_actor(ctx):
    client, engine, ids = ctx
    _login(client, "kim@t.local")
    _submit(client)
    sr = _one_request(engine)
    _login(client, "admin@t.local")  # same client re-login as admin
    client.post(f"/admin/requests/{sr.id}/workflow", data={"to": "under_review", "reason": "checking"},
                follow_redirects=False)
    with Session(engine) as s:
        evts = s.exec(select(RequestStatusEvent).where(RequestStatusEvent.request_id == sr.id)).all()
        assert any(e.to_status == "under_review" and e.reason == "checking" and e.actor_id == ids["admin"]
                   for e in evts)
        assert s.get(ServiceRequest, sr.id).workflow_status == "under_review"


def test_reject_stays_historical(ctx):
    client, engine, ids = ctx
    _login(client, "kim@t.local")
    _submit(client)
    sr = _one_request(engine)
    _login(client, "admin@t.local")
    client.post(f"/admin/requests/{sr.id}/reject", data={"reason": "not a fit"}, follow_redirects=False)
    with Session(engine) as s:
        r = s.get(ServiceRequest, sr.id)
        assert r.status == "rejected" and r.workflow_status == "rejected"


def test_request_assignment(ctx):
    client, engine, ids = ctx
    _login(client, "kim@t.local")
    _submit(client)
    sr = _one_request(engine)
    _login(client, "admin@t.local")
    client.post(f"/admin/requests/{sr.id}/assign", data={"assignee": str(ids["admin"]), "priority": "high"},
                follow_redirects=False)
    with Session(engine) as s:
        r = s.get(ServiceRequest, sr.id)
        assert r.assigned_admin_id == ids["admin"] and r.priority == "high"


def test_ready_for_delivery_creates_then_closes_deliver_task(ctx):
    client, engine, ids = ctx
    _login(client, "kim@t.local")
    _submit(client)
    sr = _one_request(engine)
    _login(client, "admin@t.local")
    client.post(f"/admin/requests/{sr.id}/workflow", data={"to": "ready_for_delivery"}, follow_redirects=False)
    with Session(engine) as s:
        dr = s.exec(select(WorkItem).where(WorkItem.type == "deliver_result")).all()
        assert len(dr) == 1 and dr[0].status in WQ.NONTERMINAL
    client.post(f"/admin/requests/{sr.id}/workflow", data={"to": "delivered"}, follow_redirects=False)
    with Session(engine) as s:
        dr = s.exec(select(WorkItem).where(WorkItem.type == "deliver_result")).first()
        assert dr.status == "completed"


# ------------------------------------------------------------- Work Queue CRUD
def _make_task(client, engine, **extra):
    _login(client, "admin@t.local")
    data = {"title": "Task", "type": "other"}
    data.update(extra)
    client.post("/admin/work-queue/create", data=data, follow_redirects=False)
    with Session(engine) as s:
        return s.exec(select(WorkItem).order_by(WorkItem.id.desc())).first().id


def test_assign_and_reassign(ctx):
    client, engine, ids = ctx
    wid = _make_task(client, engine)
    client.post(f"/admin/work-queue/{wid}/action", data={"action": "assign_me"}, follow_redirects=False)
    with Session(engine) as s:
        assert s.get(WorkItem, wid).assigned_admin_id == ids["admin"]
    client.post(f"/admin/work-queue/{wid}/action", data={"action": "reassign", "assignee": str(ids["bo"])},
                follow_redirects=False)
    with Session(engine) as s:
        assert s.get(WorkItem, wid).assigned_admin_id == ids["bo"]


def test_waiting_records_party(ctx):
    client, engine, ids = ctx
    wid = _make_task(client, engine)
    client.post(f"/admin/work-queue/{wid}/action", data={"action": "waiting", "party": "supplier"},
                follow_redirects=False)
    with Session(engine) as s:
        wi = s.get(WorkItem, wid)
        assert wi.status == "waiting" and wi.waiting_on == "supplier"


def test_complete_records_actor_and_timestamp(ctx):
    client, engine, ids = ctx
    wid = _make_task(client, engine)
    client.post(f"/admin/work-queue/{wid}/action", data={"action": "complete"}, follow_redirects=False)
    with Session(engine) as s:
        wi = s.get(WorkItem, wid)
        assert wi.status == "completed" and wi.resolved_by == ids["admin"] and wi.completed_at is not None


def test_dismiss_requires_reason(ctx):
    client, engine, ids = ctx
    wid = _make_task(client, engine)
    client.post(f"/admin/work-queue/{wid}/action", data={"action": "dismiss", "reason": ""},
                follow_redirects=False)
    with Session(engine) as s:
        assert s.get(WorkItem, wid).status == "open"           # no reason => not dismissed
    client.post(f"/admin/work-queue/{wid}/action", data={"action": "dismiss", "reason": "duplicate"},
                follow_redirects=False)
    with Session(engine) as s:
        wi = s.get(WorkItem, wid)
        assert wi.status == "dismissed" and wi.dismissed_reason == "duplicate"


def test_completed_items_are_retained(ctx):
    client, engine, ids = ctx
    wid = _make_task(client, engine)
    client.post(f"/admin/work-queue/{wid}/action", data={"action": "complete"}, follow_redirects=False)
    with Session(engine) as s:
        assert s.get(WorkItem, wid) is not None                # retained for history


def test_filters_and_views(ctx):
    client, engine, ids = ctx
    kc = TestClient(main.app)
    _login(kc, "kim@t.local")
    _submit(kc)
    sr = _one_request(engine)
    _login(client, "admin@t.local")
    client.post("/admin/work-queue/create", data={"title": "FollowSellerTask", "type": "follow_up_seller",
                "priority": "high", "related_request_id": str(sr.id)}, follow_redirects=False)
    client.post("/admin/work-queue/create", data={"title": "GenericTask", "type": "other", "priority": "low"},
                follow_redirects=False)
    # the type filter returns only matching records
    html = client.get("/admin/work-queue?type=follow_up_seller").text
    assert "FollowSellerTask" in html and "GenericTask" not in html
    # saved views resolve
    assert client.get("/admin/work-queue?view=unassigned").status_code == 200
    assert "FollowSellerTask" in client.get("/admin/work-queue?priority=high&view=all_open").text


def test_bulk_complete_admin_only(ctx):
    client, engine, ids = ctx
    wid = _make_task(client, engine)
    seller = TestClient(main.app)
    _login(seller, "kim@t.local")
    assert seller.post("/admin/work-queue/bulk", data={"action": "complete", "ids": [wid]},
                       follow_redirects=False).status_code == 403
    client.post("/admin/work-queue/bulk", data={"action": "complete", "ids": [wid]}, follow_redirects=False)
    with Session(engine) as s:
        assert s.get(WorkItem, wid).status == "completed"


# ------------------------------------------------------------- automation + idempotency
def test_resync_creates_no_duplicate(ctx):
    client, engine, ids = ctx
    _login(client, "kim@t.local")
    _submit(client)
    with Session(engine) as s:
        s1 = WQ.run_all_sync(s, None); s.commit()
        s2 = WQ.run_all_sync(s, None); s.commit()
        assert s2["total"] == 0
        assert len(s.exec(select(WorkItem).where(WorkItem.type == "review_new_request")).all()) == 1


def test_response_resolves_visible_and_creates_admin_task(ctx):
    client, engine, ids = ctx
    kc = TestClient(main.app)
    _login(kc, "kim@t.local")
    _submit(kc)
    sr = _one_request(engine)
    _login(client, "admin@t.local")
    client.post(f"/admin/requests/{sr.id}/publish", data={"public_status": "Sourcing", "summary": "x",
                "seller_question": "Accept EXW"}, follow_redirects=False)
    with Session(engine) as s:
        su = s.exec(select(SellerUpdate)).first()
    kc.post(f"/requests/{sr.id}/updates/{su.id}/resolve", data={"answer": "yes"}, follow_redirects=False)
    with Session(engine) as s:
        ra = s.exec(select(WorkItem).where(WorkItem.type == "requester_action_required")).first()
        rr = s.exec(select(WorkItem).where(WorkItem.type == "review_reply")).all()
        assert ra.status == "completed" and len(rr) == 1


def test_bounce_creates_replacement_via_sync_without_touching_bounce(ctx):
    client, engine, ids = ctx
    with Session(engine) as s:
        ld = Lead(product="x", owner_id=ids["kim"], next_action_note="bounced")
        s.add(ld); s.commit(); s.refresh(ld)
        lid = ld.id
        WQ.run_all_sync(s, None); s.commit()
        wi = s.exec(select(WorkItem).where(WorkItem.type == "replace_invalid_contact")).first()
        assert wi is not None and wi.related_lead_id == lid
        assert s.get(Lead, lid).next_action_note == "bounced"    # bounce state itself unchanged


def test_duplicate_candidate_creates_review_via_sync(ctx):
    client, engine, ids = ctx
    with Session(engine) as s:
        dc = DuplicateCandidate(tenant_id=ids["kim"], left_id=1, right_id=2, match_type="strong", status="open")
        s.add(dc); s.commit()
        WQ.run_all_sync(s, None); s.commit()
        assert s.exec(select(WorkItem).where(WorkItem.type == "review_potential_duplicate")).first() is not None


def test_work_item_failure_never_breaks_submission(ctx, monkeypatch):
    """If work-item creation blows up, the request must still be created and a workitem_link_failed audited."""
    client, engine, ids = ctx

    def boom(*a, **k):
        raise RuntimeError("work item creation exploded")
    monkeypatch.setattr(WQ, "create_work_item", boom)
    _login(client, "kim@t.local")
    _submit(client)
    with Session(engine) as s:
        assert s.exec(select(ServiceRequest)).first() is not None   # request survived
        assert s.exec(select(WorkItem)).first() is None             # no task created
        assert s.exec(select(AuditLog).where(AuditLog.action == "workitem_link_failed")).first() is not None


def test_run_all_sync_inferred_flag(ctx):
    """A backfill-style resync (inferred=True) marks its created items inferred, for a clean rollback."""
    client, engine, ids = ctx
    with Session(engine) as s:
        s.add(DuplicateCandidate(tenant_id=ids["kim"], left_id=1, right_id=2, match_type="strong",
                                 status="open"))
        s.commit()
        WQ.run_all_sync(s, None, inferred=True); s.commit()
        wi = s.exec(select(WorkItem).where(WorkItem.type == "review_potential_duplicate")).first()
        assert wi is not None and wi.inferred is True


# ------------------------------------------------------------- no GET mutation
def test_work_queue_get_does_not_mutate(ctx):
    client, engine, ids = ctx
    _login(client, "kim@t.local")
    _submit(client)
    _login(client, "admin@t.local")
    with Session(engine) as s:
        before = len(s.exec(select(WorkItem)).all())
    for _ in range(3):
        client.get("/admin/work-queue")
        client.get("/admin/work-queue/count")
    with Session(engine) as s:
        assert len(s.exec(select(WorkItem)).all()) == before


# ============================================================================
# Durable disposition: a completed/dismissed task must NOT be recreated by the
# next sync while its underlying condition is unchanged (condition_version).
# ============================================================================
def _draft_quote(s, ids):
    prod = Product(name="W", unit="pcs", exw_price=1.0, weight_kg_per_unit=0.1)
    s.add(prod); s.commit(); s.refresh(prod)
    q = Quote(lead_id=1, owner_id=ids["kim"], product_id=prod.id, status="draft")
    s.add(q); s.commit(); s.refresh(q)
    return q


def test_dispose_complete_draft_quote_not_recreated(ctx):
    client, engine, ids = ctx
    with Session(engine) as s:
        _draft_quote(s, ids)
        WQ.run_all_sync(s, None); s.commit()
        wi = s.exec(select(WorkItem).where(WorkItem.type == "approve_quote")).one()
        WQ.complete_item(s, wi, None); s.commit()          # admin completes it
        WQ.run_all_sync(s, None); s.commit()               # re-sync while quote STILL draft
        items = s.exec(select(WorkItem).where(WorkItem.type == "approve_quote")).all()
        assert len(items) == 1 and items[0].status == "completed"    # NOT recreated


def test_dispose_dismiss_duplicate_candidate_not_recreated(ctx):
    client, engine, ids = ctx
    with Session(engine) as s:
        s.add(DuplicateCandidate(tenant_id=ids["kim"], left_id=1, right_id=2, match_type="strong",
                                 status="open"))
        s.commit()
        WQ.run_all_sync(s, None); s.commit()
        wi = s.exec(select(WorkItem).where(WorkItem.type == "review_potential_duplicate")).one()
        WQ.dismiss_item(s, wi, "not a dup", None); s.commit()   # admin dismisses; candidate stays open
        WQ.run_all_sync(s, None); s.commit()
        items = s.exec(select(WorkItem).where(WorkItem.type == "review_potential_duplicate")).all()
        assert len(items) == 1 and items[0].status == "dismissed"   # NOT recreated


def test_dispose_complete_failed_job_not_recreated(ctx):
    client, engine, ids = ctx
    with Session(engine) as s:
        s.add(Outreach(lead_id=1, status="failed", error="smtp 550")); s.commit()
        WQ.run_all_sync(s, None); s.commit()
        wi = s.exec(select(WorkItem).where(WorkItem.type == "failed_system_job")).one()
        WQ.dismiss_item(s, wi, "acknowledged", None); s.commit()
        WQ.run_all_sync(s, None); s.commit()
        items = s.exec(select(WorkItem).where(WorkItem.type == "failed_system_job")).all()
        assert len(items) == 1 and items[0].status == "dismissed"   # NOT recreated


def test_dispose_complete_invalid_contact_not_recreated_until_condition_changes(ctx):
    client, engine, ids = ctx
    with Session(engine) as s:
        ld = Lead(product="x", owner_id=ids["kim"], next_action_note="bounced", email="a@b.com")
        s.add(ld); s.commit(); s.refresh(ld); lid = ld.id
        WQ.run_all_sync(s, None); s.commit()
        wi = s.exec(select(WorkItem).where(WorkItem.type == "replace_invalid_contact")).one()
        WQ.complete_item(s, wi, None); s.commit()          # admin handled this bounce
        WQ.run_all_sync(s, None); s.commit()               # same address still bounced -> NOT recreated
        assert len(s.exec(select(WorkItem).where(WorkItem.type == "replace_invalid_contact")).all()) == 1
        # condition CHANGES: a new (replacement) address later bounces -> a fresh task IS created
        ld = s.get(Lead, lid); ld.email = "c@d.com"; s.add(ld); s.commit()
        WQ.run_all_sync(s, None); s.commit()
        items = s.exec(select(WorkItem).where(WorkItem.type == "replace_invalid_contact")).all()
        assert len(items) == 2 and sum(1 for i in items if i.status in WQ.NONTERMINAL) == 1


# ============================================================================
# Workflow_status vs legacy status can never produce conflicting admin and
# seller-visible states, across all seven request states.
# ============================================================================
def _fresh_request(engine, ids):
    with Session(engine) as s:
        sr = ServiceRequest(request_type="buyer_hunt", product="Zinc", status="submitted",
                            direction="sell", workflow_status="submitted", requester_id=ids["kim"],
                            owner_id=ids["kim"], tracking_code="SR-X")
        s.add(sr); s.commit(); s.refresh(sr)
        return sr.id


def test_states_never_conflict_across_admin_and_seller(ctx):
    client, engine, ids = ctx
    admin = TestClient(main.app); _login(admin, "admin@t.local")
    seller = TestClient(main.app); _login(seller, "kim@t.local")

    def drive(rid, actions):
        for path, data in actions:
            admin.post(f"/admin/requests/{rid}/{path}", data=data, follow_redirects=False)

    # (label, actions to reach it, expected legacy status, expected workflow_status)
    cases = [
        ("Submitted", [], "submitted", "submitted"),
        ("Approved", [("approve", {})], "approved", "approved"),
        ("In Progress", [("approve", {}), ("start", {})], "running", "in_progress"),
        ("Delivered", [("approve", {}), ("start", {}), ("done", {"result": "here"})], "done", "delivered"),
        ("Completed", [("approve", {}), ("workflow", {"to": "completed"})], "done", "completed"),
        ("Rejected", [("reject", {"reason": "no"})], "rejected", "rejected"),
        ("Cancelled", [("approve", {}), ("workflow", {"to": "cancelled"})], "rejected", "cancelled"),
    ]
    for label, actions, exp_legacy, exp_wf in cases:
        rid = _fresh_request(engine, ids)
        drive(rid, actions)
        with Session(engine) as s:
            sr = s.get(ServiceRequest, rid)
            assert sr.status == exp_legacy, f"{label}: legacy {sr.status} != {exp_legacy}"
            assert RS.effective_workflow(sr) == exp_wf, f"{label}: wf {sr.workflow_status} != {exp_wf}"
            assert RS.states_consistent(sr), f"{label}: admin/seller states conflict"
        # all three surfaces render, and the seller sees the (consistent) legacy status, not a contradiction
        assert admin.get("/admin/requests").status_code == 200
        assert admin.get(f"/admin/requests/{rid}").status_code == 200
        spage = seller.get("/requests")
        assert spage.status_code == 200
        # the seller never sees an admin-only finer workflow label for this request
        for adminonly in ("Under review", "Ready for delivery", "Waiting for external"):
            assert adminonly not in spage.text


def test_cancelled_never_leaves_seller_on_a_live_state(ctx):
    client, engine, ids = ctx
    admin = TestClient(main.app); _login(admin, "admin@t.local")
    rid = _fresh_request(engine, ids)
    admin.post(f"/admin/requests/{rid}/approve", data={}, follow_redirects=False)
    admin.post(f"/admin/requests/{rid}/workflow", data={"to": "cancelled"}, follow_redirects=False)
    with Session(engine) as s:
        sr = s.get(ServiceRequest, rid)
        # workflow is 'cancelled' (admin) but the seller-visible legacy is a CLOSED state, never 'approved'
        assert sr.workflow_status == "cancelled"
        assert sr.status == "rejected" and sr.status not in ("approved", "running", "submitted")


# ============================================================================
# Work Queue synchronization is isolated from the main worker.
# ============================================================================
def test_worker_sync_isolates_exceptions(ctx, monkeypatch):
    """A sync exception is caught and returned — it can never propagate to stop other worker cycles."""
    client, engine, ids = ctx
    import app.worker as worker
    monkeypatch.setattr(worker, "engine", engine)
    monkeypatch.setattr(WQ, "run_all_sync", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    out = worker.run_work_item_sync()          # must NOT raise
    assert out.get("total") == 0 and "error" in out


def test_worker_other_cycles_continue_after_sync_failure(ctx, monkeypatch):
    """After a sync failure the worker keeps working: a later healthy sync succeeds (cycle recovered)."""
    client, engine, ids = ctx
    import app.worker as worker
    monkeypatch.setattr(worker, "engine", engine)
    real = WQ.run_all_sync
    monkeypatch.setattr(WQ, "run_all_sync", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    assert "error" in worker.run_work_item_sync()          # failure isolated
    monkeypatch.setattr(WQ, "run_all_sync", real)          # next cycle runs normally
    with Session(engine) as s:                             # a condition with no live hook, so sync must create
        s.add(DuplicateCandidate(tenant_id=ids["kim"], left_id=1, right_id=2, match_type="strong",
                                 status="open"))
        s.commit()
    out = worker.run_work_item_sync()
    assert "error" not in out and out["total"] >= 1        # recovered + created the task


def test_slow_sync_has_a_limit(ctx):
    """Creation is capped per run; the remainder is picked up on the next run (bounds a slow sync)."""
    client, engine, ids = ctx
    with Session(engine) as s:
        for i in range(5):
            s.add(DuplicateCandidate(tenant_id=ids["kim"], left_id=i * 2 + 1, right_id=i * 2 + 2,
                                     match_type="potential", status="open"))
        s.commit()
        r1 = WQ.run_all_sync(s, None, limit=3); s.commit()
        assert r1["total"] == 3 and r1["capped"] is True
        r2 = WQ.run_all_sync(s, None, limit=3); s.commit()
        assert r2["total"] == 2 and r2["capped"] is False
        assert len(s.exec(select(WorkItem).where(WorkItem.type == "review_potential_duplicate")).all()) == 5


def test_concurrent_runs_stay_idempotent(ctx):
    """Two runs on unchanged state create no duplicate; the partial-unique OPEN index is the race backstop."""
    from sqlalchemy.exc import IntegrityError
    client, engine, ids = ctx
    _login(client, "kim@t.local"); _submit(client)
    with Session(engine) as s:
        WQ.run_all_sync(s, None); s.commit()
        n1 = len(s.exec(select(WorkItem)).all())
        WQ.run_all_sync(s, None); s.commit()
        assert len(s.exec(select(WorkItem)).all()) == n1        # second run added nothing
        # the DB-level guard rejects a duplicate OPEN task with the same key (concurrency safety)
        WQ.create_work_item(s, type="other", title="a", idempotency_key="race:test"); s.commit()
        raised = False
        try:
            WQ.create_work_item(s, type="other", title="b", idempotency_key="race:test"); s.commit()
        except IntegrityError:
            raised = True; s.rollback()
        assert raised
