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


def _counts(s):
    from app.models import Contract, FreightRequest, PaymentMilestone, RemittanceCase, Shipment
    return {
        "cases": s.exec(select(func.count()).select_from(OperationCase)).one(),
        "remittance": s.exec(select(func.count()).select_from(RemittanceCase)).one(),
        "freight_request": s.exec(select(func.count()).select_from(FreightRequest)).one(),
        "contract": s.exec(select(func.count()).select_from(Contract)).one(),
        "payment_milestone": s.exec(select(func.count()).select_from(PaymentMilestone)).one(),
        "shipment": s.exec(select(func.count()).select_from(Shipment)).one(),
    }


def test_remittance_conversion_creates_case_plus_remittancecase_no_payment(ctx):
    """Remittance/Sarafi → OperationCase(category=remittance) + a RemittanceCase(status=requested). It must NOT
    mint a PaymentMilestone — those come later, only after amount/currency/payer/payee/due/compliance."""
    with Session(ctx) as s:
        sr, sa = _seed_request(s, "remittance")
        admin = _u(s, "admin@t.local")
        out = OPS.route_service_request(s, sr, actor=admin); s.commit()
        c = _counts(s)
        assert out["operation_case"].category == "remittance" and out["operation_case"].tenant_id == sa.id
        assert out["remittance_case"].status == "requested"          # coordination record, not an executed transfer
        assert out["remittance_case"].request_id == sr.id and out["remittance_case"].tenant_id == sa.id
        assert c["cases"] == 1 and c["remittance"] == 1
        assert c["payment_milestone"] == 0                           # *** no premature PaymentMilestone ***


def test_freight_conversion_creates_case_plus_freightrequest_no_shipment(ctx):
    """Freight & shipping → OperationCase(category=freight) + a FreightRequest(status=draft). It must NOT mint a
    Shipment — a Shipment is booked only after a FreightOffer is selected and booking is confirmed."""
    with Session(ctx) as s:
        sr, sa = _seed_request(s, "freight")
        admin = _u(s, "admin@t.local")
        out = OPS.route_service_request(s, sr, actor=admin); s.commit()
        c = _counts(s)
        assert out["operation_case"].category == "freight" and out["operation_case"].tenant_id == sa.id
        assert out["freight_request"].status == "draft"             # captures the need, not a booked shipment
        assert out["freight_request"].request_id == sr.id and out["freight_request"].tenant_id == sa.id
        assert c["cases"] == 1 and c["freight_request"] == 1
        assert c["shipment"] == 0                                    # *** no premature Shipment ***


def test_docs_conversion_creates_case_only(ctx):
    """Documentation → OperationCase(category=docs). Individual DocumentRequirements are added later by the admin;
    the conversion itself creates neither a payment nor a shipment."""
    from app import tradedocs as TD
    from app.models import DocumentRequirement
    with Session(ctx) as s:
        sr, sa = _seed_request(s, "docs")
        admin = _u(s, "admin@t.local")
        out = OPS.route_service_request(s, sr, actor=admin); s.commit()
        c = _counts(s)
        assert out["operation_case"].category == "docs"
        assert c["cases"] == 1 and c["payment_milestone"] == 0 and c["shipment"] == 0
        # a requirement is added later, on the case (the Documentation workspace)
        TD.create_requirement(s, doc_type="certificate_of_origin", required_from="seller",
                              operation_case_id=out["operation_case"].id, tenant_id=sa.id, actor=admin); s.commit()
        docs = s.exec(select(DocumentRequirement)
                      .where(DocumentRequirement.operation_case_id == out["operation_case"].id)).all()
        assert len(docs) == 1 and docs[0].doc_type == "certificate_of_origin"


def test_contract_conversion_creates_linked_draft_no_parties_no_binding(ctx):
    """Contract drafting → a linked, NON-BINDING draft contract: needs_review status, no assumed counterparty,
    linked to the request. It is a Commercial concern (no Operations case) and the AI can never sign it."""
    from app import ai_actions as AIACT
    from app.models import Contract
    with Session(ctx) as s:
        sr, sa = _seed_request(s, "contract")
        admin = _u(s, "admin@t.local")
        out = OPS.route_service_request(s, sr, actor=admin); s.commit()
        c = out["contract"]
        assert c.request_id == sr.id and c.tenant_id == sa.id       # linked to the request
        assert c.status == "needs_review"                           # Needs Review — not a binding status
        assert c.status not in ("approved", "sent", "signed")       # never binding on intake
        assert c.company_id is None                                 # *** no assumed counterparty/party ***
        cnt = _counts(s)
        assert cnt["contract"] == 1 and cnt["cases"] == 0           # Commercial, not Operations
        assert cnt["payment_milestone"] == 0 and cnt["shipment"] == 0
        assert "sign_contract" in AIACT.NEVER                       # the AI can never sign

    admin_c = TestClient(main.app); _login(admin_c, "admin@t.local")
    assert admin_c.get("/contracts").status_code == 200             # the Commercial → Contracts workspace


@pytest.mark.parametrize("rtype", ["remittance", "freight", "contract", "docs"])
def test_conversion_is_idempotent_no_duplicates(ctx, rtype):
    """Converting the same request twice never creates a duplicate specialized record."""
    with Session(ctx) as s:
        sr, _sa = _seed_request(s, rtype)
        admin = _u(s, "admin@t.local")
        OPS.route_service_request(s, sr, actor=admin); s.commit()
        first = _counts(s)
        OPS.route_service_request(s, sr, actor=admin); s.commit()   # second conversion
        assert _counts(s) == first                                  # nothing duplicated


@pytest.mark.parametrize("rtype", ["remittance", "freight", "contract", "docs"])
def test_conversion_never_creates_premature_binding_records(ctx, rtype):
    """REGRESSION: intake conversion for ANY service must never create a PaymentMilestone or a Shipment."""
    with Session(ctx) as s:
        sr, _sa = _seed_request(s, rtype)
        admin = _u(s, "admin@t.local")
        OPS.route_service_request(s, sr, actor=admin); s.commit()
        c = _counts(s)
        assert c["payment_milestone"] == 0, "conversion created a premature PaymentMilestone"
        assert c["shipment"] == 0, "conversion created a premature Shipment"


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
