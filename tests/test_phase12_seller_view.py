"""Phase 12 — the seller (TRSHARKS) can log in safely and sees accurate, anonymized progress.

Privacy through the real routes: the status poll and the request card never show a non-seller-safe deliverable,
note or link; a seller-safe delivery that names a buyer is refused; published updates, admin chat and admin answers
are checked against the request's own buyers (names, emails, website hosts, cities, phone digits) and a widened
domain rule; anon_ref is validated server-side. Honest wording (outreach in progress, no 'deliver into your
account'), dashboard funnel numbers instead of 'My buyers 0', admin counts from the managed funnel, the workflow
history records the reconciled legacy status, the admin can change a login email safely, and a seller cannot
self-edit the identity fields the campaign render guard matches on."""
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine, select

import app.main as main
from app import access_service as ACCESS
from app import campaign_render as CR
from app import permissions as P
from app import pipeline
from app.auth import hash_password
from app.models import (AccessAuditLog, Lead, RequestDeliverable, RequestMessage, RequestStatusEvent,
                        SellerUpdate, ServiceRequest, StageEvent, User, UserProfile)

_IDX = (
    "CREATE UNIQUE INDEX IF NOT EXISTS uq_lead_req_anonref ON lead(request_id, anon_ref) WHERE anon_ref != ''",
    "CREATE UNIQUE INDEX IF NOT EXISTS uq_userprofile_user ON userprofile(user_id) WHERE user_id IS NOT NULL",
    "CREATE UNIQUE INDEX IF NOT EXISTS uq_workitem_idem_open ON workitem(idempotency_key) "
    "WHERE idempotency_key != '' AND status IN ('open','in_progress','waiting')",
)

# every buyer-identifying string of the fixture — none may ever reach a seller page
PII = ["Richelieu", "richelieu.example", "sales@richelieu.example", "Marc Tremblay", "514 555 0101", "5145550101",
       "Montreal", "directory.example", "secret pricing note", "Inoxa", "inoxa.pl", "biuro@inoxa.pl", "Kraków",
       "Bauhaus", "bauhaus.de", "Mannheim"]


def _mk(s, email, role, account_class, role_key, name=""):
    u = User(email=email, name=name or email.split("@")[0], role=role, active=True, password_hash=hash_password("pw"))
    s.add(u); s.commit(); s.refresh(u)
    s.add(UserProfile(user_id=u.id, account_class=account_class, role_key=role_key, account_status="active",
                      scope=P.ROLE_TEMPLATES[role_key]["scope"], full_name=u.name, display_name=u.name))
    s.commit()
    return u


def _buyer(s, sr, ref, stages, **kw):
    ld = Lead(product="Anchors", managed=True, owner_id=None, seller_id=sr.owner_id, request_id=sr.id,
              anon_ref=ref, pipeline_stage=stages[-1], source=f"req-{sr.id}", tracking_code=f"G4-{ref}", **kw)
    s.add(ld); s.commit(); s.refresh(ld)
    frm = ""
    for st in stages:
        s.add(StageEvent(lead_id=ld.id, request_id=sr.id, from_stage=frm, to_stage=st)); frm = st
    s.commit()
    return ld


