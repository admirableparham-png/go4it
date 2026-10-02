"""Phase 13 — each buyer gets the email in THEIR business hours (country/city time zone, their working week, no
afternoon on the last working day), best-ranked first: each UTC day's quota is kept for the best-ranked buyers whose
hours still come that day. Nothing leaves the process: a fake SMTP server records every message."""
from datetime import datetime, timedelta
from email import policy
from email.parser import BytesParser

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine, select

import app.main as main
import app.worker as worker
from app import campaign_service as CAMP
from app import config
from app import local_time as LT
from app import outreach as OUT
from app import suppression as SUP
from app.auth import hash_password
from app.models import (AuditLog, Campaign, CampaignRecipient, CampaignSend, IngestionRun, Lead, MailAccount,
                        ServiceRequest, User, UserProfile)
from scripts import campaign_setup as SETUP

HOURS = LT.parse_hours(LT.DEFAULT_HOURS)
IDX = (
    "CREATE UNIQUE INDEX IF NOT EXISTS uq_workitem_idem_open ON workitem(idempotency_key) "
    "WHERE idempotency_key != '' AND status IN ('open','in_progress','waiting')",
    "CREATE UNIQUE INDEX IF NOT EXISTS uq_outreach_campaign_send ON outreach(campaign_id,campaign_recipient_id,"
    "campaign_version,campaign_step) WHERE campaign_id IS NOT NULL",
    "CREATE UNIQUE INDEX IF NOT EXISTS uq_suppression_addr_scope ON suppression(email_normalized, scope, tenant_id) "
    "WHERE active = 1",
    "CREATE UNIQUE INDEX IF NOT EXISTS uq_campaignsend_crvs ON campaignsend(campaign_id,recipient_id,"
    "sequence_version,step_index)",
    "CREATE UNIQUE INDEX IF NOT EXISTS uq_camprcpt_campaign_lead ON campaignrecipient(campaign_id, lead_id) "
    "WHERE lead_id IS NOT NULL",
    "CREATE UNIQUE INDEX IF NOT EXISTS uq_camprcpt_campaign_email ON campaignrecipient(campaign_id, to_email) "
    "WHERE to_email != ''",
)


def _w(country, city, day):
    tz, _name, weekend = LT.buyer_zone(country, city)
    return [(s.strftime("%a %H:%M"), e.strftime("%H:%M")) for s, e in LT.windows_on_utc_day(tz, weekend, HOURS, day)]


# ------------------------------------------------------------------------------------------- the clock rules
def test_hours_are_parsed_strictly():
    assert LT.parse_hours("9:00-11:00, 14:00-16:00") == [(540, 660), (840, 960)]
    assert LT.format_hours(LT.parse_hours("14:00-16:00;09:00-11:00")) == "09:00-11:00,14:00-16:00"
    for bad in ("9-11", "11:00-09:00", "09:00-12:00,11:00-13:00", "25:00-26:00", "x", "09:00-10:00," * 5):
        assert LT.parse_hours(bad) == [], bad
    assert LT.parse_hours("") == [] and LT.parse_hours(None) == []


def test_each_country_and_multi_zone_city_gets_its_own_clock():
    assert LT.buyer_zone("PL")[1] == "Europe/Warsaw" and LT.buyer_zone("gb")[1] == "Europe/London"
    assert LT.buyer_zone("AU", "Perth, WA")[1] == "Australia/Perth"
    assert LT.buyer_zone("AU", "Brisbane QLD")[1] == "Australia/Brisbane"
    assert LT.buyer_zone("AU", "")[1] == "Australia/Sydney"
    assert LT.buyer_zone("CA", "Victoria, BC")[1] == "America/Vancouver"
    assert LT.buyer_zone("CA", "St. Jacobs, ON")[1] == "America/Toronto"
    assert LT.buyer_zone("SA")[2] == (4, 5) and LT.buyer_zone("AE")[2] == (5, 6)       # Gulf Sun–Thu; UAE Mon–Fri
    assert LT.buyer_zone("ZZ")[1] == "UTC" and LT.buyer_zone("")[1] == "UTC"


