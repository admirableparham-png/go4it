"""Phase 11 — the real TRSHARKS template renders cleanly for real buyer names, and scripts/campaign_setup.py builds a
smoke test (founder's own inboxes, inactive smoke seller) or a request-scoped real campaign."""
import pytest
from sqlalchemy import text
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine, select

from app import campaign_render as CR
from app import campaign_service as CAMP
from app import config
from app import outreach as OUT
from app.models import (Campaign, CampaignRecipient, Lead, MailAccount, ServiceRequest, User, UserProfile)
from scripts import campaign_dryrun as DRY
from scripts import campaign_setup as SETUP

TEMPLATE = "campaigns/trsharks-anchors"


@pytest.fixture
def ctx(monkeypatch):
    e = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    SQLModel.metadata.create_all(e)
    with e.connect() as c:
        for ddl in ("CREATE UNIQUE INDEX IF NOT EXISTS uq_workitem_idem_open ON workitem(idempotency_key) "
                    "WHERE idempotency_key != '' AND status IN ('open','in_progress','waiting')",
                    "CREATE UNIQUE INDEX IF NOT EXISTS uq_lead_req_anonref ON lead(request_id, anon_ref) "
                    "WHERE anon_ref != ''",
                    "CREATE UNIQUE INDEX IF NOT EXISTS uq_camprcpt_campaign_email ON campaignrecipient(campaign_id, "
                    "to_email) WHERE to_email != ''"):
            c.execute(text(ddl))
        c.commit()
    monkeypatch.setattr(SETUP, "engine", e)
    monkeypatch.setattr(SETUP, "init_db", lambda: None)
    monkeypatch.setattr(config, "IMAP_ENABLED", True)
    monkeypatch.setattr(config, "IMAP_INTERVAL", 120)
    monkeypatch.setattr(config, "IMAP_USER", "info@qmatalsaha.com")
    monkeypatch.setattr(OUT, "mail_decrypt", lambda enc: "app-password" if enc else "")
    with Session(e) as s:
        f = User(email="founder@t", name="Founder", role="admin", active=True, password_hash="x")
        sharks = User(email="trsharks@t", name="trsharks", role="agent", active=True, password_hash="x")
        s.add(f); s.add(sharks); s.commit(); s.refresh(f); s.refresh(sharks)
        s.add(UserProfile(user_id=sharks.id, account_class="seller", role_key="seller", company="TRSHARKS"))
        s.add(MailAccount(user_id=f.id, email="info@qmatalsaha.com", from_name="Qmat Alsaha", admin_owned=True,
                          active=True, smtp_password_enc="enc", sender_company="Qmat Alsaha Goods Wholesalers",
                          postal_address="Office 1, Test Tower\nDubai, United Arab Emirates"))
        sr = ServiceRequest(tracking_code="SR-202608-0001", request_type="buyer_hunt", product="Anchors",
                            status="done", owner_id=sharks.id, requester_id=sharks.id)
        s.add(sr); s.commit(); s.refresh(sr)
        for company, iso, em in (("IHL Canada (Investments Hardware Ltd.)", "CA", "sales@ihl.example"),
                                 ("Inoxa Sp. z o.o.", "PL", "biuro@inoxa.example"),
                                 ("Dani Trading LLC", "AE", "info@dani.example")):
            s.add(Lead(product="Anchors", managed=True, seller_id=sharks.id, request_id=sr.id, buyer_company=company,
                       dest_country=iso, email=em))
        s.commit()
    return e


def test_the_real_template_is_valid_and_reads_naturally(ctx):
    step, errs = SETUP.load_template(TEMPLATE)
    assert errs == [] and step["subject"] == "Metal Wall Plugs & Butterfly Anchors | Supply Enquiry"
    with Session(ctx) as s:
        c = Campaign(name="T", tenant_id=s.exec(select(User).where(User.email == "trsharks@t")).one().id,
                     request_id=1, mailbox_id=1, status="draft")
        s.add(c); s.commit(); s.refresh(c)
        CAMP.set_sequence(s, c, [step])
        st, mb = CAMP.steps_for(s, c)[0], s.get(MailAccount, 1)
        got = {}
        for ld in s.exec(select(Lead)).all():
            m = CR.render_campaign_message(s, c, st, ld, mb)
            assert m["ok"], m["error"]
            got[ld.dest_country] = m
        assert got["CA"]["text"].startswith("Hi IHL Canada team,\n\nI came across IHL Canada while reviewing")
        assert "sector in Poland," in got["PL"]["text"] and got["PL"]["text"].startswith("Hi Inoxa team,")
        assert "in the United Arab Emirates," in got["AE"]["text"] and "Hi Dani Trading team" in got["AE"]["text"]
        m = got["CA"]
        assert "$1,080.00" in m["text"] and "$1,080.00" in m["html"]            # prices in both parts
        assert "https://qmatalsaha.com/assets/brand/wordmark-dark.png" in m["html"]
        assert m["html"].index("Qmat Alsaha Goods Wholesalers") > m["html"].index("Best regards")   # footer last
        assert "attached" not in m["text"].lower()                               # no promise of an attachment
        assert len(m["html"].encode()) < 20_000


