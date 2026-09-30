"""Phase 11 — campaign message rendering: allow-listed merge fields, fail-closed placeholders, the sender footer in
BOTH parts + the mailto List-Unsubscribe header, HTML sanitizing, the seller-identity guard (blocks, never
"[redacted]") and the internal-brand guard (buyers never see g4it/go4it). Plus the send_step wiring: a template
problem pauses the campaign without claiming or using a slot; a recipient problem skips only that buyer."""
from email import message_from_bytes, policy

import pytest
from sqlalchemy import text
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine, select

from app import campaign_render as CR
from app import campaign_service as CAMP
from app import outreach as OUT
from app.models import (Campaign, CampaignRecipient, CampaignSend, CampaignStep, Lead, MailAccount, User,
                        UserProfile, WorkItem)


@pytest.fixture
def ctx():
    e = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    SQLModel.metadata.create_all(e)
    with e.connect() as c:
        for ddl in ("CREATE UNIQUE INDEX IF NOT EXISTS uq_workitem_idem_open ON workitem(idempotency_key) "
                    "WHERE idempotency_key != '' AND status IN ('open','in_progress','waiting')",
                    "CREATE UNIQUE INDEX IF NOT EXISTS uq_campaignsend_crvs ON "
                    "campaignsend(campaign_id,recipient_id,sequence_version,step_index)"):
            c.execute(text(ddl))
        c.commit()
    with Session(e) as s:
        s.add(User(email="founder@t.local", name="Founder", role="admin", active=True, password_hash="x"))
        s.add(User(email="sharks@t.local", name="Sharkline", role="agent", active=True, password_hash="x"))
        s.commit()
        ids = {u.email.split("@")[0]: u.id for u in s.exec(select(User)).all()}
        s.add(UserProfile(user_id=ids["sharks"], account_class="seller", role_key="seller", company="TRSHARKS"))
        mb = MailAccount(user_id=ids["founder"], email="info@qmat.example", from_name="Qmat Trading",
                         admin_owned=True, active=True, daily_limit=100, sender_company="Qmat Trading LLC",
                         postal_address="Office 1, Test Tower\nDubai, UAE")
        s.add(mb); s.commit(); s.refresh(mb)
        ids["mailbox"] = mb.id
    return e, ids


def _lead(s, ids, company="Acme Hardware Ltd", iso="NL", city="Rotterdam", email="buyer@acme.example"):
    ld = Lead(product="Anchors", managed=True, seller_id=ids["sharks"], buyer_company=company, dest_country=iso,
              dest_city=city, email=email)
    s.add(ld); s.commit(); s.refresh(ld)
    return ld


def _camp(s, ids, subject="Anchors for {company}", body="Hello {company} team in {country}.", body_html=""):
    c = Campaign(name="TR anchors", tenant_id=ids["sharks"], owner_id=ids["founder"], mailbox_id=ids["mailbox"],
                 status="draft", sequence_version=1, daily_limit=100, send_days="0,1,2,3,4,5,6",
                 send_window_start=0, send_window_end=24)
    s.add(c); s.commit(); s.refresh(c)
    CAMP.set_sequence(s, c, [{"subject": subject, "body": body, "body_html": body_html}], None)
    c.status = "running"; s.add(c); s.commit(); s.refresh(c)
    return c


def _render(s, ids, c, ld):
    return CR.render_campaign_message(s, c, CAMP.steps_for(s, c)[0], ld, s.get(MailAccount, ids["mailbox"]))


# ---- merge fields -----------------------------------------------------------------------------------------------
def test_merge_fields_fill_and_country_reads_naturally(ctx):
    e, ids = ctx
    with Session(e) as s:
        m = _render(s, ids, _camp(s, ids), _lead(s, ids))
        assert m["ok"], m["error"]
        assert m["subject"] == "Anchors for Acme Hardware"           # greeting name: legal form dropped
        assert m["text"].startswith("Hello Acme Hardware team in the Netherlands.")


def test_placeholder_rules_fail_closed():
    ok = CR.validate_step("Hi {Company}", "for {country} and {city}")
    assert ok == []                                              # case-insensitive allow-list
    assert any("unknown merge field" in x for x in CR.validate_step("Hi {first_name}", "b"))
    assert any("stray" in x for x in CR.validate_step("Hi {company", "b"))
    assert any("not supported" in x for x in CR.validate_step("Hi {{company}}", "b"))
    assert any("not supported" in x for x in CR.validate_step("Hi", "Dear *|FNAME|*"))
    assert any("subject is required" in x for x in CR.validate_step("  ", "b"))
    assert any("text body is required" in x for x in CR.validate_step("S", ""))