@pytest.fixture
def ctx(monkeypatch):
    e = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    SQLModel.metadata.create_all(e)
    with e.connect() as c:
        for ddl in _IDX:
            c.execute(text(ddl))
        c.commit()
    monkeypatch.setattr(main, "engine", e)
    ids = {}
    with Session(e) as s:
        founder = _mk(s, "founder@t.local", "admin", "internal", "founder")
        manager = _mk(s, "manager@t.local", "admin", "internal", "admin_manager")
        analyst = _mk(s, "analyst@t.local", "admin", "internal", "analyst")
        seller = _mk(s, "trsharks@t.local", "agent", "seller", "seller", name="Sharks")
        other = _mk(s, "other@t.local", "agent", "seller", "seller")
        sr = ServiceRequest(tracking_code="SR-202608-0001", request_type="buyer_hunt", product="Drywall anchors",
                            status="done", workflow_status="delivered", owner_id=seller.id, requester_id=seller.id,
                            result_source_tag="req-1")
        sr2 = ServiceRequest(tracking_code="SR-202608-0002", request_type="buyer_hunt", product="Tea",
                             status="done", owner_id=other.id, requester_id=other.id)
        s.add(sr); s.add(sr2); s.commit(); s.refresh(sr); s.refresh(sr2)
        l1 = _buyer(s, sr, "Buyer-CA-001", ["identified", "contacted"], buyer_company="Richelieu Hardware Ltd",
                    contact_name="Marc Tremblay", email="sales@richelieu.example", phone="+1 514 555 0101",
                    website="https://www.richelieu.example/ca", dest_country="CA", dest_city="Montreal, QC",
                    notes="secret pricing note", source_url="https://directory.example/richelieu")
        l2 = _buyer(s, sr, "Buyer-PL-001", ["identified", "contacted", "responded"],
                    buyer_company="Inoxa Sp. z o.o.", email="biuro@inoxa.pl", website="inoxa.pl",
                    dest_country="PL", dest_city="Kraków")
        l3 = _buyer(s, sr, "Buyer-DE-001", ["identified"], buyer_company="Bauhaus Fachcentrum",
                    website="bauhaus.de", dest_country="DE", dest_city="Mannheim")
        _buyer(s, sr2, "Buyer-IQ-001", ["identified"], buyer_company="Other Tea Co", dest_country="IQ")
        ids.update(founder=founder.id, manager=manager.id, analyst=analyst.id, seller=seller.id, other=other.id,
                   req=sr.id, req2=sr2.id, l1=l1.id, l2=l2.id, l3=l3.id)
    return e, ids


def _client(email):
    c = TestClient(main.app)
    assert c.post("/login", data={"email": email, "password": "pw"}, follow_redirects=False).status_code == 303
    return c


def _no_pii(body, where):
    for leak in PII:
        assert leak not in body, f"LEAK {leak!r} at {where}"


# ------------------------------------------------------------------ privacy via the seller's routes
def test_seller_pages_never_show_a_buyer_identity(ctx):
    e, ids = ctx
    c = _client("trsharks@t.local")
    for path in ("/", "/requests", "/leads", f"/requests/{ids['req']}/status", f"/requests/{ids['req']}/thread",
                 "/me/profile"):
        r = c.get(path)
        assert r.status_code == 200, path
        _no_pii(r.text, path)
    assert "Buyer-CA-001" in c.get("/requests").text                       # the anonymized ref is what they get


def test_anon_prospect_has_no_region_and_no_pii(ctx):
    e, ids = ctx
    with Session(e) as s:
        ld = s.get(Lead, ids["l1"])
        p = pipeline.anon_prospect(ld)
    assert "region" not in p
    assert set(p) == {"anon_ref", "stage", "stage_key", "country", "category", "size_band", "fit_score",
                      "action_required"}
    for v in p.values():
        assert str(v) not in ("Montreal, QC", ld.buyer_company, ld.email, ld.phone, ld.website, ld.notes,
                              ld.source_url, ld.contact_name)


def test_status_poll_shows_only_seller_safe_deliverables(ctx):
    e, ids = ctx
    with Session(e) as s:
        s.add(RequestDeliverable(request_id=ids["req"], note="INTERNAL admin buyer csv",
                                 url="https://drive.example/internal-admin", seller_safe=False))
        s.add(RequestDeliverable(request_id=ids["req"], note="Market overview", url="https://drive.example/overview",
                                 seller_safe=True))
        s.commit()
    seller = _client("trsharks@t.local")
    for path in (f"/requests/{ids['req']}/status", "/requests", "/"):
        body = seller.get(path).text
        assert "INTERNAL admin buyer csv" not in body and "drive.example/internal-admin" not in body, path
    body = seller.get(f"/requests/{ids['req']}/status").text
    assert "Market overview" in body and "drive.example/overview" in body
    admin = _client("founder@t.local").get(f"/requests/{ids['req']}/status").text
    assert "INTERNAL admin buyer csv" in admin and "Market overview" in admin


