"""Phase 12 — Work Queue clutter.

STEP 1 (scanners): a task closes once an admin has settled its condition (duplicate pair disposed, product archived,
opportunity decided, data source fresh again, reply outcome recorded); a stale-source alert is per episode; one bounce
raises ONE task (replace_invalid_contact), not a second 'Failed outreach'.
STEP 2 (scripts/cleanup_work_queue.py): dry-run by default (nothing persists), --apply dismisses/completes per rule with
batch tags + audit, never touches buyer-reply tasks / managed buyers / sellers / manual or assigned tasks, a later sync
recreates nothing, --revert re-opens a batch, an invariant violation rolls everything back, output has no PII.
"""
import json
import re
from datetime import datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine, func, select

import app.main as main
from app import company_service as CS
from app import inbound_email as IE
from app import opportunities as OPP
from app import outreach_events as OE
from app import suppression as SUP
from app import work_queue as WQ
from app.auth import hash_password
from app.models import (AuditLog, BounceRecord, Campaign, CampaignRecipient, Company, DuplicateCandidate, Lead,
                        Opportunity, Outreach, Product, Provenance, Quote, QuoteStatusEvent, User, UserProfile,
                        WorkItem)
from scripts import cleanup_work_queue as CWQ

IDX = ("CREATE UNIQUE INDEX IF NOT EXISTS uq_workitem_idem_open ON workitem(idempotency_key) "
       "WHERE idempotency_key != '' AND status IN ('open','in_progress','waiting')",
       "CREATE UNIQUE INDEX IF NOT EXISTS uq_suppression_addr_scope ON suppression(email_normalized, scope, tenant_id) "
       "WHERE active = 1",
       "CREATE UNIQUE INDEX IF NOT EXISTS uq_outreach_campaign_send ON "
       "outreach(campaign_id, campaign_recipient_id, campaign_version, campaign_step) WHERE campaign_id IS NOT NULL")


def _engine():
    e = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    SQLModel.metadata.create_all(e)
    with e.connect() as c:
        for ddl in IDX:
            c.execute(text(ddl))
        c.commit()
    return e


@pytest.fixture
def db(monkeypatch):
    e = _engine()
    monkeypatch.setattr(main, "engine", e)
    for name in ("notify_bounce", "notify_buyer_reply", "send_message", "enrich_lead"):   # never a real alert/scrape
        monkeypatch.setattr(IE, name, lambda *a, **k: None)
    with Session(e) as s:
        s.add(User(email="admin@t.local", name="Admin", role="admin", active=True, password_hash=hash_password("pw")))
        s.commit()
    return e


def _by_key(s, key):
    return s.exec(select(WorkItem).where(WorkItem.idempotency_key == key).order_by(WorkItem.id)).all()


# ====================================================================== STEP 1 — scanner fixes
def test_dup_review_task_closes_once_the_pair_is_disposed(db):
    with Session(db) as s:
        for i in range(6):
            s.add(DuplicateCandidate(left_id=2 * i + 1, right_id=2 * i + 2, status="open"))
        s.commit()
        assert WQ.sync_open_duplicates(s) == 6; s.commit()
        cands = s.exec(select(DuplicateCandidate).order_by(DuplicateCandidate.id)).all()
        for dc, st in zip(cands, ("not_duplicate", "merged", "linked", "deferred", "confirmed", "open")):
            dc.status = st
            s.add(dc)
        s.commit()
        assert WQ.sync_open_duplicates(s) == 0; s.commit()
        for dc in cands:
            [wi] = _by_key(s, f"review_dup:cand:{dc.id}")
            if dc.status in ("open", "confirmed"):
                assert wi.status == "open"                              # 'confirmed' still needs the merge
            else:
                assert (wi.status, wi.resolution_note, wi.resolved_by) == ("completed", f"candidate {dc.status}", None)
        assert WQ.sync_open_duplicates(s) == 0; s.commit()             # nothing re-raised
        assert s.exec(select(func.count(WorkItem.id))).one() == 6


def test_merged_pair_closes_and_an_unmerge_alerts_again(db):
    with Session(db) as s:
        a, b = Company(name="Alpha Trading"), Company(name="Alpha Trading Co")
        s.add(a); s.add(b); s.commit(); s.refresh(a); s.refresh(b)
        dc = DuplicateCandidate(left_id=a.id, right_id=b.id, status="open")
        s.add(dc); s.commit(); s.refresh(dc)
        key = f"review_dup:cand:{dc.id}"
        WQ.sync_open_duplicates(s); s.commit()
        assert CS.merge_companies(s, a.id, b.id, None) == (True, ""); s.commit()
        WQ.sync_open_duplicates(s); s.commit()
        assert [w.status for w in _by_key(s, key)] == ["completed"]
        assert CS.unmerge_companies(s, b.id, None) == (True, ""); s.commit()   # the pair is open again
        assert WQ.sync_open_duplicates(s) == 1; s.commit()
        assert [w.status for w in _by_key(s, key)] == ["completed", "open"]
        assert WQ.sync_open_duplicates(s) == 0


