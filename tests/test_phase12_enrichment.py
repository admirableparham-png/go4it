"""Phase 12 — finding emails for email-less confidential buyers on their OWN websites, safely.

app/enrich_service: scrape artifacts / placeholders / function inboxes are dropped, candidates are ranked across ALL
pages, junk hosts match on a dot boundary, the User-Agent never names the platform, robots.txt is honoured, an HTTP
error is never asked twice, every lead's writes are committed before the next site is scraped, and a bounce never
re-arms an address. scripts/enrich_managed_buyers.py: scan is read-only and writes the CSV to stdout only; apply
re-checks every row in one transaction; enrol adds exactly the applied buyers, after everyone already queued.
campaign_service.audience_leads takes an exact lead_ids set. No real network anywhere: a fake opener serves pages.
"""
import csv
import hashlib
import io
import re
import urllib.error
from collections import Counter
from datetime import datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import event, text
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine, func, select

import app.main as main
import app.worker as worker
from app import campaign_service as CAMP
from app import enrich_service as ES
from app import inbound_email as IE
from app import outreach as OUT
from app import permissions as P
from app import pipeline
from app import suppression as SUP
from app.auth import hash_password
from app.models import (Activity, AuditLog, BounceRecord, Campaign, CampaignRecipient, Lead, MailAccount, Outreach,
                        SellerUpdate, ServiceRequest, StageEvent, User, UserProfile, WorkItem)
from scripts import campaign_dryrun as DRY
from scripts import enrich_managed_buyers as EMB
from scripts import migrate as MIG

SR_CODE = "SR-202608-0001"
MONDAY = datetime(2026, 10, 5, 10, 0)
IDX = (
    "CREATE UNIQUE INDEX IF NOT EXISTS uq_workitem_idem_open ON workitem(idempotency_key) "
    "WHERE idempotency_key != '' AND status IN ('open','in_progress','waiting')",
    "CREATE UNIQUE INDEX IF NOT EXISTS uq_outreach_campaign_send ON outreach(campaign_id,campaign_recipient_id,"
    "campaign_version,campaign_step) WHERE campaign_id IS NOT NULL",
    "CREATE UNIQUE INDEX IF NOT EXISTS uq_suppression_addr_scope ON suppression(email_normalized, scope, tenant_id) "
    "WHERE active = 1",
    "CREATE UNIQUE INDEX IF NOT EXISTS uq_campaignsend_crvs ON campaignsend(campaign_id,recipient_id,"
    "sequence_version,step_index)",
    "CREATE UNIQUE INDEX IF NOT EXISTS uq_lead_req_anonref ON lead(request_id, anon_ref) WHERE anon_ref != ''",
    "CREATE UNIQUE INDEX IF NOT EXISTS uq_camprcpt_campaign_lead ON campaignrecipient(campaign_id, lead_id) "
    "WHERE lead_id IS NOT NULL",
    "CREATE UNIQUE INDEX IF NOT EXISTS uq_camprcpt_campaign_email ON campaignrecipient(campaign_id, to_email) "
    "WHERE to_email != ''",
    "CREATE UNIQUE INDEX IF NOT EXISTS uq_userprofile_user ON userprofile(user_id) WHERE user_id IS NOT NULL",
)


# ------------------------------------------------------------------------------------------- a fake internet
class FakeResp:
    def __init__(self, body, url, ctype="text/html; charset=utf-8"):
        self._body, self._url, self.headers, self.status = body.encode("utf-8"), url, {"Content-Type": ctype}, 200

    def read(self, n=-1):
        return self._body if n is None or n < 0 else self._body[:n]

    def geturl(self):
        return self._url

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class FakeWeb:
    """url -> body | HTTP status | Exception | (final_url, body) for a redirect. Anything unknown is a 404."""
    def __init__(self):
        self.pages, self.calls, self.sleeps = {}, [], []

    def open(self, req, timeout=None):
        url = req.full_url
        self.calls.append((url, req.get_header("User-agent")))
        v = self.pages.get(url, 404)
        if isinstance(v, int):
            raise urllib.error.HTTPError(url, v, "error", {}, None)
        if isinstance(v, Exception):
            raise v
        if isinstance(v, tuple):
            return FakeResp(v[1], v[0])
        return FakeResp(v, url)

    def urls(self):
        return [u for u, _ in self.calls]


@pytest.fixture
def web(monkeypatch):
    w = FakeWeb()
    monkeypatch.setattr(ES, "_OPENER", w)
    monkeypatch.setattr(ES.socket, "getaddrinfo", lambda *a, **k: [(2, 1, 6, "", ("93.184.216.34", 0))])
    monkeypatch.setattr(ES.time, "sleep", lambda sec: w.sleeps.append(sec))
    DRY.lock_network(patch=monkeypatch.setattr)          # belt and braces: no real socket can open in these tests
    return w


# ------------------------------------------------------------------------------------------- extraction
def test_scrape_artifacts_are_normalised_to_the_real_address():
    assert ES._emails_from('<a href="mailto:%20info@acme.com">mail</a>', "acme.com") == ["info@acme.com"]
    assert ES._emails_from('{"html":"\\u003cb\\u003einfo@acme.com\\u003c/b\\u003e"}', "acme.com") == ["info@acme.com"]
    assert ES._emails_from('<a href="mailto:&#105;nfo&#64;acme.com">write</a>', "acme.com") == ["info@acme.com"]
    assert ES._emails_from("mailto:info%40acme.com", "acme.com") == ["info@acme.com"]
    assert ES._emails_from("Write to Sales@Acme.co.uk.We answer fast", "acme.co.uk") == ["sales@acme.co.uk"]
    assert ES._emails_from("our AU arm: info@acme.com.au", "acme.com") == ["info@acme.com.au"]   # a real sibling
    key = 0x42
    cf = f"{key:02x}" + "".join(f"{ord(ch) ^ key:02x}" for ch in "sales@acme.com")
    assert ES._emails_from(f'<span class="__cf_email__" data-cfemail="{cf}">[email&#160;protected]</span>',
                           "acme.com") == ["sales@acme.com"]


@pytest.mark.parametrize("text_", [
    "a info@acme.com b", "x@a.com@b.com", "mailto:sales@acme.co.uk?subject=hi", "first.last+tag@sub.acme-x.pl.",
    "@nobody here", "trailing@", "two: a@b.cd, c@d.ef;", "émile@acme.fr", "line\n@acme.com", "x" * 70 + "@acme.com"])
