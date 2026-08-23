"""Confidential managed buyer-outreach pipeline — the security gate.

Sellers must NEVER receive buyer identity/contacts through any surface (URL, list, search, dashboard,
quote, file, or a crafted request), and the admin must see full PII. Plus: funnel is branch-correct
(Won/Lost/Disqualified are separate; a lost buyer never counts as Won), anon-refs are DB-unique and don't
expose the id, the two status systems stay in sync, seller updates are PII-sanitized, actions are audited,
and the migration reassigns delivered buyers to the admin pool while keeping links intact.
"""
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine, select

import app.main as main
from app import pipeline
from app.auth import hash_password
from app.models import (AuditLog, Deal, Lead, RequestDeliverable, SellerUpdate, ServiceRequest,
                        StageEvent, User)

# distinctive values that are never legitimately echoed back to a seller
STRONG_PII = ["buyer@acme.com", "+9715551234", "Zoltan", "acme.com"]
LEAKY = ["mailto:", "tel:", "wa.me"]


def _seed(engine):
    ids = {}
    with Session(engine) as s:
        for email, role in [("admin@t.local", "admin"), ("kim@t.local", "agent"), ("c@t.local", "agent")]:
            s.add(User(email=email, name=email.split("@")[0], role=role, active=True,
                       password_hash=hash_password("pw")))
        s.commit()
        uid = {u.email: u.id for u in s.exec(select(User)).all()}
        ids.update(kim=uid["kim@t.local"], c=uid["c@t.local"], admin=uid["admin@t.local"])
        sr = ServiceRequest(request_type="buyer_hunt", product="black tea", status="done",
                            requester_id=ids["kim"], owner_id=ids["kim"], tracking_code="SR-1",
                            result_source_tag="req-1")
        s.add(sr); s.commit(); s.refresh(sr); ids["req"] = sr.id
        lead = Lead(product="black tea", buyer_company="Acorp", contact_name="Zoltan", email="buyer@acme.com",
                    phone="+9715551234", website="acme.com", source=f"req-{sr.id}", dest_country="Iraq",
                    owner_id=None, managed=True, seller_id=ids["kim"], request_id=sr.id,
                    anon_ref="Buyer-IQ-001", pipeline_stage="contacted", tracking_code="G4-A")
        s.add(lead); s.commit(); s.refresh(lead); ids["lead"] = lead.id
        for frm, to in [("", "identified"), ("identified", "contacted")]:
            s.add(StageEvent(lead_id=lead.id, request_id=sr.id, from_stage=frm, to_stage=to))
        s.commit()
    return ids


@pytest.fixture
def ctx(monkeypatch):
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    SQLModel.metadata.create_all(engine)
    with engine.connect() as conn:      # mirror init_db's partial-unique index
        conn.execute(text("CREATE UNIQUE INDEX IF NOT EXISTS uq_lead_req_anonref "
                          "ON lead(request_id, anon_ref) WHERE anon_ref != ''"))
        conn.commit()
    monkeypatch.setattr(main, "engine", engine)
    ids = _seed(engine)
    return TestClient(main.app), engine, ids


def _login(client, email):
    assert client.post("/login", data={"email": email, "password": "pw"},
                       follow_redirects=False).status_code == 303


# ------------------------------------------------------------------ privacy (the core guarantee)
def test_seller_never_sees_buyer_pii_anywhere(ctx):
    client, _, ids = ctx
    _login(client, "kim@t.local")
    # strong PII (email/phone/contact/website) must be absent EVERYWHERE, incl. a crafted search
    for path in ["/leads", "/leads?q=Acorp", f"/leads?source=req-{ids['req']}", "/requests", "/"]:
        body = client.get(path).text
        for leak in STRONG_PII + LEAKY:
            assert leak not in body, f"LEAK {leak!r} at {path}"
    # the company name must not appear where it isn't the seller's own echoed search term
    for path in ["/leads", f"/leads?source=req-{ids['req']}", "/requests", "/"]:
        assert "Acorp" not in client.get(path).text
    # the managed lead's detail URL is not owned by the seller -> 404 (can't even confirm it exists)
    assert client.get(f"/leads/{ids['lead']}", follow_redirects=False).status_code == 404
    # but the seller DOES see the anonymized reference + stage on their requests page
    reqs = client.get("/requests").text
    assert "Buyer-IQ-001" in reqs and "Contacted" in reqs
    # campaign board (renders buyer emails) is admin-only
    assert client.get("/campaign", follow_redirects=False).status_code == 403


