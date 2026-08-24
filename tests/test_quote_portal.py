"""Phase 6 (A) — secure buyer quote portal + hashed tokens.

Tokens hashed at rest + never plaintext; version-scoped; expired/revoked/altered fail safe; a token for quote A
never reaches quote B; idempotent accept (double-submit safe); rate limiting; and buyer acceptance raises the
admin deal task (never a Deal directly).
"""
from datetime import datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine, select

import app.main as main
import app.db as db
from app import quote_portal as QP, quote_service as QS, quote_workflow as QW, ratelimit as RL
from app.auth import hash_password
from app.models import (Deal, Lead, Product, Quote, QuoteAccessToken, QuoteVersion, User, WorkItem)


@pytest.fixture
def ctx(monkeypatch):
    e = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    SQLModel.metadata.create_all(e)
    with e.connect() as c:
        for ddl in ("CREATE UNIQUE INDEX IF NOT EXISTS uq_quoteaccesstoken_hash ON quoteaccesstoken(token_hash) WHERE token_hash != ''",
                    "CREATE UNIQUE INDEX IF NOT EXISTS uq_deal_quote_version ON deal(quote_version_id) WHERE quote_version_id IS NOT NULL",
                    "CREATE UNIQUE INDEX IF NOT EXISTS uq_workitem_idem_open ON workitem(idempotency_key) WHERE idempotency_key != '' AND status IN ('open','in_progress','waiting')"):
            c.execute(text(ddl))
        c.commit()
    monkeypatch.setattr(main, "engine", e)
    RL.reset()
    with Session(e) as s:
        s.add(User(email="admin@t.local", name="A", role="admin", active=True, password_hash=hash_password("pw")))
        s.add(User(email="kim@t.local", name="K", role="agent", active=True, password_hash=hash_password("pw")))
        s.commit()
    return e


def _login(c, email):
    assert c.post("/login", data={"email": email, "password": "pw"}, follow_redirects=False).status_code == 303


def _sent_quote(engine, code="G4-1"):
    """Create + approve + send a quote; return (quote_id, version_id, raw_token)."""
    with Session(engine) as s:
        ld = Lead(product="x", tracking_code=code, dest_country="GE"); s.add(ld); s.commit(); s.refresh(ld)
        p = Product(name="Copper", exw_price=8000, weight_kg_per_unit=1000, min_order_qty=25, currency="USD",
                    unit="tonne", origin_country="IR"); s.add(p); s.commit(); s.refresh(p)
        q = QS.create_quote(s, ld, p); s.commit()
        ver = QS.ensure_version(s, q); s.commit()
        QW.transition(s, q, "approved"); QW.transition(s, q, "sent"); s.commit()
        raw, _ = QP.mint_token(s, q, ver, valid_days=14); s.commit()
        return q.id, ver.id, raw


def test_token_stored_hashed_not_plaintext(ctx):
    qid, vid, raw = _sent_quote(ctx)
    with Session(ctx) as s:
        tok = s.exec(select(QuoteAccessToken)).first()
        assert tok.token_hash and tok.token_hash != raw and len(tok.token_hash) == 64   # sha256 hex
        assert raw not in tok.token_hash


def test_resolve_valid_and_failure_modes(ctx):
    qid, vid, raw = _sent_quote(ctx)
    with Session(ctx) as s:
        assert QP.resolve_token(s, raw) is not None
        assert QP.resolve_token(s, "totally-wrong") is None          # altered/guessed → None
        assert QP.resolve_token(s, "") is None
        tok = s.exec(select(QuoteAccessToken)).first()
        tok.expires_at = datetime.utcnow() - timedelta(days=1); s.add(tok); s.commit()
        assert QP.resolve_token(s, raw) is None                      # expired → None
        tok.expires_at = datetime.utcnow() + timedelta(days=1); tok.revoked = True; s.add(tok); s.commit()
        assert QP.resolve_token(s, raw) is None                      # revoked → None


def test_token_a_cannot_reach_quote_b(ctx):
    qa, va, raw_a = _sent_quote(ctx, "G4-A")
    qb, vb, raw_b = _sent_quote(ctx, "G4-B")
    with Session(ctx) as s:
        _, q, ver = QP.resolve_token(s, raw_a)
        assert q.id == qa and ver.id == va      # token A resolves ONLY to quote A, never B
        _, q2, _ = QP.resolve_token(s, raw_b)
        assert q2.id == qb


import re


def _csrf(html):
    m = re.search(r'name=.csrf. value=.([^"\']+)', html)
    return m.group(1) if m else ""