def test_anchored_extraction_finds_what_the_regex_finds(text_):
    expect = [h for h in ES.EMAIL_RE.findall(text_) if len(h.partition("@")[0]) <= 64]
    got = ES._email_hits(text_)
    assert got == expect or (text_.startswith("x" * 70) and got == ["x" * 64 + "@acme.com"])


def test_extraction_stays_fast_on_a_huge_unbroken_run():
    import time
    page = "a.b" * 100_000 + " [at] " + "z" * 50_000 + " info@acme.com"
    t0 = time.time()
    assert ES._emails_from(page, "acme.com") == ["info@acme.com"]
    assert time.time() - t0 < 2                       # the old unanchored regexes needed minutes here


def test_obfuscated_addresses_are_found_but_kept_apart():
    plain, hidden = ES._page_emails("Write to info [at] acme [dot] com or sales(at)acme.com", ("acme.com",))
    assert plain == [] and hidden == ["info@acme.com", "sales@acme.com"]


@pytest.mark.parametrize("addr", [
    "webmaster@acme.com", "postmaster@acme.com", "privacy@acme.com", "gdpr@acme.com", "datenschutz@acme.de",
    "rodo@acme.pl", "iod@acme.pl", "jobs@acme.com", "careers@acme.com", "kariera@acme.pl", "hr@acme.com",
    "hr.uk@acme.com", "press@acme.com", "media@acme.com", "newsletter@acme.com", "noreply@acme.com",
    "no-reply@acme.com", "donotreply@acme.com", "do-not-reply@acme.com", "invoices@acme.com", "faktury@acme.pl",
    "billing@acme.com", "accounts@acme.com"])
def test_function_inboxes_are_dropped_not_down_ranked(addr):
    assert ES._emails_from(f"write to {addr} today", addr.split("@")[1]) == []


@pytest.mark.parametrize("addr", ["info@yourcompany.com", "john@doe.com", "name@acme.com", "user@acme.com",
                                  "your@acme.com", "info@company.com", "sales@test.com", "you@example.org",
                                  "jane.doe@acme.com", "info@domain.com"])
def test_template_placeholders_are_dropped(addr):
    assert ES._emails_from(f"e.g. {addr}", "acme.com") == []


def test_real_addresses_that_merely_resemble_noise_survive():
    text = "hrvoje@acme.hr mediamarkt@acme.de info@acmecompany.com info@contest.com pressure@acme.com"
    assert set(ES._emails_from(text, "acme.hr")) == {"hrvoje@acme.hr", "mediamarkt@acme.de", "info@acmecompany.com",
                                                    "info@contest.com", "pressure@acme.com"}


def test_admin_is_no_longer_a_preferred_role():
    assert ES._emails_from("admin@acme.com or info@acme.com", "acme.com")[0] == "info@acme.com"


# ------------------------------------------------------------------------------------------- clean_site
@pytest.mark.parametrize("site", ["fedex.com", "inox.com", "apex.com", "https://www.intex.com.au/", "kraft.metals.ca",
                                  "ottawa.metalsupply.ca", "visimpex.com", "https://www.hispanox.com", "nestinox.com",
                                  "https://timberfix.com.au", "amazonas-steel.com.br", "https://shop.berner.eu/es-es"])
def test_clean_site_keeps_real_sites_that_merely_contain_a_junk_host(site):
    assert ES.clean_site(site)


@pytest.mark.parametrize("site", [
    "x.com/acme", "https://twitter.com/acme", "t.me/acme", "wa.me/9715551234", "facebook.com/acme",
    "https://sub.facebook.com/x", "m.facebook.de/acme", "https://www.linkedin.com/company/acme",
    "https://www.europages.co.uk/ACME-LTD/00000004-1.html", "https://pl.kompass.com/c/acme/pl123/",
    "acme.en.ec21.com", "https://www.tradekey.com/company/acme.htm", "https://dir.indiamart.com/impcat/anchors.html",
    "https://sites.google.com/view/acme", "https://www.google.com/maps/place/Acme", "maps.google.com/?cid=1",
    "https://maps.app.goo.gl/abc", "https://www.amazon.de/stores/acme", "https://www.ebay.co.uk/str/acme",
    "https://www.dnb.com/business-directory/company-profiles.acme.html", "https://yellowpages-uae.com/acme"])
def test_clean_site_rejects_social_directory_and_marketplace_hosts(site):
    assert ES.clean_site(site) == ""


# ------------------------------------------------------------------------------------------- crawling
def test_user_agent_never_names_the_platform(web):
    assert not re.search(r"go4it|g4it", ES.UA, re.I)
    web.pages["https://acme.com/"] = "<p>info@acme.com</p>"
    ES.scrape_site("https://acme.com")
    assert web.calls and all(ua == ES.UA for _, ua in web.calls)


def test_ranking_is_across_pages_so_an_agency_on_the_homepage_never_wins(web):
    web.pages.update({
        "https://acme.ge/": '<footer>site by hello@webagency.com</footer><a href="/pagina-7">Contatti</a>',
        "https://acme.ge/pagina-7": "<p>Ufficio: info@acme.ge</p>"})
    r = ES.scrape_site("https://acme.ge", max_pages=5)
    assert r["email"] == "info@acme.ge" and r["emails"] == ["info@acme.ge", "hello@webagency.com"]
    assert r["email_pages"] == {"info@acme.ge": "https://acme.ge/pagina-7", "hello@webagency.com": "https://acme.ge/"}
    # the homepage's own 'Contatti' link is followed first, and the crawl stops at the same-domain role mailbox
    assert web.urls() == ["https://acme.ge/robots.txt", "https://acme.ge/", "https://acme.ge/pagina-7"]
    assert r["final_url"] == "https://acme.ge/" and r["pages"] == 2


def test_noreply_only_site_yields_no_email(web):
    web.pages["https://acme.com/"] = "<p>Sent by noreply@acme.com</p>"
    r = ES.scrape_site("https://acme.com", max_pages=1)
    assert r["email"] == "" and r["emails"] == []


def test_http_errors_are_never_asked_twice_and_a_site_gets_a_bounded_number_of_requests(web):
    web.pages["https://acme.com/"] = "<p>Welcome</p>"            # no address, no links; every guessed path 404s
    r = ES.scrape_site("https://acme.com", max_pages=5)
    assert len(web.urls()) == len(set(web.urls()))                # nothing requested twice
    assert web.urls()[0] == "https://acme.com/robots.txt" and len(web.urls()) == 1 + ES.MAX_FETCHES
    assert r["pages"] == 1 and r["email"] == "" and r["fetches"] == len(web.urls())