def test_internal_delivery_never_reaches_the_seller(ctx):
    e, ids = ctx
    admin = _client("founder@t.local")
    assert admin.post(f"/admin/requests/{ids['req']}/done",
                      data={"result": "Internal: see the ADMIN csv", "url": "https://drive.example/admin-csv"},
                      follow_redirects=False).status_code == 303
    with Session(e) as s:
        sr = s.get(ServiceRequest, ids["req"])
        assert sr.result == "" and sr.result_url == ""                      # not copied onto the seller card
        assert len(s.exec(select(RequestDeliverable)).all()) == 1           # still recorded for the admin
    seller = _client("trsharks@t.local")
    for path in ("/requests", "/", f"/requests/{ids['req']}/status"):
        body = seller.get(path).text
        assert "ADMIN csv" not in body and "drive.example/admin-csv" not in body, path


def test_legacy_result_link_fallback_is_admin_only(ctx):
    e, ids = ctx
    with Session(e) as s:                     # pre-fix data: a link/file on the request itself, no deliverable rows
        sr = s.get(ServiceRequest, ids["req"])
        sr.result_url = "https://drive.example/old-internal"; sr.result_file_path = f"{sr.id}/buyers_ADMIN.csv"
        s.add(sr); s.commit()
    seller = _client("trsharks@t.local")
    for path in ("/requests", "/", f"/requests/{ids['req']}/status"):
        body = seller.get(path).text
        assert "old-internal" not in body and f"/requests/{ids['req']}/result/file" not in body, path
    assert "old-internal" in _client("founder@t.local").get(f"/requests/{ids['req']}/status").text


def test_seller_safe_delivery_naming_a_buyer_is_refused(ctx):
    e, ids = ctx
    admin = _client("founder@t.local")
    for data in ({"result": "Richelieu Hardware asked for a quote", "seller_safe": "1"},
                 {"result": "write to sales@richelieu.example", "seller_safe": "1"},
                 {"result": "Buyers in Montreal", "seller_safe": "1"},
                 {"result": "Summary", "url": "https://www.richelieu.example/ca/catalog", "seller_safe": "1"},
                 {"result": "Summary", "url": "mailto:someone@x.example", "seller_safe": "1"}):
        admin.post(f"/admin/requests/{ids['req']}/done", data=data, follow_redirects=False)
    with Session(e) as s:
        assert s.exec(select(RequestDeliverable)).all() == []
        assert s.get(ServiceRequest, ids["req"]).result == ""
    # a clean seller-safe delivery goes through and the seller sees it
    admin.post(f"/admin/requests/{ids['req']}/done",
               data={"result": "Outreach report for September", "url": "https://drive.example/report-sept",
                     "seller_safe": "1"}, follow_redirects=False)
    with Session(e) as s:
        sr = s.get(ServiceRequest, ids["req"])
        assert sr.result == "Outreach report for September" and sr.result_url == "https://drive.example/report-sept"
    body = _client("trsharks@t.local").get("/requests").text
    assert "Outreach report for September" in body and "drive.example/report-sept" in body


# ------------------------------------------------------------------ honest status text + no whole-card poll
def test_running_buyer_hunt_card_is_honest_and_does_not_poll(ctx):
    e, ids = ctx
    with Session(e) as s:
        sr = s.get(ServiceRequest, ids["req"]); sr.status = "running"; s.add(sr)
        s.add(ServiceRequest(tracking_code="SR-202608-0003", request_type="contract", product="Sale contract",
                             status="running", owner_id=ids["seller"], requester_id=ids["seller"]))
        s.commit()
        contract_id = s.exec(select(ServiceRequest).where(ServiceRequest.request_type == "contract")).one().id
    c = _client("trsharks@t.local")
    body = c.get("/requests").text
    assert "Outreach in progress" in body and "we are contacting buyers for you" in body
    assert f'hx-get="/requests/{ids["req"]}/status"' not in body           # no 5 s poll that wipes the chat draft
    assert f'hx-get="/requests/{contract_id}/status"' in body              # other running services still refresh
    assert "Research in progress" in body                                   # (the contract card's wording)
    status = c.get(f"/requests/{ids['req']}/status").text
    assert "Outreach in progress" in status and f'hx-get="/requests/{ids["req"]}/status"' not in status