def test_archived_product_task_closes(db):
    with Session(db) as s:
        p, q = Product(name="Rubber tile", active=True), Product(name="Cold asphalt", active=True)
        s.add(p); s.add(q); s.commit(); s.refresh(p); s.refresh(q)
        assert WQ.sync_incomplete_products(s) == 2; s.commit()
        p.status, p.active = "archived", False                         # what POST /catalog/products/{id}/status does
        s.add(p); s.commit()
        WQ.sync_incomplete_products(s); s.commit()
        [wp] = _by_key(s, f"product_incomplete:product:{p.id}")
        assert (wp.status, wp.resolution_note, wp.resolved_by) == ("completed", "product archived", None)
        assert [w.status for w in _by_key(s, f"product_incomplete:product:{q.id}")] == ["open"]
        assert WQ.sync_incomplete_products(s) == 0; s.commit()
        assert s.exec(select(func.count(WorkItem.id))).one() == 2


def test_opportunity_task_closes_after_the_decision_not_on_a_detour(db):
    with Session(db) as s:
        opps = [Opportunity(reference=f"OPP-{i}", status="new", score=45) for i in range(3)]
        for o in opps:
            s.add(o)
        s.commit()
        a, b, c = opps
        assert WQ.sync_opportunities_needing_review(s) == 3; s.commit()

        def task(o):
            return [(w.status, w.resolution_note) for w in _by_key(s, f"opportunity_needs_review:opp:{o.id}")]
        for to in ("needs_research", "ready_for_review"):              # research detour: the review stays open
            assert OPP.set_status(s, a, to)[0]; s.commit()
            assert WQ.sync_opportunities_needing_review(s) == 0; s.commit()
            assert task(a) == [("open", "")]
        assert OPP.set_status(s, a, "approved")[0]
        assert OPP.set_status(s, b, "rejected")[0]
        assert OPP.set_status(s, c, "archived")[0]
        s.commit()
        assert WQ.sync_opportunities_needing_review(s) == 0; s.commit()
        assert task(a) == [("completed", "opportunity approved")]
        assert task(b) == [("completed", "opportunity rejected")]
        assert task(c) == [("completed", "opportunity archived")]


def _command_harvest_seen(s, when):
    pv = s.exec(select(Provenance).where(Provenance.source_type == "command")).first() \
        or Provenance(entity_id=1, source_type="command")
    pv.last_seen_at = when
    s.add(pv); s.commit()


SRC_KEY = "source_stale_failed:src:command_harvest"


def test_stale_source_closes_when_fresh_and_a_new_episode_alerts_again(db):
    now = datetime.utcnow()
    first, second = now - timedelta(days=30), now - timedelta(days=25)   # window 7d → stale after 21d
    with Session(db) as s:
        _command_harvest_seen(s, first)
        assert WQ.sync_stale_sources(s) == 1; s.commit()
        [wi] = _by_key(s, SRC_KEY)
        assert wi.condition_version == f"Stale:{first:%Y%m%d}"
        WQ.dismiss_item(s, wi, "command harvests are paused", None); s.commit()
        assert WQ.sync_stale_sources(s) == 0; s.commit()               # same episode: the dismissal holds
        _command_harvest_seen(s, now)                                   # a harvest ran → Current
        assert WQ.sync_stale_sources(s) == 0; s.commit()
        _command_harvest_seen(s, second)                                # …and went stale again: a NEW episode
        assert WQ.sync_stale_sources(s) == 1; s.commit()
        items = _by_key(s, SRC_KEY)
        assert [w.status for w in items] == ["dismissed", "open"]
        assert items[1].condition_version == f"Stale:{second:%Y%m%d}"
        _command_harvest_seen(s, now)                                   # fresh again → the open alert closes
        WQ.sync_stale_sources(s); s.commit()
        assert (_by_key(s, SRC_KEY)[1].status, _by_key(s, SRC_KEY)[1].resolution_note) == ("completed",
                                                                                           "source current")


def test_a_dismissed_pre_phase12_source_alert_is_not_re_raised(db):
    now = datetime.utcnow()
    with Session(db) as s:
        _command_harvest_seen(s, now - timedelta(days=30))
        old = WorkItem(type="source_stale_failed", status="dismissed", source="automatic", idempotency_key=SRC_KEY,
                       condition_version="Stale", created_at=now - timedelta(days=5))    # the old bare-label version
        s.add(old); s.commit()
        assert WQ.sync_stale_sources(s) == 0; s.commit()               # dismissed during THIS episode → held
        old.created_at = now - timedelta(days=40); s.add(old); s.commit()   # …but one from an older episode isn't
        assert WQ.sync_stale_sources(s) == 1