def test_fetch_retries_a_network_error_once_but_never_a_4xx(web):
    web.pages["https://acme.com/a"] = 404
    web.pages["https://acme.com/b"] = urllib.error.URLError("timed out")
    assert ES._fetch_page("https://acme.com/a") == ("", "", "http-404")
    assert ES._fetch_page("https://acme.com/b") == ("", "", "network")
    assert Counter(web.urls()) == {"https://acme.com/a": 1, "https://acme.com/b": 2}


def test_robots_txt_is_honoured(web):
    web.pages.update({"https://acme.com/robots.txt": "User-agent: *\nDisallow: /contact\n",
                      "https://acme.com/": '<a href="/contact">Contact</a> <a href="/kontakt">Kontakt</a>',
                      "https://acme.com/contact": "info@acme.com", "https://acme.com/kontakt": "sales@acme.com"})
    r = ES.scrape_site("https://acme.com", max_pages=5)
    assert "https://acme.com/contact" not in web.urls() and r["email"] == "sales@acme.com"


@pytest.mark.parametrize("robots,blocked", [
    ("User-agent: contact-check\nDisallow: /\n", "robots.txt"),
    (403, "robots.txt"),
    (503, "robots.txt unreadable (http-503)"),
    ("User-agent: *\nCrawl-delay: 120\n", "robots.txt crawl-delay")])
def test_robots_refusal_means_nothing_else_is_fetched(web, robots, blocked):
    web.pages.update({"https://acme.com/robots.txt": robots, "https://acme.com/": "info@acme.com"})
    r = ES.scrape_site("https://acme.com")
    assert web.urls() == ["https://acme.com/robots.txt"] and r["blocked"] == blocked and r["email"] == ""


def test_crawl_delay_is_respected(web):
    web.pages.update({"https://acme.com/robots.txt": "User-agent: *\nCrawl-delay: 3\n",
                      "https://acme.com/": "<p>hi</p>", "https://acme.com/contact": "biuro@acme.com"})
    assert ES.scrape_site("https://acme.com", pause=0.2)["email"] == "biuro@acme.com"
    assert web.sleeps and min(web.sleeps) >= 3


def test_a_bare_host_is_tried_on_https_then_http(web):
    web.pages.update({"https://acme.pl/robots.txt": urllib.error.URLError("refused"),
                      "http://acme.pl/": "<p>biuro@acme.pl</p>"})
    r = ES.scrape_site("acme.pl")
    assert r["email"] == "biuro@acme.pl" and r["final_url"] == "http://acme.pl/"


def test_pages_are_decoded_in_their_declared_charset():
    body = "<title>Hurtownia Śruba</title>".encode("cp1250")
    assert "Śruba" in ES._decode(body, "text/html; charset=windows-1250")
    assert "Śruba" in ES._decode(b'<meta charset="windows-1250">' + body)
    assert ES._decode(b"plain", "text/html; charset=no-such-charset") == "plain"


def test_redirects_and_parked_pages_are_reported(web):
    web.pages["https://old-acme.com/"] = ("https://acme-group.com/", "<title>ACME Group</title> info@acme-group.com")
    r = ES.scrape_site("https://old-acme.com", max_pages=1)
    assert r["final_url"] == "https://acme-group.com/" and r["email"] == "info@acme-group.com"
    assert r["title"] == "ACME Group" and not r["parked"]
    web.pages["https://gone.com/"] = "<h1>This domain is for sale!</h1> Contact sales@gone.com"
    assert ES.scrape_site("https://gone.com", max_pages=1)["parked"]


# ------------------------------------------------------------------------------------------- the old callers
def test_generic_enrichment_never_touches_confidential_managed_buyers():
    e = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    SQLModel.metadata.create_all(e)
    with Session(e) as s:
        s.add(Lead(product="x", website="https://plain.example"))
        s.add(Lead(product="x", website="https://managed.example", managed=True))
        s.commit()
        assert [ld.website for ld in ES.candidate_leads(s)] == ["https://plain.example"]
        assert len(ES.candidate_leads(s, include_managed=True)) == 2


def test_run_web_enrichment_commits_every_lead_before_the_next_scrape(monkeypatch):
    e = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    SQLModel.metadata.create_all(e)
    with Session(e) as s:
        for i in range(4):
            s.add(Lead(product="x", website=f"https://b{i}.example", buyer_company=f"B{i}"))
        s.commit()
    trace = []
    event.listen(e, "before_cursor_execute", lambda conn, cur, st, *a: trace.append(st.split()[0].upper()))
    event.listen(e, "commit", lambda conn: trace.append("COMMIT"))

    def fake_scrape(website, max_pages=3, pause=0.3, **kw):
        trace.append("SCRAPE")
        hit = website == "https://b3.example"          # the newest lead (scraped first) is the only hit
        return {"site": website, "email": "info@b3.example" if hit else "", "phone": "", "emails": []}
    monkeypatch.setattr(ES, "scrape_site", fake_scrape)
    with Session(e) as s:
        assert ES.run_web_enrichment(s, apply=True, log=lambda *a: None)["enriched"] == 1
    assert trace.count("SCRAPE") == 4
    pending = False
    for t in trace:                                    # no write may still be open when a site is being fetched
        if t in ("INSERT", "UPDATE", "DELETE"):
            pending = True
        elif t == "COMMIT":
            pending = False
        elif t == "SCRAPE":
            assert not pending, trace
    with Session(e) as s:
        assert len(s.exec(select(Activity).where(Activity.kind == "enrichment")).all()) == 4


@pytest.fixture
def bounce_world(monkeypatch):
    e = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    SQLModel.metadata.create_all(e)
    with Session(e) as s:
        seller = User(email="s@t", name="s", role="agent", active=True, password_hash="x")
        s.add(seller); s.commit(); s.refresh(seller)
        ld = Lead(product="Anchors", managed=True, seller_id=seller.id, buyer_company="Acme",
                  email="old@acme.example", website="https://acme.example", pipeline_stage="contacted")
        s.add(ld); s.commit(); s.refresh(ld)
        s.add(Outreach(lead_id=ld.id, direction="out", channel="email", recipient=ld.email, status="sent"))
        s.commit()
        lid = ld.id
    alerts = []
    monkeypatch.setattr(IE, "notify_bounce", lambda lead, email, reason, new_email="": alerts.append(new_email))
    monkeypatch.setattr(IE, "send_message", lambda *a, **k: None)
    return e, lid, alerts