def test_done_buyer_hunt_reads_completed_not_delivered(ctx):
    e, ids = ctx
    body = _client("trsharks@t.local").get(f"/requests/{ids['req']}/status").text
    assert "Completed" in body and "Delivered" not in body
    assert "view 0 buyers" not in body and "/leads?source=" not in body


# ------------------------------------------------------------------ seller dashboard funnel + wording
def test_seller_dashboard_shows_the_managed_funnel(ctx):
    e, ids = ctx
    body = _client("trsharks@t.local").get("/").text
    assert "Buyers we&#39;re working for you" in body or "Buyers we're working for you" in body
    kpis = body.split("Buyers we")[1].split("Request a buyer search")[0]
    assert ">3<" in kpis                                                     # total managed buyers
    assert "Contacted" in kpis and ">2<" in kpis                             # identified→contacted (x2)
    assert "In conversation" in kpis and ">1<" in kpis                       # one replied
    assert "across 3 countries" in kpis
    assert "No buyers yet" not in body and "into your account" not in body
    _no_pii(body, "/")


def test_seller_without_managed_buyers_keeps_the_classic_dashboard(ctx):
    e, ids = ctx
    body = _client("other@t.local").get("/").text                           # their request has 1 buyer
    assert "Buyers we" in body
    with Session(e) as s:                                                    # a seller with no request at all
        _mk(s, "fresh@t.local", "agent", "seller", "seller")
    fresh = _client("fresh@t.local").get("/").text
    assert "My buyers" in fresh and "Buyers we" not in fresh and "into your account" not in fresh


def test_request_texts_promise_anonymized_progress_not_delivery(ctx):
    e, ids = ctx
    c = _client("trsharks@t.local")
    assert "into your account" not in c.get("/requests").text
    c.post("/requests", data={"product": "Wall plugs", "request_type": "buyer_hunt"}, follow_redirects=False)
    body = c.get("/requests").text
    assert "will appear here once it" not in body and "anonymized progress" in body


# ------------------------------------------------------------------ publish hardening (scan + denylist + anon_ref)
def _publish(c, rid, **data):
    return c.post(f"/admin/requests/{rid}/publish", data=data, follow_redirects=False)


@pytest.mark.parametrize("summary", [
    "Their site inoxa.pl lists anchors",                 # bare country domain (.pl)
    "Compare with bauhaus.de prices",                    # bare country domain (.de)
    "Prices like toolfast.ie or foo.nz",                 # more 2-letter domains
    "Richelieu Hardware asked for pricing",              # buyer name (legal form dropped)
    "RICHELIEU HARDWARE LTD replied",                    # buyer name, any case
    "Richelieu wants a quote",                           # the brand alone (= their own web domain)
    "Inoxa wants samples",                               # buyer name (Sp. z o.o. dropped)
    "A buyer in Montreal replied",                       # buyer city
    "A distributor in Krakow is interested",             # buyer city without the accent
    "Call 514 555 0101 tomorrow",                        # buyer phone digits
    "Their local line is 555-0101",                      # too short for the phone pattern; the denylist knows it
])
def test_publish_blocks_domains_and_buyer_identity(ctx, summary):
    e, ids = ctx
    admin = _client("founder@t.local")
    _publish(admin, ids["req"], public_status="Update", summary=summary)
    with Session(e) as s:
        assert s.exec(select(SellerUpdate)).first() is None, summary
    pv = admin.post(f"/admin/requests/{ids['req']}/publish/preview",
                    data={"public_status": "Update", "summary": summary}).text
    assert "Blocked" in pv and "disabled" in pv


