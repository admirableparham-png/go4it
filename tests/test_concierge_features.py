"""Trader power features: re-deliver, per-request chat, reversible unlist, connected-mailbox outreach.
Isolation is enforced the same way as everywhere else (owns/scoped) — these tests prove the happy path
AND that a second trader can't reach another's request/leads."""
import pytest
from fastapi.testclient import TestClient
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine, select

import app.main as main
from app.auth import hash_password
from app.models import Lead, MailAccount, RequestDeliverable, RequestMessage, ServiceRequest, User


@pytest.fixture
def ctx(monkeypatch):
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    SQLModel.metadata.create_all(engine)
    monkeypatch.setattr(main, "engine", engine)
    ids = {}
    with Session(engine) as s:
        for email, role in [("admin@t.local", "admin"), ("kim@t.local", "agent"), ("c@t.local", "agent")]:
            s.add(User(email=email, name=email.split("@")[0], role=role, active=True,
                       password_hash=hash_password("pw")))
        s.commit()
        uid = {u.email: u.id for u in s.exec(select(User)).all()}
        ids["trader"] = uid["kim@t.local"]
        sr = ServiceRequest(request_type="buyer_hunt", product="black tea", status="running",
                            requester_id=uid["kim@t.local"], owner_id=uid["kim@t.local"],
                            tracking_code="SR-202608-0009", result_source_tag="req-1")
        s.add(sr); s.commit(); s.refresh(sr)
        ids["req"] = sr.id
        for co, email in [("Acorp", "a@buyer.com"), ("Bcorp", "")]:
            s.add(Lead(product="black tea", buyer_company=co, email=email, owner_id=uid["kim@t.local"],
                       tracking_code=f"G4-{co}", active=True))
        s.commit()
        ids["lead_ids"] = [ld.id for ld in s.exec(select(Lead)).all()]
    return TestClient(main.app), engine, ids


def _login(client, email):
    assert client.post("/login", data={"email": email, "password": "pw"},
                       follow_redirects=False).status_code == 303


def test_new_pages_render(ctx):
    client, _, _ = ctx
    _login(client, "kim@t.local")
    for path in ["/requests", "/leads", "/leads?listed=no", "/leads?listed=all"]:
        assert client.get(path).status_code == 200, path
    assert client.get("/mail", follow_redirects=False).status_code == 403   # email tool is admin-only now
    _login(client, "admin@t.local")
    for path in ["/admin/requests", "/mail", "/leads"]:
        assert client.get(path).status_code == 200, path


def test_chat_roundtrip_and_isolation(ctx):
    client, engine, ids = ctx
    req = ids["req"]
    _login(client, "kim@t.local")
    assert client.post(f"/requests/{req}/messages", data={"body": "hi admin"},
                       follow_redirects=False).status_code == 303
    _login(client, "admin@t.local")
    assert client.post(f"/requests/{req}/messages", data={"body": "hi trader"},
                       follow_redirects=False).status_code == 303
    _login(client, "kim@t.local")
    thread = client.get(f"/requests/{req}/thread").text
    assert "hi admin" in thread and "hi trader" in thread
    with Session(engine) as s:
        assert len(s.exec(select(RequestMessage)).all()) == 2
    # a different trader can neither post nor read this request's chat
    _login(client, "c@t.local")
    assert client.post(f"/requests/{req}/messages", data={"body": "sneak"},
                       follow_redirects=False).status_code == 404
    assert client.get(f"/requests/{req}/thread", follow_redirects=False).status_code == 404


def test_redeliver_appends_history(ctx):
    client, engine, ids = ctx
    req = ids["req"]
    _login(client, "admin@t.local")
    for note in ["first delivery", "second delivery"]:
        assert client.post(f"/admin/requests/{req}/done", data={"result": note},
                           follow_redirects=False).status_code == 303
    with Session(engine) as s:
        assert s.get(ServiceRequest, req).status == "done"
        dvs = s.exec(select(RequestDeliverable).where(RequestDeliverable.request_id == req)).all()
        assert len(dvs) == 2   # re-delivery stacked, didn't overwrite


def test_unlist_then_relist(ctx):
    client, engine, ids = ctx
    lead = ids["lead_ids"][0]
    _login(client, "kim@t.local")
    assert client.post(f"/leads/{lead}/unlist", follow_redirects=False).status_code == 303
    with Session(engine) as s:
        assert not s.get(Lead, lead).active
    assert "G4-Acorp" not in client.get("/leads").text          # hidden from the default list
    assert "G4-Acorp" in client.get("/leads?listed=no").text     # visible under "Unlisted"
    assert client.post(f"/leads/{lead}/unlist?relist=1", follow_redirects=False).status_code == 303
    with Session(engine) as s:
        assert s.get(Lead, lead).active


def test_bulk_email_compose_and_guards(ctx):
    client, _, ids = ctx
    # buyer outreach is ADMIN-ONLY now (confidential model): a seller is forbidden
    _login(client, "kim@t.local")
    assert client.post("/leads/bulk", data={"action": "email", "ids": ids["lead_ids"]},
                       follow_redirects=False).status_code == 403
    assert client.post("/leads/bulk/email",
                       data={"account_id": 1, "subject": "hi", "body": "x", "ids": ids["lead_ids"]},
                       follow_redirects=False).status_code == 403
    # the admin can compose; sending with no connected account bounces to /mail
    _login(client, "admin@t.local")
    r = client.post("/leads/bulk", data={"action": "email", "ids": ids["lead_ids"]})
    assert r.status_code == 200 and "Email selected buyers" in r.text
    r = client.post("/leads/bulk/email",
                    data={"account_id": 999, "subject": "hi", "body": "x", "ids": ids["lead_ids"]},
                    follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/mail"


def test_mail_add_encrypts_and_defaults(ctx, monkeypatch):
    client, engine, ids = ctx
    monkeypatch.setattr(main, "verify_smtp", lambda *a, **k: (True, ""))
    _login(client, "admin@t.local")     # Go4it mailboxes are admin-only now
    assert client.post("/mail", data={"email": "go4it@gmail.com", "app_password": "app-pw-123",
                                       "provider": "gmail", "from_name": "Go4it"},
                       follow_redirects=False).status_code == 303
    with Session(engine) as s:
        accts = s.exec(select(MailAccount)).all()
        assert len(accts) == 1 and accts[0].is_default and accts[0].email == "go4it@gmail.com"
        assert accts[0].smtp_password_enc and accts[0].smtp_password_enc != "app-pw-123"


def test_mail_encrypt_roundtrip():
    from app.outreach import mail_decrypt, mail_encrypt
    tok = mail_encrypt("s3cret-app-pw")
    assert tok and tok != "s3cret-app-pw" and mail_decrypt(tok) == "s3cret-app-pw"