def test_a_campaign_bounce_raises_one_task_not_two(db):
    with Session(db) as s:
        seller = User(email="sharks@t.local", role="agent", active=True)
        s.add(seller); s.commit(); s.refresh(seller)
        ld = Lead(product="Anchors", managed=True, seller_id=seller.id, request_id=1, email="hb@buyer.example")
        c = Campaign(name="TRSHARKS anchors", tenant_id=seller.id, status="running")
        s.add(ld); s.add(c); s.commit(); s.refresh(ld); s.refresh(c)
        s.add(Outreach(lead_id=ld.id, direction="out", recipient="hb@buyer.example", status="sent", campaign_id=c.id,
                       campaign_recipient_id=1, campaign_version=1, campaign_step=0, message_id="<m1@x>"))
        s.commit()
        assert IE.handle_bounce(s, "hb@buyer.example", "550 5.1.1 user unknown") == "bounced"
        WQ.run_all_sync(s, None); s.commit()
        WQ.run_all_sync(s, None); s.commit()
        types = [w.type for w in s.exec(select(WorkItem)).all()]
        assert types.count("replace_invalid_contact") == 1
        assert "failed_system_job" not in types


def test_a_send_failure_that_is_not_a_bounce_still_raises_a_task(db):
    with Session(db) as s:
        a, b = Lead(product="x", email="a@buyer.example"), Lead(product="x", email="b@buyer.example")
        s.add(a); s.add(b); s.commit(); s.refresh(a); s.refresh(b)
        # b's address bounced 10 days BEFORE this send — today's failure is not that bounce
        s.add(BounceRecord(email_normalized="b@buyer.example", last_bounce_at=datetime.utcnow() - timedelta(days=10)))
        s.add(Outreach(lead_id=a.id, recipient="a@buyer.example", status="failed", error="535 auth failed"))
        s.add(Outreach(lead_id=b.id, recipient="b@buyer.example", status="failed", error="timed out"))
        s.commit()
        assert WQ.sync_failed_jobs(s) == 2


def _login(c, email, pw="pw"):
    assert c.post("/login", data={"email": email, "password": pw}, follow_redirects=False).status_code == 303


def test_recording_a_reply_outcome_closes_its_review_task(db):
    with Session(db) as s:
        admin_id = s.exec(select(User)).first().id
        ld = Lead(product="Anchors", email="r@buyer.example")
        s.add(ld); s.commit(); s.refresh(ld)
        lid = ld.id
        s.add(Outreach(lead_id=lid, direction="in", status="received")); s.commit()
        OE.on_reply(s, ld, "Re: anchors", "Yes please, send the price list")
        assert [w.status for w in _by_key(s, f"review_inbound_reply:lead:{lid}")] == ["open"]
    c = TestClient(main.app)
    _login(c, "admin@t.local")
    assert c.post(f"/inbox/{lid}/outcome", data={"outcome": ""}, follow_redirects=False).status_code == 303
    with Session(db) as s:                                              # no outcome chosen → still to review
        assert [w.status for w in _by_key(s, f"review_inbound_reply:lead:{lid}")] == ["open"]
    assert c.post(f"/inbox/{lid}/outcome", data={"outcome": "positive"}, follow_redirects=False).status_code == 303
    with Session(db) as s:
        [wi] = _by_key(s, f"review_inbound_reply:lead:{lid}")
        assert (wi.status, wi.resolution_note, wi.resolved_by) == ("completed", "outcome: positive", admin_id)


# ====================================================================== STEP 2 — scripts/cleanup_work_queue.py
FOUNDER = "founder@t.local"
AUG = datetime(2026, 8, 23)          # when the legacy (inferred) tasks were seeded
JULY = datetime(2026, 7, 28)         # the lead-import auto-drafts
NAMES = ("Kartli", "Honey House", "Bee Co", "Anchor Buyer", "Rubber tile", "Real Buyer", "Zinc Corp", "Fixed Co")


def _user(s, email, role, cls, status="active"):
    u = User(email=email, name=email.split("@")[0], role=role, active=(status == "active"), password_hash="x")
    s.add(u); s.commit(); s.refresh(u)
    s.add(UserProfile(user_id=u.id, account_class=cls, role_key="founder" if cls == "internal" else "seller",
                      account_status=status))
    s.commit()
    return u