def test_publish_buyer_identity_only_from_this_request(ctx):
    e, ids = ctx
    admin = _client("founder@t.local")
    _publish(admin, ids["req"], summary="Other Tea Co is a buyer of another seller")   # not this request's buyer
    with Session(e) as s:
        assert len(s.exec(select(SellerUpdate)).all()) == 1


def test_publish_validates_anon_ref_and_links_the_buyer(ctx):
    e, ids = ctx
    admin = _client("founder@t.local")
    for bad in ("b7@secretco7.example", "Buyer-IQ-001", "Buyer-CA-999", "Richelieu"):
        _publish(admin, ids["req"], anon_ref=bad, summary="Buyer requested pricing.")
    with Session(e) as s:
        assert s.exec(select(SellerUpdate)).first() is None
        ld = s.get(Lead, ids["l1"]); ld.seller_action_required = True; s.add(ld); s.commit()
    pv = admin.post(f"/admin/requests/{ids['req']}/publish/preview",
                    data={"anon_ref": "b7@secretco7.example", "summary": "x"}).text
    assert "Blocked" in pv
    _publish(admin, ids["req"], anon_ref="Buyer-CA-001", public_status="Pricing requested",
             summary="Buyer requested pricing for 5,000 pcs under CIF.", seller_question="What is your floor price?")
    with Session(e) as s:
        su = s.exec(select(SellerUpdate)).one()
        assert su.lead_id == ids["l1"] and su.anon_ref == "Buyer-CA-001" and su.status == "open"
        sid = su.id
    seller = _client("trsharks@t.local")
    assert "Buyer requested pricing for 5,000 pcs" in seller.get("/requests").text
    seller.post(f"/requests/{ids['req']}/updates/{sid}/resolve", data={"answer": "3.80 per box"},
                follow_redirects=False)
    with Session(e) as s:
        assert s.get(SellerUpdate, sid).status == "resolved"
        assert s.get(Lead, ids["l1"]).seller_action_required is False        # the linked buyer's flag cleared


def test_admin_chat_and_answers_use_the_denylist(ctx):
    e, ids = ctx
    admin = _client("founder@t.local")
    for msg in ("Richelieu Hardware replied today", "the buyer in Montreal wants samples",
                "see www.richelieu.example", "they are on inoxa.pl"):
        admin.post(f"/requests/{ids['req']}/messages", data={"body": msg}, follow_redirects=False)
    with Session(e) as s:
        assert s.exec(select(RequestMessage)).all() == []
    admin.post(f"/requests/{ids['req']}/messages", data={"body": "Two buyers replied this week."},
               follow_redirects=False)
    with Session(e) as s:
        assert [m.body for m in s.exec(select(RequestMessage)).all()] == ["Two buyers replied this week."]
        su = SellerUpdate(request_id=ids["req"], seller_id=ids["seller"], seller_question="Can you ship by May?",
                          status="open", published=True)
        s.add(su); s.commit(); s.refresh(su); sid = su.id
    admin.post(f"/requests/{ids['req']}/updates/{sid}/resolve", data={"answer": "Bauhaus confirmed"},
               follow_redirects=False)
    with Session(e) as s:
        assert s.get(SellerUpdate, sid).status == "open"                    # blocked → nothing resolved
    # the seller's own messages are never buyer-scanned (they may share their own contact with Go4it)
    _client("trsharks@t.local").post(f"/requests/{ids['req']}/messages", data={"body": "Montreal is fine for us"},
                                     follow_redirects=False)
    with Session(e) as s:
        assert any("Montreal is fine" in m.body for m in s.exec(select(RequestMessage)).all())


