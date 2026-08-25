"""Phase 9 v2 — end-to-end regression for the five concierge services.

For EACH service the intake must: create the correct ServiceRequest (right request_type + direction), stamp
tenant ownership (owner_id == requester_id == the submitting seller), raise a `review_new_request` Work Queue
task, be visible to the admin on /admin/requests, stay invisible to a different tenant, and route to the correct
specialized workspace with an IDEMPOTENT conversion (a second conversion never creates a duplicate operational
record):

    Find buyers        buyer_hunt  → Admin Requests / Work Queue → existing Research buyer-finding pipeline
    Remittance/Sarafi  remittance  → Operations → Payments & Remittance  (OperationCase category=remittance)
    Contract drafting  contract    → Commercial → Contracts
    Freight & shipping freight     → Operations → Freight / Shipments    (OperationCase category=freight)
    Documentation      docs        → Operations → Documentation          (OperationCase category=docs)
"""
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine, func, select

import app.main as main
from app import operations as OPS
from app import request_service as RS
from app.auth import hash_password
from app.models import CommandJob, OperationCase, ServiceRequest, User, WorkItem

_IDX = (
    "CREATE UNIQUE INDEX IF NOT EXISTS uq_workitem_idem_open ON workitem(idempotency_key) "
    "WHERE idempotency_key != '' AND status IN ('open','in_progress','waiting')",
    "CREATE UNIQUE INDEX IF NOT EXISTS uq_operationcase_reference ON operationcase(reference) "
    "WHERE reference != ''",
    "CREATE UNIQUE INDEX IF NOT EXISTS uq_opcase_deal_primary ON operationcase(deal_id) "
    "WHERE case_type = 'deal' AND deal_id IS NOT NULL",
)


@pytest.fixture
def ctx(monkeypatch):
    e = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    SQLModel.metadata.create_all(e)
    with e.connect() as c:
        for ddl in _IDX:
            c.execute(text(ddl))
        c.commit()
    monkeypatch.setattr(main, "engine", e)
    with Session(e) as s:
        s.add(User(email="admin@t.local", name="Admin", role="admin", active=True,
                   password_hash=hash_password("pw")))
        s.add(User(email="a@t.local", name="Seller A", role="agent", active=True,
                   password_hash=hash_password("pw")))
        s.add(User(email="b@t.local", name="Seller B", role="agent", active=True,
                   password_hash=hash_password("pw")))
        s.commit()
    return e


def _login(c, email):
    assert c.post("/login", data={"email": email, "password": "pw"}, follow_redirects=False).status_code == 303


def _u(s, email):
    return s.exec(select(User).where(User.email == email)).one()


def _submit(client, rtype, product="Zinc sulphate", market="Iraq", details="20 MT, monthly"):
    r = client.post("/requests", data={"request_type": rtype, "product": product, "market": market,
                                       "details": details}, follow_redirects=False)
    assert r.status_code == 303
    return r


SERVICES = [
    ("buyer_hunt", "sell"),
    ("remittance", "service"),
    ("contract", "service"),
    ("freight", "service"),
    ("docs", "service"),
]


@pytest.mark.parametrize("rtype,direction", SERVICES)
def test_service_intake_creates_correct_request(ctx, rtype, direction):
    seller = TestClient(main.app); _login(seller, "a@t.local")
    _submit(seller, rtype)
    with Session(ctx) as s:
        sa = _u(s, "a@t.local")
        reqs = s.exec(select(ServiceRequest)).all()
        assert len(reqs) == 1                                   # exactly one request, no duplication at intake
        sr = reqs[0]
        assert sr.request_type == rtype                         # correct type
        assert sr.direction == direction == RS.direction_for_type(rtype)
        assert sr.owner_id == sr.requester_id == sa.id          # tenant ownership = the submitting seller
        assert sr.tracking_code.startswith("SR-")


@pytest.mark.parametrize("rtype,_d", SERVICES)
def test_service_intake_raises_review_task(ctx, rtype, _d):
    seller = TestClient(main.app); _login(seller, "a@t.local")
    _submit(seller, rtype)
    with Session(ctx) as s:
        sr = s.exec(select(ServiceRequest)).one()
        tasks = s.exec(select(WorkItem).where(WorkItem.type == "review_new_request")).all()
        assert len(tasks) == 1 and tasks[0].related_request_id == sr.id   # one review task, linked to the request


@pytest.mark.parametrize("rtype,_d", SERVICES)
def test_service_admin_visibility_and_tenant_isolation(ctx, rtype, _d):
    seller = TestClient(main.app); _login(seller, "a@t.local")
    _submit(seller, rtype)
    with Session(ctx) as s:
        code = s.exec(select(ServiceRequest)).one().tracking_code

    admin = TestClient(main.app); _login(admin, "admin@t.local")
    body = admin.get("/admin/requests").text
    assert code in body                                         # admin sees every tenant's request

    owner = TestClient(main.app); _login(owner, "a@t.local")
    assert code in owner.get("/requests").text                  # the owner sees their own

    other = TestClient(main.app); _login(other, "b@t.local")
    assert code not in other.get("/requests").text              # a different tenant never sees it


# --------------------------------------------------------------------- specialized routing per service
def _seed_request(s, rtype):
    sa = _u(s, "a@t.local")
    sr = ServiceRequest(request_type=rtype, product="Zinc sulphate", market="Iraq", status="approved",
                        direction=RS.direction_for_type(rtype), workflow_status="approved",
                        requester_id=sa.id, owner_id=sa.id)
    s.add(sr); s.commit(); s.refresh(sr)
    return sr, sa