def test_seller_cannot_use_admin_or_email_routes(ctx):
    client, _, ids = ctx
    _login(client, "kim@t.local")
    r, l = ids["req"], ids["lead"]
    # crafted POSTs against the admin pipeline / publish / email tool
    assert client.post(f"/admin/requests/{r}/pipeline/{l}/stage", data={"to_stage": "won"},
                       follow_redirects=False).status_code == 403
    assert client.post(f"/admin/requests/{r}/publish", data={"summary": "hi"},
                       follow_redirects=False).status_code == 403
    assert client.get(f"/admin/requests/{r}/pipeline", follow_redirects=False).status_code == 403
    assert client.post("/leads/bulk", data={"action": "email", "ids": [l]},
                       follow_redirects=False).status_code == 403
    assert client.get("/mail", follow_redirects=False).status_code == 403


def test_cross_tenant_seller_sees_nothing(ctx):
    client, _, ids = ctx
    _login(client, "c@t.local")          # a different seller
    assert "Buyer-IQ-001" not in client.get("/requests").text
    for leak in STRONG_PII:
        assert leak not in client.get("/requests").text
    assert client.get(f"/admin/requests/{ids['req']}/pipeline", follow_redirects=False).status_code == 403


def test_admin_sees_full_pii(ctx):
    client, _, ids = ctx
    _login(client, "admin@t.local")
    body = client.get(f"/admin/requests/{ids['req']}/pipeline").text
    assert "Acorp" in body and "buyer@acme.com" in body and "+9715551234" in body


# ------------------------------------------------------------------ funnel — branch-correct
def test_funnel_won_lost_disqualified_and_nonlinear(ctx):
    _, engine, ids = ctx
    with Session(engine) as s:
        sr = s.get(ServiceRequest, ids["req"])
        admin = s.get(User, ids["admin"])

        def buyer(ref):
            b = Lead(product="tea", source=f"req-{sr.id}", managed=True, seller_id=ids["kim"],
                     request_id=sr.id, owner_id=None, anon_ref=ref, pipeline_stage="identified",
                     tracking_code=ref, dest_country="Iraq")
            s.add(b); s.commit(); s.refresh(b); return b

        won = buyer("Buyer-IQ-010")
        for st in ["contacted", "responded", "interested", "negotiating", "won"]:
            pipeline.set_pipeline_stage(s, won, st, admin)
        lost = buyer("Buyer-IQ-011")
        for st in ["contacted", "responded", "lost"]:
            pipeline.set_pipeline_stage(s, lost, st, admin)
        disq = buyer("Buyer-IQ-012")
        pipeline.set_pipeline_stage(s, disq, "disqualified", admin)
        nonlin = buyer("Buyer-IQ-013")            # interested -> lost -> reopen negotiating
        for st in ["contacted", "interested", "lost", "negotiating"]:
            pipeline.set_pipeline_stage(s, nonlin, st, admin)
        s.commit()

        f = pipeline.request_funnel(s, sr)
        # exactly ONE real Won; Lost/Disqualified never counted as Won
        assert f["reached_won"] == 1
        assert f["reached_lost"] == 2 and f["reached_disqualified"] == 1     # lost + nonlin(lost); disq
        # the reopened buyer visited interested + lost + negotiating in history, never won
        assert f["reached_interested"] == 2                                 # won-path + nonlin
        assert f["reached_negotiating"] == 2                               # won-path + nonlin(reopened)
        # current-stage counts differ from cumulative reached
        assert f["at_won"] == 1 and f["at_disqualified"] == 1 and f["at_negotiating"] == 1
        assert f["at_lost"] == 1                                            # only 'lost' buyer sits at lost now


# ------------------------------------------------------------------ anon-ref uniqueness
def test_anon_ref_unique_and_not_db_id(ctx):
    _, engine, ids = ctx
    with Session(engine) as s:
        sr = s.get(ServiceRequest, ids["req"])
        a = Lead(product="t", source="req-1", managed=True, request_id=sr.id, seller_id=ids["kim"],
                 owner_id=None, dest_country="Iraq", tracking_code="x1")
        s.add(a); s.commit(); s.refresh(a)
        r1 = pipeline.assign_anon_ref(s, a); s.commit()
        b = Lead(product="t", source="req-1", managed=True, request_id=sr.id, seller_id=ids["kim"],
                 owner_id=None, dest_country="Iraq", tracking_code="x2")
        s.add(b); s.commit(); s.refresh(b)
        r2 = pipeline.assign_anon_ref(s, b); s.commit()
        # per-request sequence, continuing after the fixture's Buyer-IQ-001 (NOT the DB id / tracking_code)
        assert r1 == "Buyer-IQ-002" and r2 == "Buyer-IQ-003" and r1 != r2
        # DB rejects a duplicate ref in the same request
        with pytest.raises(IntegrityError):
            s.add(Lead(product="t", source="req-1", managed=True, request_id=sr.id, owner_id=None,
                       anon_ref=r1, tracking_code="x3"))
            s.commit()
        s.rollback()