def test_denylist_needles_and_matching():
    leads = [Lead(buyer_company="Kenroc Building Materials Co. Ltd.", email="info@kenroc.example; x@gmail.com",
                  website="https://kenroc.example/", dest_city="Regina, SK (14 branches across AB/BC/SK/MB)",
                  phone="+1 (306) 555-0199", dest_country="CA"),
             Lead(buyer_company="IHL Canada (Investments Hardware Ltd.)", dest_city="Spain", dest_country="ES"),
             Lead(buyer_company="ACE", dest_city="Nationwide", dest_country="QA"),
             Lead(buyer_company="Fastener Agencies", website="fastener.co.nz", dest_city="Łódź", dest_country="NZ"),
             Lead(buyer_company="Fixings Ltd", dest_country="GB")]
    needles = pipeline.denylist_needles(leads)
    hit = lambda t: pipeline.denylist_hits(t, needles)                       # noqa: E731
    assert hit("Kenroc Building Materials asked") and hit("kenroc.example") and hit("in Regina")
    assert hit("Kenroc replied")                             # the brand alone: it is their own web domain
    assert hit("IHL Canada replied") and hit("Investments Hardware") and hit("dial 306 555 0199")
    assert hit("info@kenroc.example") and hit("Fastener Agencies called") and hit("a buyer in Lodz")
    assert not hit("Two buyers in Spain replied")            # a country in the city field is not a needle
    assert not hit("ACE quality, nationwide reach")          # <4-char names and generic place words are skipped
    assert not hit("gmail.com addresses")                    # free-mail hosts are never buyer hosts
    assert not hit("Contacted 25 new fastener distributors in Canada")   # a trade word is never a brand needle
    assert not hit("They want fixings for drywall") and hit("Fixings Ltd replied")   # ... nor a whole name
    assert not hit("Reginald from our team")                 # whole words only
    assert not hit("Order of 5550 boxes, 199 pallets")       # short digit groups are not a phone


# ------------------------------------------------------------------ admin displays use the managed funnel
def test_admin_displays_count_managed_buyers(ctx):
    e, ids = ctx
    admin = _client("founder@t.local")
    assert "3 buyers" in admin.get("/admin/requests").text
    assert "Confidential buyer pipeline (3 buyers)" in admin.get(f"/admin/requests/{ids['req']}?tab=related").text
    pipe = admin.get(f"/admin/requests/{ids['req']}?tab=pipeline").text
    assert "managed buyers" in pipe and "buyers delivered" not in pipe
    with Session(e) as s:
        assert s.get(ServiceRequest, ids["req"]).leads_delivered == 0          # legacy counter left alone


# ------------------------------------------------------------------ workflow history records the reconciled legacy
def test_workflow_history_records_reconciled_legacy(ctx):
    e, ids = ctx
    admin = _client("founder@t.local")
    admin.post(f"/admin/requests/{ids['req']}/workflow", data={"to": "in_progress", "reason": "wave 1"},
               follow_redirects=False)
    with Session(e) as s:
        sr = s.get(ServiceRequest, ids["req"])
        assert sr.status == "running" and sr.workflow_status == "in_progress"
        ev = s.exec(select(RequestStatusEvent).where(RequestStatusEvent.request_id == sr.id)).all()[-1]
        assert (ev.from_status, ev.to_status, ev.from_legacy, ev.to_legacy) == ("delivered", "in_progress",
                                                                                 "done", "running")
        fresh = ServiceRequest(request_type="buyer_hunt", product="X", status="submitted", owner_id=ids["seller"],
                               requester_id=ids["seller"])
        s.add(fresh); s.commit(); s.refresh(fresh); fid = fresh.id
    admin.post(f"/admin/requests/{fid}/approve", follow_redirects=False)
    with Session(e) as s:
        ev = s.exec(select(RequestStatusEvent).where(RequestStatusEvent.request_id == fid)).all()[-1]
        assert (ev.from_legacy, ev.to_legacy) == ("submitted", "approved")