def _open(client, raw):
    """The buyer flow: the token rides in the URL FRAGMENT (never sent), so tests emulate the JS by POSTing it
    to the exchange. Returns the client with its portal cookie set."""
    client.get("/q/")                                            # bootstrap page (no token server-side)
    assert client.post("/q/exchange", data={"token": raw}).json()["ok"] is True
    return client


def test_token_never_in_url_bootstrap_is_clean(ctx):
    qid, vid, raw = _sent_quote(ctx)
    c = TestClient(main.app)
    b = c.get("/q/")                                             # the server-visible URL is just "/q/" — no token
    assert b.status_code == 200 and raw not in b.text           # token is in the fragment (client-only), not here
    assert "history.replaceState" in b.text                     # strips the fragment from URL + history
    assert "connect-src 'self'" in b.headers["content-security-policy"]


def test_exchange_then_tokenless_headered_session(ctx):
    qid, vid, raw = _sent_quote(ctx)
    c = _open(TestClient(main.app), raw)
    v = c.get("/q/session")                                     # tokenless URL
    assert v.status_code == 200 and raw not in v.text
    assert "frame-ancestors 'none'" in v.headers["content-security-policy"]
    assert v.headers["x-robots-tag"].startswith("noindex") and "no-store" in v.headers["cache-control"]
    assert v.headers["referrer-policy"] == "no-referrer"


def test_cookie_holds_only_opaque_sid_state_is_server_side(ctx):
    qid, vid, raw = _sent_quote(ctx)
    c = _open(TestClient(main.app), raw)
    from app.models import PortalSession
    with Session(ctx) as s:
        ps = s.exec(select(PortalSession)).one()
        assert ps.sid and ps.quote_id == qid and ps.quote_version_id == vid   # state lives SERVER-SIDE
        assert ps.token_hash and ps.csrf                                       # not in the cookie
    # a fresh client with no cookie cannot reach the session (proves the cookie, not the URL, carries access)
    assert TestClient(main.app).get("/q/session").status_code == 404


def test_link_is_truly_single_use(ctx):
    """A consumed link cannot be redeemed on a SECOND (cookieless) device — only the browser that already
    holds the session cookie may continue. Recovery is an admin link rotation."""
    from app.models import PortalSession
    qid, vid, raw = _sent_quote(ctx)
    c1 = _open(TestClient(main.app), raw)                      # first exchange: consumes the token
    with Session(ctx) as s:
        assert s.exec(select(QuoteAccessToken)).one().consumed_at is not None
        assert len(s.exec(select(PortalSession)).all()) == 1
    # a SECOND, cookieless client presenting the same (consumed) token is REFUSED — not a bearer link
    c2 = TestClient(main.app)
    r = c2.post("/q/exchange", data={"token": raw})
    assert r.status_code == 410 and r.json()["ok"] is False
    assert c2.get("/q/session").status_code == 404             # and it gets no session
    with Session(ctx) as s:
        assert len(s.exec(select(PortalSession)).all()) == 1   # no second session was minted
    # the ORIGINAL browser (still holding its cookie) may re-open the same link and continue
    assert c1.post("/q/exchange", data={"token": raw}).json()["ok"] is True
    assert c1.get("/q/session").status_code == 200
    with Session(ctx) as s:
        assert len(s.exec(select(PortalSession)).all()) == 1   # same session, not duplicated


def test_rotation_recovers_on_new_device_and_kills_old(ctx):
    """Recovery: an admin rotates the link → a NEW token works on a fresh device, and the OLD link + its live
    session are dead."""
    from app.models import PortalSession
    qid, vid, raw_old = _sent_quote(ctx)
    c_old = _open(TestClient(main.app), raw_old)               # buyer opened the old link
    assert c_old.get("/q/session").status_code == 200
    # admin rotates the link (new token; revokes the old token + its live session)
    with Session(ctx) as s:
        q = s.get(Quote, qid); ver = s.get(QuoteVersion, vid)
        raw_new, _ = QP.mint_token(s, q, ver, actor=None, valid_days=14, rotate=True); s.commit()
    assert c_old.get("/q/session").status_code == 404          # old cookie session revoked
    assert c_old.post("/q/exchange", data={"token": raw_old}).status_code == 410   # old token dead
    c_new = TestClient(main.app)                               # recovery on a new device with the new link
    assert c_new.post("/q/exchange", data={"token": raw_new}).json()["ok"] is True
    assert c_new.get("/q/session").status_code == 200