def test_windows_follow_the_local_clock_week_and_daylight_saving():
    thu, fri, sun, mon = datetime(2026, 10, 1), datetime(2026, 10, 2), datetime(2026, 10, 4), datetime(2026, 10, 5)
    assert _w("PL", "", thu) == [("Thu 07:00", "09:00"), ("Thu 12:00", "14:00")]       # 09–11 + 14–16 CEST
    assert _w("PL", "", fri) == [("Fri 07:00", "09:00")]                               # no Friday afternoon
    assert _w("PL", "", datetime(2026, 10, 26)) == [("Mon 08:00", "10:00"), ("Mon 13:00", "15:00")]   # CET now
    assert _w("PL", "", sun) == []                                                     # weekend
    assert _w("SA", "", sun) == [("Sun 06:00", "08:00"), ("Sun 11:00", "13:00")]       # the Gulf works Sunday …
    assert _w("SA", "", datetime(2026, 10, 8)) == [("Thu 06:00", "08:00")]             # … Thursday = mornings only
    assert _w("SA", "", datetime(2026, 10, 9)) == []                                   # … and Friday is weekend
    assert _w("AE", "", fri) == [("Fri 05:00", "07:00")]                               # UAE: Friday morning
    assert _w("NZ", "", sun) == [("Sun 20:00", "22:00")]                               # = Monday 09:00 in Auckland
    assert _w("NZ", "", fri) == []                                                     # = Saturday there
    assert _w("AU", "Melbourne", sun) == [("Sun 22:00", "00:00")]                      # AEDT from 4 Oct
    assert _w("AU", "Brisbane QLD", sun) == [("Sun 23:00", "01:00")]                   # Queensland: no DST
    tz, _n, wk = LT.buyer_zone("PL")
    assert LT.in_local_hours(tz, wk, HOURS, thu.replace(hour=8)) and not LT.in_local_hours(tz, wk, HOURS, thu)
    assert LT.next_window_start(tz, wk, HOURS, fri.replace(hour=10)) == mon.replace(hour=7)


# ------------------------------------------------------------------------------------------- a week of sending
class FakeSMTP:
    sent = []
    clock = None

    def __init__(self, host, port, timeout=None):
        pass

    def starttls(self, context=None):
        pass

    def login(self, user, pw):
        pass

    def send_message(self, msg):
        FakeSMTP.sent.append((FakeSMTP.clock, BytesParser(policy=policy.default).parsebytes(msg.as_bytes())))

    def quit(self):
        pass


BUYERS = [("Kiwi Fixings", "NZ", "Auckland"), ("Polish Anchors", "PL", "Krakow"), ("Gulf Supply", "SA", "Riyadh"),
          ("Dubai Hardware", "AE", "Dubai"), ("Brisbane Bolts", "AU", "Brisbane QLD"), ("London Fixings", "GB", "London"),
          ("Madrid Anclajes", "ES", "Madrid"), ("Stockholm Skruv", "SE", "Stockholm"), ("Opted Out Co", "IT", "Milano"),
          ("Wien Dübel", "AT", "Wien")]