# ------------------------------------------------------------------ login email change
def test_founder_changes_a_seller_login_email(ctx):
    e, ids = ctx
    seller = _client("trsharks@t.local")
    admin = _client("founder@t.local")
    r = admin.post(f"/admin/users/{ids['seller']}/email",
                   data={"email": "  Sales@TRSharks.Example ", "reason": "real address"}, follow_redirects=False)
    assert r.status_code == 303 and "ok=" in r.headers["location"]
    with Session(e) as s:
        assert s.get(User, ids["seller"]).email == "sales@trsharks.example"
        row = s.exec(select(AccessAuditLog).where(AccessAuditLog.action == "email_changed")).one()
        assert row.target_user_id == ids["seller"] and row.before == "trsharks@t.local"
        assert row.after == "sales@trsharks.example" and row.actor_id == ids["founder"]
    assert seller.get("/", follow_redirects=False).status_code in (302, 303)   # old session revoked
    assert TestClient(main.app).post("/login", data={"email": "trsharks@t.local", "password": "pw"},
                                     follow_redirects=False).headers["location"].startswith("/login")
    _client("SALES@trsharks.example")                                           # the new address logs in


def test_change_email_rules(ctx):
    e, ids = ctx
    with Session(e) as s:
        founder, manager = s.get(User, ids["founder"]), s.get(User, ids["manager"])
        seller, analyst = s.get(User, ids["seller"]), s.get(User, ids["analyst"])
        assert ACCESS.change_email(s, founder, seller, "OTHER@t.local")[0] is False          # duplicate, any case
        assert ACCESS.change_email(s, founder, seller, "not-an-email")[0] is False
        assert ACCESS.change_email(s, founder, seller, "a b@x.example")[0] is False
        assert ACCESS.change_email(s, founder, seller, "trsharks@t.local")[0] is False       # unchanged
        assert ACCESS.change_email(s, manager, founder, "boss@x.example")[0] is False        # founder-protected
        assert ACCESS.change_email(s, analyst, seller, "x@x.example")[0] is False            # no users.manage
        ok, _m = ACCESS.change_email(s, manager, seller, "new@x.example"); s.commit()
        assert ok and s.get(User, ids["seller"]).email == "new@x.example"
        assert s.exec(select(UserProfile).where(UserProfile.user_id == ids["seller"])).one().sessions_revoked_at
        assert s.get(User, ids["founder"]).email == "founder@t.local"


def test_change_email_route_is_users_manage_only(ctx):
    e, ids = ctx
    for who in ("trsharks@t.local", "analyst@t.local"):
        assert _client(who).post(f"/admin/users/{ids['other']}/email", data={"email": "x@x.example"},
                                 follow_redirects=False).status_code == 403
    admin = _client("founder@t.local")
    r = admin.post(f"/admin/users/{ids['other']}/email", data={"email": "founder@t.local"}, follow_redirects=False)
    assert "error=" in r.headers["location"]
    with Session(e) as s:
        assert s.get(User, ids["other"]).email == "other@t.local"
    assert "Login email" in admin.get(f"/admin/users/{ids['seller']}?tab=security").text


# ------------------------------------------------------------------ seller identity fields are locked
def test_seller_cannot_self_edit_identity_fields(ctx):
    e, ids = ctx
    c = _client("trsharks@t.local")
    c.post("/me/profile", data={"company": "Anchor", "full_name": "Hardware", "display_name": "Fix",
                                "country": "TR", "timezone": "Europe/Istanbul"}, follow_redirects=False)
    with Session(e) as s:
        p = s.exec(select(UserProfile).where(UserProfile.user_id == ids["seller"])).one()
        assert (p.company, p.full_name, p.display_name) == ("", "Sharks", "Sharks")
        assert p.country == "TR" and p.timezone == "Europe/Istanbul"     # the other fields still save
        needles = CR._seller_needles(s, ids["seller"])
        assert "anchor" not in needles and "hardware" not in needles
    assert c.get("/me/profile").status_code == 200
    # internal staff still edit their own names
    a = _client("analyst@t.local")
    a.post("/me/profile", data={"full_name": "Ana Lyst", "display_name": "Ana"}, follow_redirects=False)
    with Session(e) as s:
        p = s.exec(select(UserProfile).where(UserProfile.user_id == ids["analyst"])).one()
        assert (p.full_name, p.display_name) == ("Ana Lyst", "Ana")