@pytest.mark.parametrize("site_lists,expect", [
    (["sales@acme.example"], "sales@acme.example"),          # a new address: proposed for review
    (["old@acme.example"], ""),                              # the site still shows the bounced one: nothing
    (["spam@acme.example"], "")])                            # a suppressed one: nothing
def test_a_bounce_never_rearms_an_address_and_hands_the_candidate_to_review(bounce_world, monkeypatch,
                                                                          site_lists, expect):
    e, lid, alerts = bounce_world
    with Session(e) as s:
        SUP.suppress(s, "spam@acme.example", "unsubscribe")
        s.commit()
    monkeypatch.setattr(ES, "scrape_site", lambda website, **kw: {
        "site": "https://acme.example", "email": site_lists[0], "emails": site_lists, "phone": ""})
    with Session(e) as s:
        assert IE.handle_bounce(s, "old@acme.example", "550 5.1.1 user unknown") == "bounced"
    assert alerts == [expect]
    with Session(e) as s:
        ld = s.get(Lead, lid)
        assert ld.email == ""                                # neither the bounced nor a found address is written
        acts = s.exec(select(Activity).where(Activity.lead_id == lid, Activity.kind == "enrichment")).all()
        assert len(acts) == 1
        if expect:
            assert expect in acts[0].body and "not applied" in acts[0].body
        else:
            assert "no new address found" in acts[0].body
        assert SUP.is_suppressed(s, "old@acme.example")      # the hard bounce itself is still handled


# ------------------------------------------------------------------------------------------- classification
def _ctx(**kw):
    base = {"lead_emails": set(), "recipient_emails": set(), "suppressed": set(), "bounced": set(),
            "taken_domains": set(), "found": Counter()}
    base.update(kw)
    return base


def _scraped(emails=(), site="https://acme-steel.pl", final="", title="Acme Steel", parked=False, obf=(),
             blocked=""):
    base = ES.clean_site(site)
    return {"site": base, "final_url": final or (base + "/" if base else ""), "emails": list(emails),
            "email": emails[0] if emails else "", "email_pages": {e: base + "/kontakt" for e in emails},
            "obfuscated": list(obf), "title": title, "site_name": "", "parked": parked, "blocked": blocked,
            "phone": ""}


A = "info@acme-steel.pl"
CASES = [
    ("accept", "", {}, {}, {"emails": [A]}),
    ("accept", "", {}, _ctx(suppressed={A}), {"emails": [A, "sales@acme-steel.pl"]}),   # next-best address
    ("review", "personal-looking", {}, {}, {"emails": ["jan.kowalski@acme-steel.pl"]}),
    ("review", "free-mail", {}, {}, {"emails": ["acmesteel@gmail.com"]}),
    ("review", "redirects", {}, {}, {"emails": ["info@acme-group.pl"], "final": "https://acme-group.pl/"}),
    ("review", "does not clearly match", {"company": "Totally Different Trading"}, {},
     {"emails": [A], "title": "Home"}),
    ("review", "support", {}, {}, {"emails": ["support@acme-steel.pl"]}),
    ("review", "already has an address", {}, _ctx(taken_domains={"acme-steel.pl"}), {"emails": [A]}),
    ("review", "obfuscated", {}, {}, {"emails": [], "obf": [A]}),
    ("reject", "another domain", {}, {}, {"emails": ["hello@webagency.ie"]}),
    ("reject", "parked", {}, {}, {"emails": [A], "parked": True}),
    ("reject", "loader blanked", {"notes": "match 80 | same email as another buyer (kept on the first)"}, {},
     {"emails": [A]}),
    ("reject", "another buyer record", {}, _ctx(lead_emails={A}), {"emails": [A]}),
    ("reject", "do-not-contact", {}, _ctx(suppressed={A}), {"emails": [A]}),
    ("reject", "bounced before", {}, _ctx(bounced={A}), {"emails": [A]}),
    ("reject", "campaign recipient", {}, _ctx(recipient_emails={A}), {"emails": [A]}),
    ("reject", "for 2 buyers", {}, _ctx(found=Counter({A: 2})), {"emails": [A]}),
    ("reject", "website unusable", {}, {}, {"emails": [A], "site": "https://facebook.com/acme"}),
    ("reject", "no email found (robots.txt)", {}, {}, {"blocked": "robots.txt"}),
]


@pytest.mark.parametrize("decision,reason,cand,ctx,scraped", CASES)
def test_classification(decision, reason, cand, ctx, scraped):
    c = {"id": 1, "company": "Acme Steel Sp. z o.o.", "notes": "", **cand}
    k = EMB.classify(c, _scraped(**scraped), ctx or _ctx())
    assert k["decision"] == decision, k
    assert reason in k["reasons"], k
    if decision == "accept":
        assert k["email"].endswith("@acme-steel.pl") and k["found_on"] and k["name_score"] >= EMB.NAME_OK


def test_name_score_is_strict_about_shared_trade_words():
    assert EMB.name_score("Würth Polska Sp. z o.o.", "wurth.pl") == 100
    assert EMB.name_score("Acme Steel Ltd", "acme-steel.co.uk") == 100
    assert EMB.name_score("Kowalski Hurtownia", "hk-hurt.pl", title="Kowalski Hurtownia Budowlana") >= EMB.NAME_OK
    assert EMB.name_score("Acme Steel Ltd", "steelpro.com", title="SteelPro — steel supplies") < EMB.NAME_OK
    assert EMB.name_score("Albert Berner Deutschland GmbH", "berner.eu") == 100      # a distinctive later word
    assert EMB.name_score("Ferdinand Gross GmbH", "schrauben-gross.com") == 100
    assert EMB.name_score("Otto Olsen AS", "oo.no") == 100                            # initials
    assert EMB.name_score("Acme Steel Ltd", "steel.pl") < EMB.NAME_OK                 # a trade word is not a name
    assert EMB.name_score("Steel Masters Ltd", "steel.co.uk") < EMB.NAME_OK


# ------------------------------------------------------------------------------------------- the script
class FakeSMTP:
    sent = []

    def __init__(self, host, port, timeout=None):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def starttls(self, context=None):
        pass

    def login(self, user, pw):
        pass

    def send_message(self, msg):
        FakeSMTP.sent.append(msg["To"])

    def quit(self):
        pass