@pytest.fixture
def world(monkeypatch):
    e = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    SQLModel.metadata.create_all(e)
    with e.connect() as c:
        for ddl in IDX:
            c.execute(text(ddl))
        c.commit()
    for mod in (worker, SETUP, main):
        monkeypatch.setattr(mod, "engine", e)
    monkeypatch.setattr(SETUP, "init_db", lambda: None)
    monkeypatch.setattr(OUT.smtplib, "SMTP", FakeSMTP)
    monkeypatch.setattr(OUT, "mail_decrypt", lambda enc: "app-password" if enc else "")
    monkeypatch.setattr(config, "IMAP_ENABLED", True)
    monkeypatch.setattr(config, "IMAP_USER", "info@qmat.example")
    monkeypatch.setattr(worker, "CAMPAIGN_SEND_MAX_PER_RUN", 1)          # prod pacing: one email per cycle
    FakeSMTP.sent = []
    with Session(e) as s:
        founder = User(email="founder@t", name="Founder", role="admin", active=True,
                       password_hash=hash_password("pw"))
        seller = User(email="trsharks@t", name="trsharks", role="agent", active=True, password_hash="x")
        s.add(founder); s.add(seller); s.commit(); s.refresh(founder); s.refresh(seller)
        s.add(UserProfile(user_id=founder.id, account_class="internal", role_key="founder", account_status="active"))
        s.add(UserProfile(user_id=seller.id, account_class="seller", role_key="seller", company="TRSHARKS"))
        mb = MailAccount(user_id=founder.id, email="info@qmat.example", from_name="Qmat Trading", admin_owned=True,
                         active=True, smtp_password_enc="enc", daily_limit=500)
        sr = ServiceRequest(tracking_code="SR-202608-0001", request_type="buyer_hunt", product="Anchors",
                            status="done", owner_id=seller.id, requester_id=seller.id)
        s.add(mb); s.add(sr); s.commit(); s.refresh(mb); s.refresh(sr)
        for i, (name, iso, city) in enumerate(BUYERS):
            s.add(Lead(product="Anchors", managed=True, owner_id=None, seller_id=seller.id, request_id=sr.id,
                       buyer_company=name, dest_country=iso, dest_city=city, email=f"buyer{i}@b{i}.example"))
        s.commit()
        c = Campaign(name="TRSHARKS anchors", tenant_id=seller.id, request_id=sr.id, owner_id=founder.id,
                     mailbox_id=mb.id, status="draft", daily_limit=3, send_window_start=8, send_window_end=18,
                     send_days="0,1,2,3,4", local_hours=LT.DEFAULT_HOURS)
        s.add(c); s.commit(); s.refresh(c)
        CAMP.set_sequence(s, c, [{"subject": "Anchors for {company}", "body": "Hello {company} team."}])
        f = {"request_id": sr.id}
        CAMP.enroll(s, c, None, f, expected=CAMP.audience_preview(s, c, f)["final_eligible"])
        assert CAMP.start_problems(s, c) == []
        CAMP.transition(s, c, "running"); s.commit()
        ids = {"campaign": c.id, "mailbox": mb.id, "seller": seller.id, "founder": founder.id}
    return e, ids


def _run(e, start, end, step=5):
    t = start
    while t < end:
        FakeSMTP.clock = t
        with Session(e) as s:                                            # the IMAP poller runs every cycle
            s.add(IngestionRun(source="email-inbound", status="ok", started_at=t, finished_at=t)); s.commit()
        res = worker.run_campaign_send(now=t)
        assert "error" not in res and res.get("errors", 0) == 0, res
        t += timedelta(minutes=step)


def _by_day(sent):
    out = {}
    for t, m in sent:
        out.setdefault(t.strftime("%a %d"), []).append((t.strftime("%H:%M"), str(m["Subject"]).split(" for ")[1]))
    return out


def test_every_buyer_gets_it_in_their_own_morning_best_ranked_first(world):
    e, ids = world
    with Session(e) as s:                                                # an opt-out never takes a slot
        SUP.suppress(s, "buyer8@b8.example", "unsubscribe"); s.commit()
    _run(e, datetime(2026, 10, 2), datetime(2026, 10, 7))               # Fri 2 → Tue 6 Oct (UTC)
    sent = FakeSMTP.sent
    for t, m in sent:                                                   # always inside the buyer's own hours
        name = str(m["Subject"]).split(" for ")[1]
        iso, city = next((iso, city) for n, iso, city in BUYERS if n.startswith(name))
        tz, _n, wk = LT.buyer_zone(iso, city)
        assert LT.in_local_hours(tz, wk, HOURS, t), (t, name)
    days = _by_day(sent)
    # Friday (UTC): the best-ranked buyers with hours that day — Brisbane's Friday morning is still open at 00:00
    assert days["Fri 02"] == [("00:00", "Brisbane Bolts"), ("05:00", "Dubai Hardware"), ("07:00", "Polish Anchors")]
    # Sunday (UTC): the Gulf's Sunday and Auckland's Monday morning (20:00 UTC) — #1 keeps its place all day
    assert days["Sun 04"] == [("06:00", "Gulf Supply"), ("20:00", "Kiwi Fixings")]
    assert "Sat 03" not in days                                          # nobody works then
    # Monday: London (#6) keeps its slot for 09:00 its time although Vienna (#10) is open first
    assert days["Mon 05"] == [("07:00", "Madrid Anclajes"), ("07:05", "Stockholm Skruv"), ("08:00", "London Fixings")]
    assert days["Tue 06"] == [("07:00", "Wien Dübel")]
    assert all(len(v) <= 3 for v in days.values())
    with Session(e) as s:
        rc = {r.to_email: r.status for r in s.exec(select(CampaignRecipient)).all()}
        assert rc["buyer8@b8.example"] == "suppressed"                     # settled, so the campaign can finish
        assert s.get(Campaign, ids["campaign"]).status == "completed"