# ------------------------------------------------------------------ status sync + won->deal
def test_pipeline_status_stay_in_sync(ctx):
    client, engine, ids = ctx
    with Session(engine) as s:
        admin = s.get(User, ids["admin"]); lead = s.get(Lead, ids["lead"])
        ok, _ = pipeline.set_pipeline_stage(s, lead, "quote_sent", admin); s.commit()
        assert ok and lead.pipeline_stage == "quote_sent" and lead.status == "quoted"
        pipeline.set_pipeline_stage(s, lead, "negotiating", admin); s.commit()
        assert lead.status == "negotiating"
        pipeline.set_pipeline_stage(s, lead, "lost", admin, note="price"); s.commit()
        assert lead.status == "lost" and lead.lost_reason == "price"
    # 'won' via the admin route creates a Deal and keeps both fields consistent
    _login(client, "admin@t.local")
    with Session(engine) as s:                       # reopen the lost buyer so it can go to won
        lead = s.get(Lead, ids["lead"]); pipeline.set_pipeline_stage(s, lead, "negotiating", s.get(User, ids["admin"])); s.commit()
    client.post(f"/admin/requests/{ids['req']}/pipeline/{ids['lead']}/stage",
                data={"to_stage": "won"}, follow_redirects=False)
    with Session(engine) as s:
        lead = s.get(Lead, ids["lead"])
        assert lead.pipeline_stage == "won" and lead.status == "won"
        assert s.exec(select(Deal).where(Deal.lead_id == lead.id)).first() is not None


# ------------------------------------------------------------------ seller-update sanitization
def test_publish_blocks_pii_and_clean_reaches_seller(ctx):
    client, engine, ids = ctx
    _login(client, "admin@t.local")
    # PII in the update -> blocked, nothing published
    client.post(f"/admin/requests/{ids['req']}/publish",
                data={"summary": "reach the buyer at buyer@acme.com or +9715551234", "public_status": "x"},
                follow_redirects=False)
    with Session(engine) as s:
        assert s.exec(select(SellerUpdate)).first() is None
    # clean update -> published, and the seller sees it (no leak of private data)
    client.post(f"/admin/requests/{ids['req']}/publish",
                data={"anon_ref": "Buyer-IQ-001", "public_status": "Negotiating",
                      "summary": "Buyer requested pricing for 5 MT under CIF."},
                follow_redirects=False)
    with Session(engine) as s:
        su = s.exec(select(SellerUpdate)).first()
        assert su is not None and su.seller_id == ids["kim"]
    _login(client, "kim@t.local")
    body = client.get("/requests").text
    assert "requested pricing for 5 MT" in body
    for leak in STRONG_PII:
        assert leak not in body


# ------------------------------------------------------------------ audit
def test_admin_actions_are_audited(ctx):
    client, engine, ids = ctx
    _login(client, "admin@t.local")
    client.get(f"/admin/requests/{ids['req']}/pipeline")
    client.post(f"/admin/requests/{ids['req']}/pipeline/{ids['lead']}/stage",
                data={"to_stage": "responded"}, follow_redirects=False)
    client.post(f"/admin/requests/{ids['req']}/publish",
                data={"summary": "Waiting for confirmation."}, follow_redirects=False)
    with Session(engine) as s:
        actions = {a.action for a in s.exec(select(AuditLog)).all()}
    assert {"pii_view", "stage_change", "publish_update"} <= actions