@pytest.fixture
def world(monkeypatch, tmp_path):
    """A file database (so scan's real read-only engine can open it) with request SR-202608-0001: three emailed
    buyers already queued in a RUNNING campaign, six email-less buyers with websites, one enrolled-then-bounced buyer
    and another seller's request."""
    db = tmp_path / "go4it.db"
    e = create_engine(f"sqlite:///{db}", connect_args={"check_same_thread": False})
    SQLModel.metadata.create_all(e)
    with e.connect() as c:
        for ddl in IDX:
            c.execute(text(ddl))
        c.commit()
    for mod in (EMB, main, worker):
        monkeypatch.setattr(mod, "engine", e)
    monkeypatch.setattr(MIG, "_db_path", lambda: str(db))
    monkeypatch.setattr(EMB, "lock_network", lambda: DRY.lock_network(patch=monkeypatch.setattr))
    monkeypatch.setattr(OUT.smtplib, "SMTP", FakeSMTP)
    monkeypatch.setattr(OUT, "mail_decrypt", lambda enc: "app-password" if enc else "")
    DRY.NET_ATTEMPTS.clear()
    FakeSMTP.sent = []
    ids = {"db": db}
    with Session(e) as s:
        founder = User(email="founder@t", name="Founder", role="admin", active=True, password_hash="x")
        seller = User(email="sharks@t", name="sharks", role="agent", active=True, password_hash=hash_password("pw"))
        other = User(email="other@t", name="other", role="agent", active=True, password_hash="x")
        s.add_all([founder, seller, other])
        s.commit()
        for u in (founder, seller, other):
            s.refresh(u)
        s.add(UserProfile(user_id=seller.id, account_class="seller", role_key="seller", company="TRSHARKS",
                          scope=P.ROLE_TEMPLATES["seller"]["scope"], account_status="active"))
        sr = ServiceRequest(tracking_code=SR_CODE, request_type="buyer_hunt", product="Drywall anchors",
                            status="done", owner_id=seller.id, requester_id=seller.id)
        sr2 = ServiceRequest(tracking_code="SR-202609-0002", request_type="buyer_hunt", product="Zinc", status="done",
                             owner_id=other.id, requester_id=other.id)
        mb = MailAccount(user_id=founder.id, email="info@qmat.example", from_name="Qmat Trading", admin_owned=True,
                         active=True, smtp_password_enc="enc", daily_limit=200, sender_company="Qmat Trading LLC",
                         postal_address="Office 1, Test Tower\nDubai, UAE")
        s.add_all([sr, sr2, mb])
        s.commit()
        for x in (sr, sr2, mb):
            s.refresh(x)

        def buyer(key, company, iso, email="", website="", request=sr, notes="", seller_id=seller.id):
            ld = Lead(source=f"req-{request.id}", external_id=f"req-{request.id}:{key}:{iso}",
                      product="Drywall anchors", managed=True, owner_id=None, seller_id=seller_id, request_id=request.id, status="new",
                      pipeline_stage="identified", buyer_company=company, dest_country=iso, email=email,
                      website=website, notes=notes)
            s.add(ld)
            s.flush()
            ld.anon_ref = f"Buyer-{iso}-{ld.id:03d}"
            s.add(StageEvent(lead_id=ld.id, request_id=request.id, from_stage="", to_stage="identified"))
            ids[key] = ld.id
            return ld
        old = [buyer(f"old{i}", f"Old Buyer {i}", "PL", email=f"old{i}@old{i}.pl",
                     website=f"https://old{i}.pl") for i in range(3)]
        buyer("acme", "Acme Steel Sp. z o.o.", "PL", website="https://acme-steel.pl")
        buyer("beta", "Beta Hardware Ltd", "GB", website="https://www.beta-hardware.co.uk/en/")
        buyer("gamma", "Gamma Fixings", "IE", website="gamma-fixings.ie")
        buyer("delta", "Delta Tools", "ZA", website="https://facebook.com/deltatools")
        buyer("eps", "Epsilon Bolts", "AU", website="https://epsilon-bolts.com.au",
              notes="match 80 | potential | anchors | same email as another buyer (kept on the first)")
        buyer("zeta", "Zeta Anchors", "NZ", website="https://zeta-anchors.co.nz")
        gone = buyer("gone", "Bounced Co", "CH", website="https://bounced.ch")
        buyer("foreign", "Other Request Co", "DK", website="https://foreign.dk", request=sr2, seller_id=other.id)
        s.commit()
        c = Campaign(name="TRSHARKS anchors — wave 1", tenant_id=seller.id, request_id=sr.id, owner_id=founder.id,
                     mailbox_id=mb.id, status="draft", daily_limit=3, send_window_start=0, send_window_end=24,
                     send_days="0,1,2,3,4,5,6")
        s.add(c)
        s.commit()
        s.refresh(c)
        CAMP.set_sequence(s, c, [{"subject": "Drywall anchors for {company}",
                                  "body": "Hello {company} team, we supply drywall anchors to {country}."}])
        c.status = "running"
        s.add(c)
        s.commit()
        for ld in old:
            s.add(CampaignRecipient(campaign_id=c.id, tenant_id=seller.id, lead_id=ld.id, request_id=sr.id,
                                    to_email=ld.email, sequence_version=1))
        s.add(CampaignRecipient(campaign_id=c.id, tenant_id=seller.id, lead_id=gone.id, request_id=sr.id,
                                to_email="gone@bounced.ch", status="hard_bounced"))
        SUP.suppress(s, "info@zeta-anchors.co.nz", "unsubscribe")
        s.commit()
        ids.update(seller=seller.id, other=other.id, request=sr.id, request2=sr2.id, campaign=c.id, mailbox=mb.id)
    return e, ids


SITES = {
    "https://acme-steel.pl/": '<title>Acme Steel – kotwy</title><a href="/kontakt">Kontakt</a>',
    "https://acme-steel.pl/kontakt": "<p>Biuro: info@acme-steel.pl</p>",
    "https://www.beta-hardware.co.uk/": "<p>Ask Jan: jan.kowalski@beta-hardware.co.uk</p>",
    "https://gamma-fixings.ie/": "<p>Website by hello@webagency.ie</p>",
    "https://epsilon-bolts.com.au/": "<p>info@epsilon-bolts.com.au</p>",
    "https://zeta-anchors.co.nz/": "<p>info@zeta-anchors.co.nz</p>",
}