def test_missing_city_is_a_recipient_error_not_a_blank(ctx):
    e, ids = ctx
    with Session(e) as s:
        c = _camp(s, ids, body="See you in {city}.")
        m = _render(s, ids, c, _lead(s, ids, city="Unit 5 (Industrial Park), Rotterdam"))   # an address, not a city
        assert not m["ok"] and m["scope"] == "recipient" and "{city}" in m["error"]


def test_unicode_company_survives_the_encoded_subject(ctx, monkeypatch):
    e, ids = ctx
    sent = []

    class FakeSMTP:
        def __init__(self, *a, **k): pass
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def starttls(self, **k): pass
        def login(self, *a): pass
        def send_message(self, msg): sent.append(msg.as_bytes())
    monkeypatch.setattr(OUT.smtplib, "SMTP", FakeSMTP)
    monkeypatch.setattr(OUT, "mail_decrypt", lambda _enc: "app-password")
    with Session(e) as s:
        m = _render(s, ids, _camp(s, ids), _lead(s, ids, company="Öztürk Yapı Market"))
        ok, err, _mid = OUT.send_via_account(s.get(MailAccount, ids["mailbox"]), "buyer@acme.example", m["subject"],
                                             m["text"], html=m["html"], headers={**m["headers"], "Bcc": "x@evil"})
        assert ok, err
    msg = message_from_bytes(sent[0], policy=policy.default)
    assert str(msg["Subject"]) == "Anchors for Öztürk Yapı Market"
    assert msg["List-Unsubscribe"] == "<mailto:info@qmat.example?subject=unsubscribe>"
    assert msg["Bcc"] is None                                     # only the List-Unsubscribe pair may be added


# ---- footer + header ---------------------------------------------------------------------------------------------
def test_footer_in_both_parts_and_list_unsubscribe(ctx):
    e, ids = ctx
    with Session(e) as s:
        m = _render(s, ids, _camp(s, ids), _lead(s, ids))
        for part in (m["text"], CR.html_to_text(m["html"])):
            assert "Qmat Trading LLC" in part and "Dubai, UAE" in part and CR.OPT_OUT_LINE in part
        assert "mailto:info@qmat.example?subject=unsubscribe" in m["html"]
        assert m["headers"] == {"List-Unsubscribe": "<mailto:info@qmat.example?subject=unsubscribe>"}


def test_missing_footer_details_pause_the_campaign_without_claiming(ctx):
    e, ids = ctx
    with Session(e) as s:
        mb = s.get(MailAccount, ids["mailbox"]); mb.postal_address = ""; s.add(mb); s.commit()
        c = _camp(s, ids); ld = _lead(s, ids)
        r = CampaignRecipient(campaign_id=c.id, tenant_id=ids["sharks"], lead_id=ld.id, to_email=ld.email,
                              sequence_version=1, current_step=0, status="pending")
        s.add(r); s.commit(); s.refresh(r)
        out = CAMP.send_step(s, c, r, mb, sender=lambda *a, **k: pytest.fail("must not send"))
        assert out["status"] == "render_failed" and out["scope"] == "template"
        assert s.get(Campaign, c.id).status == "paused" and "postal address" in s.get(Campaign, c.id).pause_reason
        assert s.exec(select(CampaignSend)).all() == []            # nothing claimed
        assert s.get(MailAccount, ids["mailbox"]).sent_today == 0   # no daily slot used
        assert s.exec(select(WorkItem).where(WorkItem.idempotency_key == f"campaign_paused:{c.id}")).first()


def test_recipient_error_skips_only_that_buyer(ctx):
    e, ids = ctx
    with Session(e) as s:
        c = _camp(s, ids, body="See you in {city}.")
        good, bad = _lead(s, ids), _lead(s, ids, city="", email="nocity@x.example")
        rs = []
        for ld in (bad, good):
            r = CampaignRecipient(campaign_id=c.id, tenant_id=ids["sharks"], lead_id=ld.id, to_email=ld.email,
                                  sequence_version=1, current_step=0, status="pending")
            s.add(r); s.commit(); s.refresh(r); rs.append(r)
        mb = s.get(MailAccount, ids["mailbox"])
        sender = lambda *a, **k: (True, "", "")                    # noqa: E731
        assert CAMP.send_step(s, c, rs[0], mb, sender=sender)["status"] == "render_failed"
        assert s.get(CampaignRecipient, rs[0].id).status == "skipped"
        assert CAMP.send_step(s, c, rs[1], mb, sender=sender)["status"] == "sent"
        assert s.get(Campaign, c.id).status == "running"