@pytest.fixture
def world(monkeypatch):
    """A queue shaped like prod's: legacy noise, settled conditions, TRSHARKS work, buyer replies, manual tasks."""
    e = _engine()
    monkeypatch.setattr(CWQ, "engine", e)
    monkeypatch.setattr(CWQ, "init_db", lambda: None)
    monkeypatch.setattr(main, "engine", e)
    k = {}
    with Session(e) as s:
        f = _user(s, FOUNDER, "admin", "internal")
        seller = _user(s, "sharks@t.local", "agent", "seller")
        demo = _user(s, "ali@go4it.local", "agent", "seller", status="disabled")    # seed demo agent, disabled

        def lead(**kw):
            kw.setdefault("product", "Tiles")
            kw.setdefault("owner_id", f.id)
            ld = Lead(**kw)
            s.add(ld); s.commit(); s.refresh(ld)
            return ld
        research = lead(source="research-tile", buyer_company="Kartli Tiles LLC", email="tiles@kartli.example")
        managed = lead(owner_id=None, managed=True, seller_id=seller.id, request_id=1, product="Anchors",
                       buyer_company="Anchor Buyer GmbH", email="buy@anchor.example")
        honey = dict(source="iran-export-honey-royaljelly", product="Honey", email="", next_action_note="bounced")
        honey1, honey2 = lead(buyer_company="Honey House", **honey), lead(buyer_company="Bee Co", **honey)
        fixed = lead(buyer_company="Fixed Co", **honey)
        zinc = lead(source="zinc", product="Zinc sulphate", buyer_company="Zinc Corp", email="",
                    next_action_note="bounced")
        managed_b = lead(owner_id=None, managed=True, seller_id=seller.id, request_id=1, product="Anchors",
                         buyer_company="Anchor Buyer Two", email="", next_action_note="bounced")
        demo_lead = lead(owner_id=demo.id, source="manual", product="Steel rebar 12mm",
                         buyer_company="Kartli Construction LLC", email="buyer@kartli.example")
        real = lead(source="research-tile", buyer_company="Real Buyer", email="real@buyer.example")
        replied = lead(source="research-tile", buyer_company="Real Buyer Two", email="two@buyer.example")
        s.add(Outreach(lead_id=replied.id, direction="in", status="received"))      # the buyer answered

        def prod(name, **kw):
            p = Product(**{**dict(name=name, hs_code="4016", origin_country="IR", category="Rubber", supplier_id=1),
                           **kw})
            s.add(p); s.commit(); s.refresh(p)
            return p
        tile = prod("Ceramic tile", unit="m2", exw_price=4.5)                # complete → never a task
        p_unpriced, p_archived = prod("Rubber tile 50mm"), prod("Pour-in-place rubber")
        p_data, p_used = prod("Cold asphalt", hs_code=""), prod("Rubber tile 30mm")
        p_assigned, p_started = prod("Rubber tile 40mm"), prod("Rubber tile 20mm")

        def quote(ld, product, **kw):
            q = Quote(lead_id=ld.id, owner_id=ld.owner_id, product_id=product.id, **kw)
            s.add(q); s.commit(); s.refresh(q)
            return q
        q_auto = quote(research, tile, status="draft", created_at=JULY)
        q_managed = quote(managed, tile, status="draft", created_at=JULY)
        q_left = quote(real, tile, status="draft")                          # approved later, still valid
        q_real = quote(real, tile, status="draft", created_at=JULY, created_by=FOUNDER)
        q_replied = quote(replied, tile, status="draft", created_at=JULY)     # auto-draft, but the buyer replied
        q_new = quote(research, tile, status="draft")                        # an auto-draft from this week
        quote(research, p_used, status="approved")                          # p_used is quoted → "in use"
        q_demo = quote(demo_lead, tile, status="sent", created_at=JULY)     # → expired at the first sync
        q_exp = quote(real, tile, status="sent", created_at=JULY)

        comp = [Company(name=f"Co {i}", tenant_id=f.id) for i in range(10)]
        for co in comp:
            s.add(co)
        s.commit()
        managed.company_id = comp[4].id
        s.add(managed)
        camp = Campaign(name="TRSHARKS anchors", tenant_id=seller.id, status="running")
        legacy_group = Campaign(name="[legacy group] honey", status="archived", inferred=True)
        s.add(camp); s.add(legacy_group); s.commit(); s.refresh(camp); s.refresh(legacy_group)
        s.add(CampaignRecipient(campaign_id=camp.id, company_id=comp[6].id, to_email="x@c6.example"))
        s.add(CampaignRecipient(campaign_id=legacy_group.id, company_id=comp[0].id, to_email="x@c0.example"))

        def pair(a, b, signals=("phone_exact",)):
            dc = DuplicateCandidate(tenant_id=f.id, left_id=comp[a].id, right_id=comp[b].id, status="open",
                                    signals=json.dumps(list(signals)), created_at=AUG)
            s.add(dc); s.commit(); s.refresh(dc)
            return dc
        dc_legacy, dc_done, dc_managed = pair(0, 1), pair(2, 3), pair(4, 5)
        dc_campaign, dc_strong = pair(6, 7), pair(8, 9, ("email_exact",))

        o_legacy = Outreach(lead_id=honey1.id, recipient="h1@honey.example", status="failed", error="550 no such user",
                            created_at=datetime(2026, 8, 15))
        o_campaign = Outreach(lead_id=research.id, recipient="tiles@kartli.example", status="failed", error="550",
                              campaign_id=camp.id, campaign_recipient_id=9, created_at=datetime(2026, 8, 20))
        o_recent = Outreach(lead_id=real.id, recipient="real@buyer.example", status="failed", error="timeout",
                            created_at=datetime(2026, 9, 20))
        for o in (o_legacy, o_campaign, o_recent):
            s.add(o)
        s.add(BounceRecord(email_normalized="h1@honey.example", lead_id=honey1.id, bounce_type="hard",
                           suppression_decision="suppressed", last_bounce_at=datetime(2026, 8, 16)))
        SUP.suppress(s, "h1@honey.example", "hard_bounce")
        opp = Opportunity(reference="OPP-1", status="new", score=30)
        s.add(opp); s.commit()

        WQ.run_all_sync(s, None); s.commit()                              # the scanners raise the usual tasks
        # pre-Phase-12 tasks the fixed scanners would no longer raise / tasks from other creators
        legacy_fail = WQ.create_work_item_safe(
            s, type="failed_system_job", title=f"Failed outreach #{o_legacy.id}", related_outreach_id=o_legacy.id,
            related_lead_id=honey1.id, idempotency_key=f"failed_job:outreach:{o_legacy.id}", condition_version="failed")
        for key in ("send_review:cs:1", "mailbox_paused:1", "campaign_render_skip:1"):
            WQ.create_work_item_safe(s, type="failed_system_job", title="system", idempotency_key=key)
        WQ.create_work_item_safe(s, type="review_inbound_reply", title="Review inbound reply",
                                 tenant_id=None, related_lead_id=research.id,
                                 idempotency_key=f"review_inbound_reply:lead:{research.id}", condition_version="reply:1")
        WQ.create_work_item_safe(s, type="unmatched_inbound", title="Unmatched inbound message",
                                 idempotency_key="unmatched_inbound:<test@founder>", condition_version="unmatched")
        manual = WQ.create_work_item(s, type="other", title="call the bank", source="manual")
        WQ.create_work_item_safe(s, type="other", title="bank reply", waiting_on="internal",
                                 idempotency_key="other:bank:1")                     # status 'waiting'
        s.commit()
        # conditions an admin settled AFTER the tasks were raised (before the fixed scanners ran again)
        p_archived.active, p_archived.status = False, "archived"
        q_left.status = "approved"
        dc_done.status = "not_duplicate"
        fixed.next_action_note = "replied"
        for row in (p_archived, q_left, dc_done, fixed):
            s.add(row)
        for wi in s.exec(select(WorkItem)).all():                          # everything looks like the Aug backlog
            wi.created_at = AUG
            s.add(wi)
        s.commit()
        assigned = _by_key(s, f"product_incomplete:product:{p_assigned.id}")[0]
        WQ.assign_item(s, assigned, f.id, f)
        WQ.start_item(s, _by_key(s, f"product_incomplete:product:{p_started.id}")[0], f)
        s.commit()
        k.update(
            founder=f.id, manual=manual.id, legacy_fail=legacy_fail.id, q_auto=q_auto.id, q_managed=q_managed.id,
            q_real=q_real.id, q_replied=q_replied.id, q_new=q_new.id, dc_legacy=dc_legacy.id,
            dc_managed=dc_managed.id, dc_campaign=dc_campaign.id,
            dc_strong=dc_strong.id,
            dismiss={"p_unpriced": f"product_incomplete:product:{p_unpriced.id}",
                     "q_auto": f"approve_quote:quote:{q_auto.id}",
                     "o_legacy": f"failed_job:outreach:{o_legacy.id}",
                     "honey1": f"replace_contact:lead:{honey1.id}", "honey2": f"replace_contact:lead:{honey2.id}",
                     "q_demo": f"quote_expired:quote:{q_demo.id}"},
            resolve={"p_archived": (f"product_incomplete:product:{p_archived.id}", "product archived"),
                     "q_left": (f"approve_quote:quote:{q_left.id}", "quote left draft"),
                     "dc_done": (f"review_dup:cand:{dc_done.id}", "candidate not_duplicate"),
                     "fixed": (f"replace_contact:lead:{fixed.id}", "contact no longer bounced")},
            manual_keys={
                "p_data": f"product_incomplete:product:{p_data.id}", "p_used": f"product_incomplete:product:{p_used.id}",
                "p_assigned": f"product_incomplete:product:{p_assigned.id}",
                "p_started": f"product_incomplete:product:{p_started.id}",
                "q_managed": f"approve_quote:quote:{q_managed.id}", "q_real": f"approve_quote:quote:{q_real.id}",
                "q_replied": f"approve_quote:quote:{q_replied.id}", "q_new": f"approve_quote:quote:{q_new.id}",
                "waiting": "other:bank:1",
                "dc_legacy": f"review_dup:cand:{dc_legacy.id}", "dc_managed": f"review_dup:cand:{dc_managed.id}",
                "dc_campaign": f"review_dup:cand:{dc_campaign.id}", "dc_strong": f"review_dup:cand:{dc_strong.id}",
                "o_campaign": f"failed_job:outreach:{o_campaign.id}", "o_recent": f"failed_job:outreach:{o_recent.id}",
                "send_review": "send_review:cs:1", "mailbox_paused": "mailbox_paused:1",
                "render_skip": "campaign_render_skip:1", "zinc": f"replace_contact:lead:{zinc.id}",
                "managed_b": f"replace_contact:lead:{managed_b.id}", "q_exp": f"quote_expired:quote:{q_exp.id}",
                "opp": f"opportunity_needs_review:opp:{opp.id}",
                "reply": f"review_inbound_reply:lead:{research.id}", "unmatched": "unmatched_inbound:<test@founder>"})
    return e, k