def _scan(monkeypatch, capsys, *args):
    rc = EMB.main(["scan", "--request", SR_CODE, *args])
    cap = capsys.readouterr()
    return rc, cap.out, cap.err


def _rows(out):
    return list(csv.DictReader(io.StringIO(out)))


def _csv(rows):
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=EMB.COLS, lineterminator="\n")
    w.writeheader()
    w.writerows(rows)
    return buf.getvalue()


def _apply(monkeypatch, csv_text, *args):
    monkeypatch.setattr("sys.stdin", io.StringIO(csv_text))
    return EMB.main(["apply", "--request", SR_CODE, "--stdin", *args])


def test_scan_is_read_only_and_writes_the_csv_to_stdout_only(world, web, monkeypatch, capsys, tmp_path):
    e, ids = world
    web.pages.update(SITES)
    stmts = []
    real = EMB.readonly_engine

    def traced():
        eng = real()
        assert "mode=ro" in str(eng.url)
        event.listen(eng, "before_cursor_execute", lambda conn, cur, st, *a: stmts.append(st))
        return eng
    monkeypatch.setattr(EMB, "readonly_engine", traced)
    before = hashlib.sha256(ids["db"].read_bytes()).hexdigest()
    cwd = tmp_path / "cwd"
    cwd.mkdir()
    monkeypatch.chdir(cwd)
    rc, out, err = _scan(monkeypatch, capsys, "--expect-candidates", "6")
    assert rc == 0
    assert stmts and not [st for st in stmts if st.split()[0].upper() in ("INSERT", "UPDATE", "DELETE")]
    assert hashlib.sha256(ids["db"].read_bytes()).hexdigest() == before
    assert list(cwd.iterdir()) == []                          # no file written anywhere
    assert "@" not in err and "accept 1 · review 1 · reject 4" in err
    rows = {int(r["lead_id"]): r for r in _rows(out)}
    assert set(rows) == {ids[k] for k in ("acme", "beta", "gamma", "delta", "eps", "zeta")}  # never gone/old/foreign
    assert tuple(_rows(out)[0].keys()) == EMB.COLS
    got = {k: (rows[ids[k]]["decision"], rows[ids[k]]["email"]) for k in ("acme", "beta", "gamma", "eps", "zeta")}
    assert got == {"acme": ("accept", "info@acme-steel.pl"),
                   "beta": ("review", "jan.kowalski@beta-hardware.co.uk"),
                   "gamma": ("reject", "hello@webagency.ie"),
                   "eps": ("reject", "info@epsilon-bolts.com.au"),
                   "zeta": ("reject", "info@zeta-anchors.co.nz")}
    assert rows[ids["delta"]]["decision"] == "reject" and "unusable" in rows[ids["delta"]]["reasons"]
    assert rows[ids["acme"]]["found_on"] == "https://acme-steel.pl/kontakt"
    assert not any("facebook" in u or "bounced" in u or "foreign" in u for u in web.urls())
    assert all(ua == ES.UA for _, ua in web.calls)


def test_scan_refuses_on_an_unexpected_candidate_count_and_fetches_nothing(world, web, monkeypatch, capsys):
    rc, out, err = _scan(monkeypatch, capsys, "--expect-candidates", "462")
    assert rc == 2 and out == "" and "REFUSED" in err and web.calls == []


def test_scan_refuses_a_request_with_seller_owned_buyers(world, web, monkeypatch, capsys):
    e, ids = world
    with Session(e) as s:
        s.add(Lead(product="x", owner_id=ids["seller"], request_id=ids["request"], buyer_company="Legacy"))
        s.commit()
    rc, out, err = _scan(monkeypatch, capsys)
    assert rc == 2 and "not confidential managed buyers" in err and web.calls == []


def test_scan_offset_and_limit_take_stable_chunks(world, web, monkeypatch, capsys):
    web.pages.update(SITES)
    first = [r["lead_id"] for r in _rows(_scan(monkeypatch, capsys, "--offset", "0", "--limit", "4")[1])]
    rest = [r["lead_id"] for r in _rows(_scan(monkeypatch, capsys, "--offset", "4", "--limit", "4")[1])]
    assert len(first) == 4 and len(rest) == 2 and not set(first) & set(rest)
    assert first + rest == sorted(first + rest, key=int)