# ------------------------------------------------------------------ seller-safe file gating
def test_seller_only_downloads_seller_safe_files(ctx, monkeypatch, tmp_path):
    client, engine, ids = ctx
    monkeypatch.setattr(main, "REQUEST_FILES_DIR", tmp_path)
    d = tmp_path / str(ids["req"]); d.mkdir(parents=True)
    (d / "buyers.pdf").write_text("buyer list with PII")
    rel = f"{ids['req']}/buyers.pdf"
    with Session(engine) as s:
        sr = s.get(ServiceRequest, ids["req"]); sr.result_file_path = rel; s.add(sr)
        s.add(RequestDeliverable(request_id=ids["req"], file_path=rel, seller_safe=False))
        s.commit()
        dv_unsafe = s.exec(select(RequestDeliverable)).first().id
    _login(client, "kim@t.local")
    assert client.get(f"/requests/{ids['req']}/result/file", follow_redirects=False).status_code == 404
    assert client.get(f"/requests/{ids['req']}/deliverable/{dv_unsafe}", follow_redirects=False).status_code == 404
    # mark it seller-safe -> seller can now download
    with Session(engine) as s:
        dv = s.get(RequestDeliverable, dv_unsafe); dv.seller_safe = True; s.add(dv); s.commit()
    assert client.get(f"/requests/{ids['req']}/deliverable/{dv_unsafe}", follow_redirects=False).status_code == 200


# ------------------------------------------------------------------ migration behavior
def test_migration_reassigns_and_anonymizes(ctx):
    client, engine, ids = ctx
    with Session(engine) as s:      # a seller-owned delivered buyer = the pre-migration state
        sr2 = ServiceRequest(request_type="buyer_hunt", product="honey", status="done",
                             requester_id=ids["kim"], owner_id=ids["kim"], tracking_code="SR-2")
        s.add(sr2); s.commit(); s.refresh(sr2)
        sr2.result_source_tag = f"req-{sr2.id}"; s.add(sr2); s.commit()
        old = Lead(product="honey", buyer_company="Zed Foods", email="z@zed.com", phone="+9990001111",
                   source=f"req-{sr2.id}", owner_id=ids["kim"], managed=False, dest_country="Iraq",
                   status="new", tracking_code="G4-Z")
        s.add(old); s.commit(); s.refresh(old); lid, req2 = old.id, sr2.id
        # migrate inline — the same steps as scripts/backfill_confidential.py
        old.request_id = req2; old.seller_id = old.owner_id; old.managed = True
        old.pipeline_stage = "identified"; s.add(old)
        pipeline.assign_anon_ref(s, old)
        s.add(StageEvent(lead_id=old.id, request_id=req2, from_stage="", to_stage="identified"))
        old.owner_id = None; s.add(old); s.commit()
        m = s.get(Lead, lid)
        anon = m.anon_ref
        assert m.owner_id is None and m.managed and m.seller_id == ids["kim"] and anon.startswith("Buyer-IQ-")
    _login(client, "kim@t.local")
    leads_body = client.get("/leads").text
    assert "z@zed.com" not in leads_body and "Zed Foods" not in leads_body   # PII left the seller
    assert anon in client.get("/requests").text                             # anonymized ref remains
    _login(client, "admin@t.local")
    pbody = client.get(f"/admin/requests/{req2}/pipeline").text
    assert "z@zed.com" in pbody and "Zed Foods" in pbody                     # admin keeps full PII


# ------------------------------------------------------------------ resolvable seller questions (no stale "action required")
def test_seller_resolves_question_and_action_clears(ctx):
    client, engine, ids = ctx
    from app.models import RequestMessage
    with Session(engine) as s:                # a buyer flagged + a published question tied to it
        lead = s.get(Lead, ids["lead"]); lead.seller_action_required = True; s.add(lead)
        su = SellerUpdate(request_id=ids["req"], lead_id=ids["lead"], seller_id=ids["kim"],
                          anon_ref="Buyer-IQ-001", summary="Need your target price.",
                          seller_question="What's your floor price for 5 MT?", status="open", published=True)
        s.add(su); s.commit(); s.refresh(su); sid = su.id
        assert pipeline.request_funnel(s, s.get(ServiceRequest, ids["req"]))["outstanding_seller_actions"] == 1
    _login(client, "kim@t.local")
    assert client.post(f"/requests/{ids['req']}/updates/{sid}/resolve",
                       data={"answer": "Floor is 3.80/kg CIF."}, follow_redirects=False).status_code == 303
    with Session(engine) as s:
        su = s.get(SellerUpdate, sid)
        assert su.status == "resolved" and su.resolved_by == "kim@t.local" and su.resolved_at is not None
        assert s.get(Lead, ids["lead"]).seller_action_required is False        # flag can't linger
        assert pipeline.request_funnel(s, s.get(ServiceRequest, ids["req"]))["outstanding_seller_actions"] == 0
        msgs = s.exec(select(RequestMessage).where(RequestMessage.request_id == ids["req"])).all()
        assert any("Floor is 3.80" in m.body for m in msgs)                    # answer routed to the mediated chat
    assert "Answered" in client.get("/requests").text                         # seller now sees the answered state