def test_smoke_setup_sends_only_to_the_founder(ctx, capsys):
    rc = SETUP.main(["--template", TEMPLATE, "--mailbox", "info@qmatalsaha.com",
                     "--smoke", "admirable.parham+1@gmail.com|IHL Canada (Investments Hardware Ltd.)|CA",
                     "--smoke", "admirable.parham+2@gmail.com|Inoxa Sp. z o.o.|PL", "--start"])
    out = capsys.readouterr().out
    assert rc == 0 and "STARTED" in out
    with Session(ctx) as s:
        c = s.exec(select(Campaign)).one()
        assert c.status == "running" and c.daily_limit == 2 and c.send_window_end == 24
        sr = s.get(ServiceRequest, c.request_id)
        assert sr.tracking_code == "SMOKE-TEST" and not s.get(User, sr.owner_id).active   # inactive smoke seller
        to = sorted(r.to_email for r in s.exec(select(CampaignRecipient)).all())
        assert to == ["admirable.parham+1@gmail.com", "admirable.parham+2@gmail.com"]   # never a real buyer
    with Session(ctx, autoflush=False) as s:
        assert DRY.check(s, s.exec(select(Campaign)).one())["errors"] == {}
    # a second run reuses the smoke seller/request/buyers and makes a fresh campaign
    assert SETUP.main(["--template", TEMPLATE, "--mailbox", "info@qmatalsaha.com",
                       "--smoke", "admirable.parham+1@gmail.com|IHL Canada (Investments Hardware Ltd.)|CA"]) == 0
    with Session(ctx) as s:
        assert len(s.exec(select(Campaign)).all()) == 2
        assert len(s.exec(select(Lead).where(Lead.source == "smoke-test")).all()) == 2


def test_real_campaign_preview_then_enrol(ctx, capsys):
    base = ["--template", TEMPLATE, "--mailbox", "info@qmatalsaha.com", "--request", "SR-202608-0001",
            "--name", "TRSHARKS anchors"]
    assert SETUP.main(base) == 0
    assert "final_eligible=3" in capsys.readouterr().out
    with Session(ctx) as s:
        c = s.exec(select(Campaign)).one()
        assert c.status == "draft" and s.exec(select(CampaignRecipient)).all() == []
        cid = c.id
    assert SETUP.main(base + ["--campaign", str(cid), "--enrol"]) == 0
    with Session(ctx) as s:
        assert len(s.exec(select(CampaignRecipient)).all()) == 3
        assert s.get(Campaign, cid).status == "draft" and s.get(Campaign, cid).send_window_end == 18


def test_refuses_a_mailbox_that_is_not_go4it_owned(ctx, capsys):
    with Session(ctx) as s:
        mb = s.get(MailAccount, 1); mb.admin_owned = False; s.add(mb); s.commit()
    assert SETUP.main(["--template", TEMPLATE, "--mailbox", "info@qmatalsaha.com", "--request", "1"]) == 2
    assert "not a connected Go4it-owned mailbox" in capsys.readouterr().out


def test_a_draft_sequence_can_be_saved_twice(ctx):
    with Session(ctx) as s:
        c = Campaign(name="D", request_id=1, mailbox_id=1, status="draft")
        s.add(c); s.commit(); s.refresh(c)
        CAMP.set_sequence(s, c, [{"subject": "A", "body": "a"}])
        CAMP.set_sequence(s, c, [{"subject": "B", "body": "b"}])
        assert [st.subject for st in CAMP.steps_for(s, c)] == ["B"]