def test_get_does_not_mutate_view_recorded_via_post(ctx):
    qid, vid, raw = _sent_quote(ctx)
    c = _open(TestClient(main.app), raw)
    page = c.get("/q/session")                                   # GET render — must NOT change status
    with Session(ctx) as s:
        assert s.get(Quote, qid).status == "sent" and s.get(Quote, qid).viewed_at is None
    csrf = _csrf(page.text)
    assert c.post("/q/session/view", data={"csrf": csrf}).status_code == 200
    with Session(ctx) as s:
        assert s.get(Quote, qid).status == "viewed"
    c.post("/q/session/view", data={"csrf": csrf})              # repeat → still viewed once
    with Session(ctx) as s:
        assert s.get(Quote, qid).status == "viewed"


def test_decisions_are_post_only_and_csrf_protected(ctx):
    qid, vid, raw = _sent_quote(ctx)
    c = _open(TestClient(main.app), raw)
    page = c.get("/q/session"); csrf = _csrf(page.text)
    assert c.get("/q/session/respond").status_code == 405        # POST-only
    assert c.post("/q/session/respond", data={"action": "accept", "csrf": "bad"},
                  follow_redirects=False).status_code == 403     # CSRF rejected
    assert c.post("/q/session/respond", data={"action": "accept", "csrf": csrf},
                  follow_redirects=False).status_code == 200     # valid


def test_every_decision_idempotent_and_records_version(ctx):
    qid, vid, raw = _sent_quote(ctx)
    c = _open(TestClient(main.app), raw)
    csrf = _csrf(c.get("/q/session").text)
    c.post("/q/session/respond", data={"action": "accept", "csrf": csrf}, follow_redirects=False)
    c.post("/q/session/respond", data={"action": "accept", "csrf": csrf}, follow_redirects=False)  # replay
    c.post("/q/session/respond", data={"action": "reject", "csrf": csrf}, follow_redirects=False)  # after accept
    with Session(ctx) as s:
        from app.models import QuoteStatusEvent
        evs = s.exec(select(QuoteStatusEvent)).all()
        assert len([e for e in evs if e.to_status == "accepted"]) == 1     # exactly one accept, replay-safe
        assert not [e for e in evs if e.to_status == "rejected"]           # reject after accept is a no-op
        acc = next(e for e in evs if e.to_status == "accepted")
        assert acc.quote_version_id == vid                                 # exact version recorded
        assert len(s.exec(select(WorkItem).where(WorkItem.type == "accepted_quote_needs_deal")).all()) == 1
        assert s.exec(select(Deal)).all() == []                            # accept never creates a Deal


def test_exw_option_shown_but_internal_cost_hidden(ctx):
    """An intentionally-included buyer-facing EXW option appears; the internal EXW *cost* never does."""
    with Session(ctx) as s:
        ld = Lead(product="x", tracking_code="G4-E", dest_country="GE", buyer_company="ACME")
        s.add(ld); s.commit(); s.refresh(ld)
        p = Product(name="Copper", exw_price=8000, weight_kg_per_unit=1000, min_order_qty=25, currency="USD",
                    unit="tonne", origin_country="IR"); s.add(p); s.commit(); s.refresh(p)
        q = QS.create_quote(s, ld, p); s.commit()
        ver = QS.ensure_version(s, q); s.commit()
        import json
        ver.options = json.dumps([{"name": "Ex-works", "incoterm": "EXW", "unit_price": 8200.0,
                                   "currency": "USD", "included": "goods at works",
                                   "excluded": "freight, insurance, duties"}])
        s.add(ver); s.commit()
        QW.transition(s, q, "approved"); QW.transition(s, q, "sent"); s.commit()
        raw, _ = QP.mint_token(s, q, ver, valid_days=14); s.commit()
    c = _open(TestClient(main.app), raw)
    body = c.get("/q/session").text
    assert "EXW" in body and "Ex-works" in body and "8200" in body        # the buyer EXW OPTION is shown
    assert "8000" not in body                                             # the internal EXW buy-cost is NOT


def test_bad_token_fails_safe(ctx):
    c = TestClient(main.app)
    r = c.post("/q/exchange", data={"token": "totally-wrong"})
    assert r.status_code in (404, 410) and r.json()["ok"] is False       # unknown token → refused
    assert c.get("/q/session").status_code == 404                        # no session without an exchange
    seller = TestClient(main.app); _login(seller, "kim@t.local")
    assert seller.post("/q/exchange", data={"token": "x"}).json()["ok"] is False


def test_rate_limit_blocks_flood(ctx):
    RL.reset()
    c = TestClient(main.app)
    codes = [c.post("/q/exchange", data={"token": "guess"}).status_code for _ in range(60)]
    assert 429 in codes                                          # token-guessing is throttled