def test_outside_the_worker_the_same_rule_answers(world):
    e, ids = world
    with Session(e) as s:
        c, mb = s.get(Campaign, ids["campaign"]), s.get(MailAccount, ids["mailbox"])
        nz, pl = (s.exec(select(CampaignRecipient).where(CampaignRecipient.to_email == f"buyer{i}@b{i}.example"))
                  .one() for i in (0, 1))
        thu7 = datetime(2026, 10, 1, 7, 0)                # 09:00 in Kraków and 10:00 in Riyadh; 20:00 in Auckland
        assert CAMP.can_send(s, c, pl, mb, thu7) == (True, "")
        assert CAMP.can_send(s, c, nz, mb, thu7) == (False, "outside the buyer's local hours")
        assert not CAMP.is_campaign_level_skip("outside the buyer's local hours")
        assert CAMP.local_plan(s, c, mb, thu7) == {nz.id: "later", pl.id: "now", pl.id + 1: "now"}   # quota 3


def test_the_warm_up_counts_a_full_day_across_a_short_weekend(world):
    e, ids = world
    with Session(e) as s:
        c = s.get(Campaign, ids["campaign"])
        c.daily_limit, c.warmup_plan = 2, "2,3"
        s.add(c); s.commit()
    _run(e, datetime(2026, 10, 2), datetime(2026, 10, 3))               # Friday: 2 of 2 — a full day
    _run(e, datetime(2026, 10, 3), datetime(2026, 10, 4, 0, 10))        # Saturday: nothing; Sunday's decision …
    with Session(e) as s:
        assert s.get(Campaign, ids["campaign"]).daily_limit == 3          # … still sees Friday's full day
        assert s.exec(select(AuditLog).where(AuditLog.action == "warmup_advance")).first() is not None


# ------------------------------------------------------------------------------------------- the controls
def test_the_campaign_page_sets_and_clears_local_hours(world):
    e, ids = world
    cl = TestClient(main.app)
    assert cl.post("/login", data={"email": "founder@t", "password": "pw"}, follow_redirects=False).status_code == 303
    cid = ids["campaign"]
    base = {"daily_limit": "3", "daily_limit_was": "3", "send_window_start": "8", "send_window_end": "18",
            "send_days": ["0", "1", "2", "3", "4"]}
    cl.post(f"/campaigns/{cid}/controls", data={**base, "local_hours": "14:00-16:00, 9:00-11:00"})
    with Session(e) as s:
        assert s.get(Campaign, cid).local_hours == "09:00-11:00,14:00-16:00"
        assert '"local_hours": "09:00-11:00,14:00-16:00"' in s.exec(
            select(AuditLog).where(AuditLog.action == "controls")).all()[-1].meta
    page = cl.post(f"/campaigns/{cid}/controls", data={**base, "local_hours": "9-11"}).text
    assert "local hours NOT changed" in page
    with Session(e) as s:
        assert s.get(Campaign, cid).local_hours == "09:00-11:00,14:00-16:00"
    cl.post(f"/campaigns/{cid}/controls", data={**base, "local_hours": ""})
    with Session(e) as s:
        assert s.get(Campaign, cid).local_hours == ""                      # back to the UTC window
    assert "buyer local hours" in cl.get(f"/campaigns/{cid}").text


def test_campaign_setup_sets_local_hours(world, capsys):
    e, ids = world
    base = ["--template", "campaigns/trsharks-anchors", "--mailbox", "info@qmat.example",
            "--request", "SR-202608-0001", "--campaign", str(ids["campaign"])]
    assert SETUP.main(base + ["--local-hours", "nonsense"]) == 2
    assert SETUP.main(base + ["--local-hours", "08:30-11:00"]) in (0, 1)
    assert "buyer-local hours 08:30-11:00" in capsys.readouterr().out
    with Session(e) as s:
        assert s.get(Campaign, ids["campaign"]).local_hours == "08:30-11:00"