def _dump(e):
    cols = WorkItem.__table__.columns.keys()
    with Session(e) as s:
        return {"items": [tuple(getattr(w, c) for c in cols)
                          for w in s.exec(select(WorkItem).order_by(WorkItem.id)).all()],
                "audit": s.exec(select(func.count(AuditLog.id))).one(),
                "events": s.exec(select(func.count(QuoteStatusEvent.id))).one(),
                "quotes": [(q.id, q.status) for q in s.exec(select(Quote).order_by(Quote.id)).all()],
                "cands": [(d.id, d.status, d.reviewer, d.reviewed_at)
                          for d in s.exec(select(DuplicateCandidate).order_by(DuplicateCandidate.id)).all()]}


def _row(out, t):
    return tuple(int(x) for x in re.search(rf"^{t}\s+(\d+)\s+(\d+)\s+(\d+)\s+(\d+)$", out, re.M).groups())


def _batch(out):
    return re.search(r"batch (wqc-\d{8}-\d{6}(?:-\d+)?)", out).group(1)


def _one(s, key):
    return _by_key(s, key)[-1]


def test_dry_run_writes_nothing_and_shows_the_plan(world, capsys):
    e, k = world
    before = _dump(e)
    assert CWQ.main([]) == 0
    out = capsys.readouterr().out
    assert "DRY-RUN" in out and "rolled back" in out
    #                                       open resolve dismiss manual
    assert _row(out, "product_incomplete") == (6, 1, 1, 4)
    assert _row(out, "approve_quote") == (6, 1, 1, 4)
    assert _row(out, "review_potential_duplicate") == (5, 1, 0, 4)
    assert _row(out, "failed_system_job") == (6, 0, 1, 5)
    assert _row(out, "replace_invalid_contact") == (5, 1, 2, 2)
    assert _row(out, "quote_expired") == (2, 0, 1, 1)
    assert _row(out, "review_inbound_reply") == (1, 0, 0, 1)
    assert _row(out, "unmatched_inbound") == (1, 0, 0, 1)
    assert _row(out, "other") == (2, 0, 0, 2)
    assert _row(out, "TOTAL") == (35, 4, 6, 25)
    # the opt-in data fixes, previewed: still nothing persists
    assert CWQ.main(["--cancel-stale-drafts", "--defer-legacy-duplicates", "--actor", FOUNDER]) == 0
    assert "1 draft quote(s) cancelled, 1 duplicate pair(s) deferred" in capsys.readouterr().out
    assert _dump(e) == before