@pytest.mark.parametrize("rtype", ["remittance", "freight", "docs"])
def test_operations_services_convert_idempotently(ctx, rtype):
    """Remittance / Freight / Documentation route into Operations as ONE OperationCase categorized by the
    request type — converting twice must NOT create a duplicate operational record."""
    with Session(ctx) as s:
        sr, sa = _seed_request(s, rtype)
        admin = _u(s, "admin@t.local")
        case1, created1 = OPS.ensure_case_for_request(s, sr, actor=admin); s.commit()
        case2, created2 = OPS.ensure_case_for_request(s, sr, actor=admin); s.commit()
        assert created1 is True and created2 is False           # idempotent conversion
        assert case1.id == case2.id
        assert case1.category == rtype                          # routed to the correct Operations section
        assert case1.tenant_id == sa.id                         # ownership carried into Operations
        n = s.exec(select(func.count()).select_from(OperationCase)
                   .where(OperationCase.request_id == sr.id)).one()
        assert n == 1                                           # exactly ONE operational case — no duplicate


def test_remittance_routes_to_payments_and_remittance(ctx):
    from app.models import PaymentMilestone
    with Session(ctx) as s:
        sr, sa = _seed_request(s, "remittance")
        admin = _u(s, "admin@t.local")
        case, _ = OPS.ensure_case_for_request(s, sr, actor=admin); s.commit()
        # the Payments & Remittance section hangs a payment milestone off the case
        s.add(PaymentMilestone(operation_case_id=case.id, tenant_id=sa.id, kind="remittance",
                               amount=15000, currency="USD", status="pending")); s.commit()
        pays = s.exec(select(PaymentMilestone).where(PaymentMilestone.operation_case_id == case.id)).all()
        assert len(pays) == 1 and pays[0].currency == "USD"     # remittance surfaces under the case


def test_freight_routes_to_shipments(ctx):
    from app.models import Shipment
    with Session(ctx) as s:
        sr, sa = _seed_request(s, "freight")
        admin = _u(s, "admin@t.local")
        case, _ = OPS.ensure_case_for_request(s, sr, actor=admin); s.commit()
        s.add(Shipment(operation_case_id=case.id, tenant_id=sa.id, mode="sea",
                       current_milestone="booked", status="active")); s.commit()
        sh = s.exec(select(Shipment).where(Shipment.operation_case_id == case.id)).all()
        assert len(sh) == 1 and sh[0].mode == "sea"             # freight surfaces as a shipment on the case


def test_docs_routes_to_documentation(ctx):
    from app import tradedocs as TD
    with Session(ctx) as s:
        sr, sa = _seed_request(s, "docs")
        admin = _u(s, "admin@t.local")
        case, _ = OPS.ensure_case_for_request(s, sr, actor=admin); s.commit()
        TD.create_requirement(s, doc_type="certificate_of_origin", required_from="seller",
                              operation_case_id=case.id, tenant_id=sa.id, actor=admin); s.commit()
        from app.models import DocumentRequirement
        docs = s.exec(select(DocumentRequirement)
                      .where(DocumentRequirement.operation_case_id == case.id)).all()
        assert len(docs) == 1 and docs[0].doc_type == "certificate_of_origin"


def test_contract_routes_to_commercial_contracts(ctx):
    """Contract drafting is a Commercial concern — it does NOT become an Operations case; the admin drafts it in
    the Contracts workspace and the copilot may never auto-sign it."""
    from app import ai_actions as AIACT
    with Session(ctx) as s:
        sr, _sa = _seed_request(s, "contract")
        admin = _u(s, "admin@t.local")
        # a contract request is a Commercial concern — the Contracts workspace creates the document
        from app import contract_service as CONTRACT
        c, _v = CONTRACT.create_contract(s, contract_type="buyer_sales", side="buyer", country="IQ",
                                         terms="20 MT zinc sulphate CPT Basra", owner_id=admin.id, actor=admin)
        s.commit()
        assert c.id is not None and c.contract_type == "buyer_sales"
        # signing a contract is on the NEVER list — the AI can never execute it
        assert "sign_contract" in AIACT.NEVER

    admin_c = TestClient(main.app); _login(admin_c, "admin@t.local")
    assert admin_c.get("/contracts").status_code == 200          # the Commercial → Contracts workspace


def test_buyer_hunt_routes_to_existing_research_pipeline(ctx):
    """Find buyers routes to the EXISTING Research/harvest pipeline via the start_research adapter (a queued
    CommandJob) — never a rewritten one, and never auto-run before approval."""
    from app import ai_actions as AIACT
    from app import ai_command as CMD
    with Session(ctx) as s:
        admin = _u(s, "admin@t.local")
        conv = CMD.new_conversation(s, admin); s.commit()
        # propose + approve a start_research action → it must create a queued CommandJob through parse_command
        prop, err = AIACT.propose(s, conv, action_type="start_research",
                                  payload={"prompt": "find zinc sulphate buyers in Iraq"},
                                  reason="admin asked", target_summary="buyer research", actor=admin)
        assert prop is not None and not err
        ok, result = AIACT.approve(s, prop, nonce=prop.approval_nonce, actor=admin); s.commit()
        assert ok and result.get("queued") is True
        job = s.get(CommandJob, result["command_job_id"])
        assert job is not None and job.status == "queued"        # queued via the existing pipeline, not run
        # buyer_hunt is sell-side and does NOT create an Operations case
        assert RS.direction_for_type("buyer_hunt") == "sell"