def test_a_utc_window_campaign_is_unchanged(world):
    e, ids = world
    with Session(e) as s:
        c = s.get(Campaign, ids["campaign"])
        c.local_hours = ""
        s.add(c); s.commit()
    _run(e, datetime(2026, 10, 2, 7, 0), datetime(2026, 10, 2, 9, 0))
    assert [t.strftime("%H:%M") for t, _m in FakeSMTP.sent] == ["08:00", "08:05", "08:10"]   # 08–18 UTC, by rank


# ------------------------------------------------------------------------------------------- admin: set a password
def test_the_founder_sets_a_users_password_from_the_admin_panel(world):
    e, ids = world
    from app.auth import verify_password
    cl = TestClient(main.app)
    assert cl.post("/login", data={"email": "founder@t", "password": "pw"}, follow_redirects=False).status_code == 303
    page = cl.get(f"/admin/users/{ids['seller']}?tab=security").text
    assert 'type="password" name="password"' in page and 'name="confirm"' in page      # masked, typed twice
    r = cl.post(f"/admin/users/{ids['seller']}/password", data={"password": "Sharks-2026", "confirm": "Sharks-2O26"},
                follow_redirects=False)
    assert "error=" in r.headers["location"]                                         # a typo changes nothing
    with Session(e) as s:
        assert s.get(User, ids["seller"]).password_hash == "x"
    r = cl.post(f"/admin/users/{ids['seller']}/password", data={"password": "Sharks-2026", "confirm": "Sharks-2026"},
                follow_redirects=False)
    assert "tab=security&ok=Password+set" in r.headers["location"]
    with Session(e) as s:
        assert verify_password("Sharks-2026", s.get(User, ids["seller"]).password_hash)


# ------------------------------------------------------------------------------------------- review fixes
def test_a_send_that_cannot_go_out_never_takes_a_slot(world):
    e, ids = world
    thu7 = datetime(2026, 10, 1, 7, 0)
    with Session(e) as s:
        c, mb = s.get(Campaign, ids["campaign"]), s.get(MailAccount, ids["mailbox"])
        nz, pl, sa = (s.exec(select(CampaignRecipient).where(CampaignRecipient.to_email == f"buyer{i}@b{i}.example"))
                      .one() for i in (0, 1, 2))
        s.add(CampaignSend(campaign_id=c.id, recipient_id=nz.id, sequence_version=nz.sequence_version, step_index=0,
                           status="unknown_needs_review"))                      # a crash mid-send, held for review
        s.add(CampaignSend(campaign_id=c.id, recipient_id=pl.id, sequence_version=pl.sequence_version, step_index=0,
                           status="retryable", next_attempt_at=thu7 + timedelta(hours=10)))   # back-off past its hours
        s.commit()
        plan = CAMP.local_plan(s, c, mb, thu7)
        assert nz.id not in plan and pl.id not in plan and plan[sa.id] == "now" and len(plan) == 3


def test_the_window_that_closes_first_goes_first(world):
    e, ids = world
    _run(e, datetime(2026, 10, 2, 8, 0), datetime(2026, 10, 2, 8, 15))      # Friday 08:00 UTC, quota 3
    names = [str(m["Subject"]).split(" for ")[1] for _t, m in FakeSMTP.sent]
    assert names == ["Polish Anchors", "Madrid Anclajes", "London Fixings"]   # Kraków/Madrid close 09:00, London 10:00


def test_the_daily_summary_names_the_next_local_sending_day(world):
    from app import ops_summary as OS
    e, ids = world
    with Session(e) as s:
        c, mb = s.get(Campaign, ids["campaign"]), s.get(MailAccount, ids["mailbox"])
        assert OS._next_send_day(c, datetime(2026, 10, 2, 6, 0), s, mb).isoformat() == "2026-10-02"   # today
        assert OS._next_send_day(c, datetime(2026, 10, 2, 20, 0), s, mb).isoformat() == "2026-10-04"  # the Gulf's Sunday