def test_apply_dismisses_and_resolves_per_rule_with_batch_tags(world, capsys):
    e, k = world
    with Session(e) as s:
        done_by_founder = s.exec(select(func.count(WorkItem.id)).where(
            WorkItem.status == "completed", WorkItem.resolved_by == k["founder"])).one()
    assert CWQ.main(["--apply", "--actor", FOUNDER]) == 0
    out = capsys.readouterr().out
    batch = _batch(out)
    tag = f"[{batch}] "
    with Session(e) as s:
        for name, key in k["dismiss"].items():
            wi = _one(s, key)
            assert wi.status == "dismissed" and wi.dismissed_reason.startswith(tag), name
            assert wi.resolved_by == k["founder"] and wi.completed_at is not None
        assert "bounce + suppression already recorded" in _one(s, k["dismiss"]["o_legacy"]).dismissed_reason
        assert "auto-drafted at lead import 2026-07-28" in _one(s, k["dismiss"]["q_auto"]).dismissed_reason
        for name, (key, note) in k["resolve"].items():
            wi = _one(s, key)
            assert (wi.status, wi.resolution_note, wi.resolved_by) == ("completed", tag + note, None), name
        for name, key in k["manual_keys"].items():                       # left exactly as they were
            wi = _one(s, key)
            assert wi.status in WQ.NONTERMINAL and not wi.dismissed_reason and not wi.resolution_note, name
        assert s.get(WorkItem, k["manual"]).status == "open"
        # condition-gone completions are nobody's work: the staff-performance input is unchanged
        assert s.exec(select(func.count(WorkItem.id)).where(
            WorkItem.status == "completed", WorkItem.resolved_by == k["founder"])).one() == done_by_founder
        rows = s.exec(select(AuditLog).where(AuditLog.action == "work_item_cleanup")).all()
        assert len(rows) == 10
        for r in rows:
            meta = json.loads(r.meta)
            assert meta["batch"] == batch and meta["prev_status"] == "open" and meta["action"] in ("dismiss", "resolve")
            assert meta["type"] == s.get(WorkItem, r.entity_id).type and meta["rule"].startswith(meta["type"] + ":")
            assert r.actor_id == k["founder"]
        # no business row moved
        assert s.get(Quote, k["q_auto"]).status == "draft"
        assert s.get(DuplicateCandidate, k["dc_legacy"]).status == "open"
    assert "approve_quote · managed buyer" in out and "review_potential_duplicate · managed buyer" in out