# ---- guards ------------------------------------------------------------------------------------------------------
def test_seller_name_blocks_instead_of_redacting(ctx):
    e, ids = ctx
    with Session(e) as s:
        m = _render(s, ids, _camp(s, ids, subject="TRSHARKS anchors for {company}"), _lead(s, ids))
        assert not m["ok"] and m["scope"] == "template" and "seller" in m["error"]
        # merged value: a buyer record that carries the seller's brand is skipped, never sent as "[redacted]"
        m = _render(s, ids, _camp(s, ids), _lead(s, ids, company="TRSHARKS Distribution"))
        assert not m["ok"] and m["scope"] == "recipient"


def test_short_seller_names_do_not_mangle_words(ctx):
    e, ids = ctx
    with Session(e) as s:
        u = s.get(User, ids["sharks"]); u.name = "Ali"; s.add(u); s.commit()
        m = _render(s, ids, _camp(s, ids, body="Top quality anchors for {company}."), _lead(s, ids))
        assert m["ok"] and "quality" in m["text"]


def test_internal_brand_never_reaches_a_buyer(ctx):
    e, ids = ctx
    assert CR.validate_step("S", "Order at https://g4it.vip/p/x")
    assert CR.validate_step("From the Go4it team", "b")
    with Session(e) as s:
        mb = s.get(MailAccount, ids["mailbox"]); mb.from_name = "go4it desk"; s.add(mb); s.commit()
        m = _render(s, ids, _camp(s, ids), _lead(s, ids))
        assert not m["ok"] and "internal platform" in m["error"]


# ---- HTML ----------------------------------------------------------------------------------------------------------
def test_sanitizer_keeps_email_layout_drops_active_content():
    raw = ('<!DOCTYPE html><html><head><style>td{color:#333}</style><script>alert(1)</script></head>'
           '<body><table width="600"><tr><td style="padding:8px" onclick="x()">Hi {company}</td></tr></table>'
           '<a href="javascript:alert(1)">bad</a><a href="https://qmat.example/catalog">ok</a>'
           '<img src="data:image/png;base64,AAAA"><img src="https://qmat.example/logo.png" alt="logo">'
           '<!--[if mso]><table><tr><td><![endif]--><iframe src="https://x"></iframe></body></html>')
    out = CR.sanitize_html(raw)
    assert "<table width=\"600\">" in out and "<style>td{color:#333}</style>" in out
    assert "script" not in out and "onclick" not in out and "iframe" not in out
    assert "javascript:" not in out and "data:image" not in out
    assert 'href="https://qmat.example/catalog"' in out and 'src="https://qmat.example/logo.png"' in out
    assert "<!--[if mso]>" in out


def test_html_design_is_merged_escaped_and_footered(ctx):
    e, ids = ctx
    html = "<html><body><p>Hello <b>{company}</b></p></body></html>"
    with Session(e) as s:
        m = _render(s, ids, _camp(s, ids, body_html=html), _lead(s, ids, company="A&B <Tools>"))
        assert m["ok"], m["error"]
        assert "<b>A&amp;B &lt;Tools&gt;</b>" in m["html"]
        assert m["html"].index("Qmat Trading LLC") < m["html"].index("</body>")
        assert "Hello A&B <Tools>" in m["text"]                   # text part derived from the design


def test_oversized_html_is_rejected():
    big = "<html><body>" + ("<p>" + "x" * 1000 + "</p>") * 95 + "</body></html>"
    assert any("90 KB" in x for x in CR.validate_step("S", "b", big))


def test_render_is_read_only(ctx):
    e, ids = ctx
    with Session(e) as s:
        c = _camp(s, ids); ld = _lead(s, ids)
        _render(s, ids, c, ld)
        assert not s.new and not s.dirty and not s.deleted


def test_html_to_text_can_drop_quoted_history():
    doc = ('<div>Please send the price list.</div><div class="gmail_quote">On Mon wrote:<blockquote>'
           + CR.OPT_OUT_LINE + '</blockquote></div>')
    assert CR.html_to_text(doc, drop_quotes=True) == "Please send the price list."
    assert "unsubscribe" in CR.html_to_text(doc)