def test_full_flow_scan_apply_enrol_then_the_worker_sends_the_old_queue_first(world, web, monkeypatch, capsys):
    e, ids = world
    web.pages.update(SITES)
    web.pages["https://acme-steel.pl/kontakt"] += ' <a href="tel:+48 22 555 01 01">call</a>'
    rows = _rows(_scan(monkeypatch, capsys)[1])
    for r in rows:                                    # the founder approves the personal address at Beta
        if r["lead_id"] == str(ids["beta"]):
            r["approve"] = "y"
    assert _apply(monkeypatch, _csv(rows), "--include-auto", "--dry-run") == 0
    with Session(e) as s:
        assert s.get(Lead, ids["acme"]).email == ""          # the dry run wrote nothing
    assert _apply(monkeypatch, _csv(rows), "--include-auto", "--mark-misses") == 0
    out = capsys.readouterr().out
    assert "apply 2 email(s) (reviewed 1, auto-accept 1) + 1 phone(s)" in out and "misses marked 4" in out
    with Session(e) as s:
        acme, beta = s.get(Lead, ids["acme"]), s.get(Lead, ids["beta"])
        assert (acme.email, acme.phone) == ("info@acme-steel.pl", "+48225550101")
        assert beta.email == "jan.kowalski@beta-hardware.co.uk"
        bodies = {a.lead_id: a.body for a in s.exec(select(Activity).where(Activity.kind == "enrichment")).all()}
        assert bodies[ids["acme"]].startswith("Web-enrich (reviewed, auto-accept): email info@acme-steel.pl")
        assert bodies[ids["beta"]].startswith("Web-enrich (reviewed): email jan.kowalski@")
        assert all(bodies[ids[k]].startswith(EMB.MISS) for k in ("gamma", "delta", "eps", "zeta"))
        # a marked miss is never picked up by an automatic (skip_attempted) enrichment pass
        left = {ld.id for ld in ES.candidate_leads(s, skip_attempted=True, include_managed=True)}
        assert not left & {ids[k] for k in ("gamma", "eps", "zeta")}
        # a Canada wave loaded meanwhile (email, never applied by this tool) must not slip in
        ca = Lead(product="Drywall anchors", managed=True, owner_id=None, seller_id=ids["seller"],
                  request_id=ids["request"], buyer_company="Maple Hardware", dest_country="CA",
                  email="buy@maple.ca", anon_ref="Buyer-CA-001")
        s.add(ca)
        c = s.get(Campaign, ids["campaign"])
        CAMP.set_sequence(s, c, [{"subject": "Anchors for {company}", "body": "Hello {company}, anchors."}])
        s.commit()                                     # a running edit: the sequence is now v2
        top = s.exec(select(func.max(CampaignRecipient.id))).one()
        ca_id = ca.id
    assert EMB.main(["enrol", "--request", SR_CODE, "--campaign", str(ids["campaign"]), "--expected", "3"]) == 2
    assert EMB.main(["enrol", "--request", SR_CODE, "--campaign", str(ids["campaign"]), "--expected", "2",
                     "--dry-run"]) == 0
    with Session(e) as s:
        assert s.exec(select(func.max(CampaignRecipient.id))).one() == top     # refused + dry run: nothing
    assert EMB.main(["enrol", "--request", SR_CODE, "--campaign", str(ids["campaign"]), "--expected", "2"]) == 0
    assert "sent after the 3 already queued" in capsys.readouterr().out
    with Session(e) as s:
        new = s.exec(select(CampaignRecipient).where(CampaignRecipient.id > top)
                     .order_by(CampaignRecipient.id)).all()
        assert [r.lead_id for r in new] == [ids["acme"], ids["beta"]]
        assert all(r.sequence_version == 2 and r.status == "pending" and r.request_id == ids["request"] for r in new)
        assert not s.exec(select(CampaignRecipient).where(CampaignRecipient.lead_id == ca_id)).first()
        assert s.exec(select(AuditLog).where(AuditLog.action == "enroll")).first()     # enroll's audit row kept
    # a second enrol finds nothing new to add
    assert EMB.main(["enrol", "--request", SR_CODE, "--campaign", str(ids["campaign"]), "--expected", "0"]) == 0
    assert DRY.NET_ATTEMPTS == []                     # scan aside, nothing above tried to reach the network
    # the worker (its own, unlocked process in production) sends the three already-queued buyers first (daily
    # limit 3), the enriched ones the next day
    monkeypatch.setattr(OUT.smtplib, "SMTP", FakeSMTP)
    assert worker.run_campaign_send(now=MONDAY)["sent"] == 3
    assert FakeSMTP.sent == ["old0@old0.pl", "old1@old1.pl", "old2@old2.pl"]
    assert worker.run_campaign_send(now=MONDAY + timedelta(days=1))["sent"] == 2
    assert FakeSMTP.sent[3:] == ["info@acme-steel.pl", "jan.kowalski@beta-hardware.co.uk"]
    # the seller sees anonymized progress only — never an enriched address, website or company
    with Session(e) as s:
        sr = s.get(ServiceRequest, ids["request"])
        assert pipeline.request_funnel(s, sr)["at_contacted"] == 5
        for ld in s.exec(select(Lead).where(Lead.request_id == ids["request"])).all():
            blob = str(pipeline.anon_prospect(ld))
            assert "acme" not in blob.lower() and "@" not in blob and "http" not in blob
    cl = TestClient(main.app)
    assert cl.post("/login", data={"email": "sharks@t", "password": "pw"}, follow_redirects=False).status_code == 303
    for path in ("/requests", "/leads", "/dashboard"):
        body = cl.get(path).text
        for secret in ("acme-steel", "Acme Steel", "beta-hardware", "Beta Hardware", "kowalski"):
            assert secret not in body, (path, secret)


def _row(e, key_or_id, ids, **kw):
    with Session(e) as s:
        ld = s.get(Lead, ids[key_or_id])
        row = {k: "" for k in EMB.COLS}
        row.update(lead_id=str(ld.id), external_id=ld.external_id, website=ld.website, decision="review",
                   found_on=(ld.website or "") + "/contact")
    row.update(kw)
    return row


def test_apply_takes_only_approved_rows_and_an_explicit_no_beats_include_auto(world, monkeypatch, capsys):
    e, ids = world
    rows = [_row(e, "acme", ids, decision="accept", email="info@acme-steel.pl"),
            _row(e, "beta", ids, decision="accept", email="sales@beta-hardware.co.uk", approve="n"),
            _row(e, "gamma", ids, decision="review", email="info@gamma-fixings.ie", approve="Y")]
    assert _apply(monkeypatch, _csv(rows)) == 0                       # no --include-auto: only the approved row
    with Session(e) as s:
        assert [s.get(Lead, ids[k]).email for k in ("acme", "beta", "gamma")] == ["", "", "info@gamma-fixings.ie"]
    assert _apply(monkeypatch, _csv(rows), "--include-auto") == 0
    with Session(e) as s:
        assert [s.get(Lead, ids[k]).email for k in ("acme", "beta")] == ["info@acme-steel.pl", ""]
    assert "buyer already has an email: 1" in capsys.readouterr().out     # gamma is never overwritten


@pytest.mark.parametrize("breakit", ["website", "external_id", "request", "owner", "missing"])
def test_apply_rolls_the_whole_run_back_when_a_row_no_longer_matches_its_buyer(world, monkeypatch, capsys, breakit):
    e, ids = world
    rows = [_row(e, "acme", ids, email="info@acme-steel.pl", approve="y"),
            _row(e, "beta", ids, email="sales@beta-hardware.co.uk", approve="y")]
    if breakit in ("website", "external_id"):
        rows[1][breakit] = "https://someone-else.pl" if breakit == "website" else "req-9:x:GB"
    elif breakit == "missing":
        rows[1]["lead_id"] = "999999"
    else:
        with Session(e) as s:
            ld = s.get(Lead, ids["beta"])
            if breakit == "request":
                ld.request_id = ids["request2"]
            else:
                ld.owner_id = ids["seller"]
            s.add(ld)
            s.commit()
    rc, out = _apply(monkeypatch, _csv(rows)), capsys.readouterr().out
    # a buyer that turned seller-owned trips the request-wide refusal first; everything else rolls back
    assert (rc, "REFUSED" in out) == (2, True) if breakit == "owner" else (rc, "rolled back" in out) == (1, True)
    with Session(e) as s:
        assert s.get(Lead, ids["acme"]).email == ""
        assert not s.exec(select(Activity).where(Activity.kind == "enrichment")).all()