def test_resolve_rejects_foreign_or_bogus_update(ctx):
    client, engine, ids = ctx
    with Session(engine) as s:
        su = SellerUpdate(request_id=ids["req"], seller_id=ids["kim"], seller_question="q?",
                          status="open", published=True)
        s.add(su); s.commit(); s.refresh(su); sid = su.id
    _login(client, "c@t.local")               # a different seller can't resolve kim's update
    assert client.post(f"/requests/{ids['req']}/updates/{sid}/resolve",
                       data={"answer": "x"}, follow_redirects=False).status_code == 404
    _login(client, "kim@t.local")             # a bogus id is never trusted
    assert client.post(f"/requests/{ids['req']}/updates/999999/resolve",
                       data={"answer": "x"}, follow_redirects=False).status_code == 404
    with Session(engine) as s:
        assert s.get(SellerUpdate, sid).status == "open"                       # nothing was resolved


# ------------------------------------------------------------------ admin chat messages are PII-scanned
def test_admin_chat_message_pii_blocked_seller_not_scanned(ctx):
    client, engine, ids = ctx
    from app.models import RequestMessage
    r = ids["req"]
    _login(client, "admin@t.local")
    # admin -> seller with a buyer email/phone is BLOCKED before saving
    client.post(f"/requests/{r}/messages",
                data={"body": "call the buyer at buyer@acme.com or +9715551234"}, follow_redirects=False)
    with Session(engine) as s:
        assert not s.exec(select(RequestMessage).where(RequestMessage.request_id == r)).all()
    # a clean admin message goes through
    client.post(f"/requests/{r}/messages", data={"body": "Good progress this week."}, follow_redirects=False)
    with Session(engine) as s:
        msgs = s.exec(select(RequestMessage).where(RequestMessage.request_id == r)).all()
        assert len(msgs) == 1 and "Good progress" in msgs[0].body
    # a SELLER message is NOT buyer-PII-scanned (they can share their own contact with the admin)
    _login(client, "kim@t.local")
    client.post(f"/requests/{r}/messages", data={"body": "reach me at kim@myco.com / +111222333"},
                follow_redirects=False)
    with Session(engine) as s:
        assert any("kim@myco.com" in m.body
                   for m in s.exec(select(RequestMessage).where(RequestMessage.request_id == r)).all())


# ------------------------------------------------------------------ audit is tenant-scoped
def test_audit_records_are_tenant_scoped(ctx):
    client, engine, ids = ctx
    _login(client, "admin@t.local")
    client.get(f"/admin/requests/{ids['req']}/pipeline")
    client.post(f"/admin/requests/{ids['req']}/pipeline/{ids['lead']}/stage",
                data={"to_stage": "responded"}, follow_redirects=False)
    with Session(engine) as s:
        rows = s.exec(select(AuditLog).where(AuditLog.action.in_(["pii_view", "stage_change"]))).all()
    assert rows and all(a.tenant_id == ids["kim"] for a in rows)               # scoped to the seller the action concerns


# ------------------------------------------------------------------ migrated history marked inferred, never invented
def test_migrated_history_is_marked_inferred_not_invented(ctx):
    _, engine, ids = ctx
    with Session(engine) as s:
        sr = s.get(ServiceRequest, ids["req"])
        mig = Lead(product="tea", source=f"req-{sr.id}", managed=True, seller_id=ids["kim"],
                   request_id=sr.id, owner_id=None, anon_ref="Buyer-IQ-050", pipeline_stage="quote_sent",
                   dest_country="Iraq", tracking_code="mig")
        s.add(mig); s.commit(); s.refresh(mig)
        # a migration seeds ONE inferred starting event, not the ladder the buyer never walked
        s.add(StageEvent(lead_id=mig.id, request_id=sr.id, from_stage="", to_stage="quote_sent",
                         note="migrated", inferred=True)); s.commit()
        f = pipeline.request_funnel(s, sr)
        assert f["migrated_prospects"] == 1 and f["history_complete"] is False
        # NOT invented: the migrated buyer never counts as having reached contacted/responded
        assert f["reached_contacted"] == 1                                    # only the fixture buyer really did
        assert f["reached_responded"] == 0
        assert f["reached_quote_sent"] >= 1                                   # its current stage still counts