def test_after_apply_the_scanners_recreate_nothing(world):
    e, k = world
    assert CWQ.main(["--apply", "--actor", FOUNDER]) == 0
    with Session(e) as s:
        n = s.exec(select(func.count(WorkItem.id))).one()
        open_before = {w.id for w in s.exec(select(WorkItem).where(WorkItem.status.in_(WQ.NONTERMINAL))).all()}
        WQ.run_all_sync(s, None); s.commit()
        WQ.run_all_sync(s, None); s.commit()
        assert s.exec(select(func.count(WorkItem.id))).one() == n
        for key in k["dismiss"].values():
            assert _one(s, key).status == "dismissed"
        assert {w.id for w in s.exec(select(WorkItem).where(WorkItem.status.in_(WQ.NONTERMINAL))).all()} \
            == open_before


def test_buyer_reply_tasks_are_never_touched(world, capsys):
    e, k = world
    before = _dump(e)
    assert CWQ.main(["--only", "review_inbound_reply", "--apply", "--actor", FOUNDER]) == 2
    assert CWQ.main(["--only", "unmatched_inbound,product_incomplete"]) == 2
    assert "never cleaned up" in capsys.readouterr().out
    assert _dump(e) == before
    every = ",".join(t for t in WQ.TYPES if t not in CWQ.EXCLUDED)
    assert CWQ.main(["--only", every, "--apply", "--actor", FOUNDER, "--cancel-stale-drafts",
                     "--defer-legacy-duplicates"]) == 0
    with Session(e) as s:
        for name in ("reply", "unmatched"):
            wi = _one(s, k["manual_keys"][name])
            assert (wi.status, wi.resolution_note, wi.dismissed_reason, wi.completed_at) == ("open", "", "", None)


def test_managed_seller_and_manual_tasks_are_left_for_the_founder(world, capsys):
    e, k = world
    assert CWQ.main(["--apply", "--actor", FOUNDER, "--cancel-stale-drafts", "--defer-legacy-duplicates"]) == 0
    out = capsys.readouterr().out
    with Session(e) as s:
        for name in ("q_managed", "managed_b", "dc_managed", "dc_campaign", "dc_strong", "p_assigned", "zinc",
                     "q_real", "q_replied", "q_new", "o_campaign", "o_recent", "send_review", "mailbox_paused",
                     "render_skip", "opp"):
            wi = _one(s, k["manual_keys"][name])
            assert wi.status in WQ.NONTERMINAL and not wi.dismissed_reason, name
        assert _one(s, k["manual_keys"]["p_started"]).status == "in_progress"
        assert _one(s, k["manual_keys"]["waiting"]).status == "waiting"
        assert s.get(WorkItem, k["manual"]).status == "open"
        for name in ("q_managed", "q_real", "q_replied", "q_new"):            # only the stale auto-draft is cancelled
            assert s.get(Quote, k[name]).status == "draft", name
        for name in ("dc_managed", "dc_campaign", "dc_strong"):
            assert s.get(DuplicateCandidate, k[name]).status == "open"
    for line in ("replace_invalid_contact · seller", "failed_system_job · system failure",
                 "failed_system_job · campaign or recent", "product_incomplete · assigned",
                 "product_incomplete · not open", "other · manual task", "other · not open",
                 "quote_expired · lapsed quote", "approve_quote · real draft"):
        assert line in out, line