def test_apply_refuses_duplicates_suppressed_bounced_and_taken_addresses(world, monkeypatch, capsys):
    e, ids = world
    with Session(e) as s:
        s.add(BounceRecord(email_normalized="sales@zeta-anchors.co.nz", lead_id=None))
        SUP.suppress(s, "stop@eps.com.au", "unsubscribe", scope="tenant", tenant_id=ids["seller"])
        s.commit()
    rows = [_row(e, "acme", ids, email="old0@old0.pl", approve="y"),                # on another buyer
            _row(e, "beta", ids, email="gone@bounced.ch", approve="y"),             # a campaign recipient
            _row(e, "gamma", ids, email="shared@x-group.pl", approve="y"),          # chosen twice ...
            _row(e, "delta", ids, email="Shared@X-Group.pl", approve="y"),          # ... in one file
            _row(e, "eps", ids, email="stop@eps.com.au", approve="y"),              # suppressed for this seller
            _row(e, "zeta", ids, email="sales@zeta-anchors.co.nz", approve="y"),   # bounced before
            _row(e, "gone", ids, email="new@bounced.ch", approve="y")]              # buyer already enrolled
    assert _apply(monkeypatch, _csv(rows)) == 0
    out = capsys.readouterr().out
    for why in ("on another buyer record: 1", "already a campaign recipient: 1", "same email chosen for several "
                "buyers: 2", "on the do-not-contact list: 1", "bounced before: 1",
                "buyer already contacted or enrolled: 1"):
        assert why in out, out
    assert "apply 0 email(s)" in out
    with Session(e) as s:
        assert all(not s.get(Lead, ids[k]).email for k in ("acme", "beta", "gamma", "delta", "eps", "zeta", "gone"))


def test_apply_writes_nothing_else(world, monkeypatch):
    e, ids = world

    def counts():
        with Session(e) as s:
            return [s.exec(select(func.count()).select_from(m)).one()
                    for m in (SellerUpdate, StageEvent, WorkItem, Outreach, CampaignRecipient, AuditLog)]
    before = counts()
    rows = [_row(e, "acme", ids, decision="accept", email="info@acme-steel.pl", phone="4.8E+10"),
            _row(e, "beta", ids, decision="reject", email="")]
    assert _apply(monkeypatch, _csv(rows), "--include-auto", "--mark-misses") == 0
    assert counts() == before and DRY.NET_ATTEMPTS == []
    with Session(e) as s:
        acme = s.get(Lead, ids["acme"])
        assert acme.email == "info@acme-steel.pl" and acme.phone == ""      # a spreadsheet-mangled phone dropped
        assert acme.pipeline_stage == "identified"                               # no stage change, no seller update


def test_apply_reads_a_spreadsheet_saved_csv(world, monkeypatch):
    e, ids = world
    text_ = _csv([_row(e, "acme", ids, email="info@acme-steel.pl", approve="y")])
    semicolons = "\ufeff" + "\n".join(";".join(line.split(",")) for line in text_.splitlines()) + "\n"
    assert _apply(monkeypatch, semicolons) == 0
    with Session(e) as s:
        assert s.get(Lead, ids["acme"]).email == "info@acme-steel.pl"
    # two scan chunks concatenated (`cat a.csv b.csv`): the second header line is skipped, not an error
    chunks = _csv([_row(e, "beta", ids, email="sales@beta-hardware.co.uk", approve="y")]) + \
        _csv([_row(e, "gamma", ids, email="sales@gamma-fixings.ie", approve="y")])
    assert _apply(monkeypatch, chunks) == 0
    with Session(e) as s:
        assert [s.get(Lead, ids[k]).email for k in ("beta", "gamma")] == ["sales@beta-hardware.co.uk",
                                                                          "sales@gamma-fixings.ie"]


def test_enrol_refuses_a_campaign_of_another_request_or_a_finished_one(world, monkeypatch, capsys):
    e, ids = world
    with Session(e) as s:
        c = s.get(Campaign, ids["campaign"])
        c.status = "completed"
        s.add(c)
        s.commit()
    assert EMB.main(["enrol", "--request", SR_CODE, "--campaign", str(ids["campaign"]), "--expected", "0"]) == 2
    assert "completed" in capsys.readouterr().out
    assert EMB.main(["enrol", "--request", "SR-202609-0002", "--campaign", str(ids["campaign"]),
                     "--expected", "0"]) == 2


def test_enrol_refuses_when_an_applied_buyer_became_ineligible_unless_told_to_skip(world, monkeypatch, capsys):
    e, ids = world
    rows = [_row(e, k, ids, email=f"info@{k}.pl", approve="y") for k in ("acme", "beta")]
    assert _apply(monkeypatch, _csv(rows)) == 0
    with Session(e) as s:
        SUP.suppress(s, "info@beta.pl", "unsubscribe")
        s.commit()
    capsys.readouterr()
    args = ["enrol", "--request", SR_CODE, "--campaign", str(ids["campaign"]), "--expected", "1"]
    assert EMB.main(args) == 2 and "not eligible" in capsys.readouterr().out
    assert EMB.main(args + ["--skip-ineligible"]) == 0
    with Session(e) as s:
        lids = set(s.exec(select(CampaignRecipient.lead_id).where(CampaignRecipient.campaign_id == ids["campaign"])))
        assert ids["acme"] in lids and ids["beta"] not in lids


def test_audience_lead_ids_filter_is_an_exact_ordered_set(world):
    e, ids = world
    with Session(e) as s:
        a, b = ids["old2"], ids["old0"]
        got = CAMP.audience_leads(s, ids["seller"], {"request_id": ids["request"], "lead_ids": [a, b]})
        assert [ld.id for ld in got] == sorted([a, b])
        assert CAMP.audience_leads(s, ids["seller"], {"request_id": ids["request"], "lead_ids": []}) == []
        everyone = [ld.id for ld in CAMP.audience_leads(s, ids["seller"], {"request_id": ids["request"]})]
        assert everyone == sorted(everyone) and len(everyone) == 10
        # the filter still sits INSIDE the tenant + request scope
        assert CAMP.audience_leads(s, ids["seller"], {"request_id": ids["request"], "lead_ids": [ids["foreign"]]}) == []
        c = s.get(Campaign, ids["campaign"])
        prev = CAMP.audience_preview(s, c, {"request_id": ids["request"], "lead_ids": [a, b]})
        assert prev["total_companies"] == 2 and prev["final_eligible"] == 0 and prev["already_in_campaign"] == 2