def test_cancel_stale_drafts_is_opt_in_and_cannot_be_reverted(world, capsys):
    e, k = world
    assert CWQ.main(["--apply", "--actor", FOUNDER, "--cancel-stale-drafts"]) == 0
    batch = _batch(capsys.readouterr().out)
    with Session(e) as s:
        assert s.get(Quote, k["q_auto"]).status == "cancelled"
        ev = s.exec(select(QuoteStatusEvent).where(QuoteStatusEvent.quote_id == k["q_auto"])).one()
        assert (ev.from_status, ev.to_status, ev.actor_id) == ("draft", "cancelled", k["founder"])
        wi = _one(s, k["dismiss"]["q_auto"])
        assert (wi.status, wi.resolution_note, wi.resolved_by) == (
            "completed", f"[{batch}] quote cancelled (stale auto-draft)", None)
        n = s.exec(select(func.count(WorkItem.id))).one()
        WQ.run_all_sync(s, None); s.commit()
        assert s.exec(select(func.count(WorkItem.id))).one() == n
    assert CWQ.main(["--revert", batch, "--apply", "--actor", FOUNDER]) == 0
    assert "its quote was cancelled" in capsys.readouterr().out
    with Session(e) as s:
        assert _one(s, k["dismiss"]["q_auto"]).status == "completed"
        assert s.get(Quote, k["q_auto"]).status == "cancelled"


def test_revert_reopens_the_batch_and_skips_a_key_with_a_newer_open_task(world, capsys):
    e, k = world
    assert CWQ.main(["--apply", "--actor", FOUNDER, "--defer-legacy-duplicates"]) == 0
    batch = _batch(capsys.readouterr().out)
    with Session(e) as s:
        dc = s.get(DuplicateCandidate, k["dc_legacy"])
        assert (dc.status, dc.reviewer) == ("deferred", FOUNDER)
        assert s.exec(select(AuditLog).where(AuditLog.action == "dup_dispose")).one().entity_id == dc.left_id
        q = s.get(Quote, k["q_auto"])                              # the auto-draft is revised → a NEW approve task
        q.version = 2
        s.add(q); s.commit()
        WQ.sync_pending_quotes(s); s.commit()
        newer = _one(s, k["dismiss"]["q_auto"]).id
    before = _dump(e)
    assert CWQ.main(["--revert", batch]) == 0                         # a dry-run revert writes nothing
    assert _dump(e) == before
    capsys.readouterr()
    assert CWQ.main(["--revert", batch, "--apply", "--actor", FOUNDER]) == 0
    out = capsys.readouterr().out
    assert re.search(r"10 task\(s\) re-opened, 1 skipped", out)
    assert "a newer open task has the same key" in out
    with Session(e) as s:
        reopened = [k["dismiss"][n] for n in k["dismiss"] if n != "q_auto"] + [v[0] for v in k["resolve"].values()]
        for key in reopened + [k["manual_keys"]["dc_legacy"]]:
            wi = _one(s, key)
            assert (wi.status, wi.completed_at, wi.resolved_by, wi.resolution_note, wi.dismissed_reason) == (
                "open", None, None, "", ""), key
        old, new = _by_key(s, k["dismiss"]["q_auto"])
        assert old.status == "dismissed" and new.id == newer and new.status == "open"
        dc = s.get(DuplicateCandidate, k["dc_legacy"])
        assert (dc.status, dc.reviewer, dc.reviewed_at) == ("open", "", None)
        assert len(s.exec(select(AuditLog).where(AuditLog.action == "work_item_cleanup_revert")).all()) == 10


def test_an_invariant_violation_rolls_everything_back(world, monkeypatch, capsys):
    e, k = world
    before = _dump(e)
    real = CWQ.apply

    def touches_a_quote(s, decisions, actor, batch):
        out = real(s, decisions, actor, batch)
        q = s.get(Quote, k["q_real"])
        q.status = "approved"                                       # a business row the cleanup must never move
        s.add(q)
        return out

    def touches_a_reply(s, decisions, actor, batch):
        out = real(s, decisions, actor, batch)
        WQ.dismiss_item(s, _one(s, k["manual_keys"]["reply"]), "oops", actor)
        return out
    for evil, msg in ((touches_a_quote, f"quote {k['q_real']} status changed"),
                      (touches_a_reply, "excluded task")):
        monkeypatch.setattr(CWQ, "apply", evil)
        assert CWQ.main(["--apply", "--actor", FOUNDER]) == 1
        out = capsys.readouterr().out
        assert "rolled back" in out and msg in out
        assert _dump(e) == before


def test_apply_needs_an_active_admin_actor(world, capsys):
    e, k = world
    before = _dump(e)
    for argv in (["--apply"], ["--apply", "--actor", "sharks@t.local"], ["--apply", "--actor", "ali@go4it.local"],
                 ["--apply", "--actor", "nobody@t.local"], ["--revert", "wqc-yesterday"],
                 ["--revert", "wqc-20261001-093000", "--only", "approve_quote"]):
        assert CWQ.main(argv) == 2, argv
    assert _dump(e) == before


def test_output_has_counts_and_ids_only(world, capsys):
    e, k = world
    CWQ.main([])
    CWQ.main(["--apply", "--actor", FOUNDER, "--defer-legacy-duplicates"])
    out = capsys.readouterr().out
    CWQ.main(["--revert", _batch(out), "--apply", "--actor", FOUNDER])
    out += capsys.readouterr().out
    assert "@" not in out
    for name in NAMES:
        assert name not in out, name
