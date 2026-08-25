"""Phase 1 — admin header navigation (Architecture & Navigation).

The admin gets a two-level contextual header generated from ONE config (app/adminnav.py): 9 primary
workspaces (Phase 3 adds Work Queue between Dashboard and Requests), and only the ACTIVE workspace's children
in the secondary row. Non-admins never receive admin nav. Backend authorization is unchanged (admin-only
routes still 403). List/detail/action pages activate the correct parent + child. Country pages live under
Intelligence -> Markets. No migration, no data mutation.
"""
import pytest
from fastapi.testclient import TestClient
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine, func, select

import app.main as main
from app.auth import hash_password
from app.models import Deal, Lead, Product, Quote, User, WorkItem

PRIMARY = ["Dashboard", "Work Queue", "Requests", "Network", "Outreach", "Products", "Commercial",
           "Operations", "Intelligence", "Admin"]
# every existing admin destination must remain reachable from the header (primary hrefs + mobile panel kids)
REACHABLE = ["/admin/work-queue", "/admin/requests", "/leads", "/suppliers",
             "/campaigns", "/inbox", "/followups", "/templates", "/suppression", "/mail", "/outreach/analytics",
             "/catalog", "/categories", "/pricing", "/quotes", "/contracts", "/deals", "/contract-templates",
             "/commercial/analytics", "/research", "/intel", "/markets",
             "/operations", "/operations/freight", "/operations/shipments", "/operations/documentation",
             "/operations/payments", "/operations/exceptions",   # Phase 7 Operations
             "/intelligence", "/intelligence/demand", "/intelligence/opportunities", "/intelligence/performance",
             "/intelligence/reports", "/intelligence/sources",   # Phase 8 Intelligence
             "/lines/cd-dvd", "/command", "/ingest", "/admin/users", "/admin/activity",
             "/sellers", "/data-quality", "/duplicates"]   # + Phase 5: Suppliers/Categories/Pricing under Products
# admin-only routes (unchanged backend gate) — a non-admin must still get 403
ADMIN_ONLY = ["/command", "/research", "/suppliers", "/catalog", "/rates", "/ingest", "/admin/users",
              "/admin/activity", "/lines/cd-dvd", "/uae", "/intel", "/georgia", "/markets",
              "/sellers", "/data-quality", "/duplicates", "/export/buyers.csv",
              "/admin/work-queue",   # Phase 3 Work Queue
              "/campaigns", "/inbox", "/followups", "/templates", "/suppression", "/outreach/analytics",  # Phase 4
              "/categories", "/pricing",  # Phase 5 Products/Pricing
              "/contracts", "/contract-templates", "/commercial/analytics",  # Phase 6 Commercial
              "/operations", "/operations/freight", "/operations/shipments", "/operations/documentation",
              "/operations/payments", "/operations/exceptions",  # Phase 7 Operations
              "/intelligence", "/intelligence/demand", "/intelligence/opportunities", "/intelligence/performance",
              "/intelligence/reports", "/intelligence/sources"]  # Phase 8 Intelligence


@pytest.fixture
def ctx(monkeypatch):
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    SQLModel.metadata.create_all(engine)
    monkeypatch.setattr(main, "engine", engine)
    with Session(engine) as s:
        for email, role in [("admin@t.local", "admin"), ("seller@t.local", "agent")]:
            s.add(User(email=email, name=email.split("@")[0], role=role, active=True,
                       password_hash=hash_password("pw")))
        s.commit()
        prod = Product(name="Widget", unit="pcs", exw_price=1.0, weight_kg_per_unit=0.1)
        s.add(prod); s.commit(); s.refresh(prod)
        aid = s.exec(select(User).where(User.email == "admin@t.local")).first().id
        lead = Lead(product="Widget", buyer_company="Acorp", dest_country="IQ", owner_id=aid, tracking_code="G4-A")
        s.add(lead); s.commit(); s.refresh(lead)
        q = Quote(lead_id=lead.id, owner_id=aid, product_id=prod.id, quantity=10, delivered_unit=2.0,
                  delivered_total=20, status="draft", tracking_code="Q-A")
        d = Deal(lead_id=lead.id, owner_id=aid, tracking_code="D-A")
        s.add(q); s.add(d); s.commit(); s.refresh(q); s.refresh(d)
        ids = {"lead": lead.id, "quote": q.id, "deal": d.id}
    return TestClient(main.app), engine, ids


def _login(client, email):
    assert client.post("/login", data={"email": email, "password": "pw"},
                       follow_redirects=False).status_code == 303


def _region(html, aria):
    """Isolate one <nav aria-label="..."> ... </nav> block so we can assert what's INSIDE it."""
    key = 'aria-label="%s"' % aria
    i = html.find(key)
    if i < 0:
        return ""
    start = html.rfind("<nav", 0, i)
    end = html.find("</nav>", i)
    return html[start:end]


# --------------------------------------------------------------- primary workspaces
def test_admin_sees_all_primary_workspaces(ctx):
    client, _, _ = ctx
    _login(client, "admin@t.local")
    primary = _region(client.get("/leads").text, "Primary")
    for label in PRIMARY:
        assert (">" + label) in primary, f"missing primary workspace {label}"
    # Work Queue sits between Dashboard and Requests
    assert primary.find(">Dashboard") < primary.find(">Work Queue") < primary.find(">Requests")


def test_every_admin_route_reachable_from_header(ctx):
    client, _, _ = ctx
    _login(client, "admin@t.local")
    body = client.get("/").text            # dashboard: primary hrefs + mobile panel expose every destination
    for path in REACHABLE:
        assert ('href="%s"' % path) in body, f"{path} not reachable from the header"


# --------------------------------------------------------------- contextual secondary row
def test_secondary_row_shows_only_active_workspace_children(ctx):
    client, _, _ = ctx
    _login(client, "admin@t.local")
    commercial = _region(client.get("/quotes").text, "Commercial pages")
    assert ">Quotes" in commercial and ">Deals" in commercial
    # unrelated children must NOT appear in the Commercial secondary row
    for foreign in ["Buyers &amp; Prospects", "Suppliers", "Catalog", "Research", "Data Imports"]:
        assert foreign not in commercial, f"{foreign} leaked into Commercial secondary row"


def test_each_workspace_has_its_own_contextual_row(ctx):
    client, _, _ = ctx
    _login(client, "admin@t.local")
    cases = {
        "/leads": ("Network pages", ["Buyers &amp; Prospects", "Sellers"]),
        "/campaign": ("Outreach pages", ["Campaigns", "Email Accounts"]),
        "/catalog": ("Products pages", ["Catalog", "Categories", "Suppliers", "Pricing &amp; Rates"]),
        "/quotes": ("Commercial pages", ["Quotes", "Contracts", "Deals", "Commercial Analytics"]),
        "/operations": ("Operations pages", ["Overview", "Freight", "Shipments", "Documentation",
                                             "Payments &amp; Remittance", "Exceptions"]),
        "/research": ("Intelligence pages", ["Overview", "Demand", "Opportunities", "Markets", "Performance",
                                             "Reports", "Data Sources", "Research", "Market Intel",
                                             "Product Lines", "Command"]),
        "/ingest": ("Admin pages", ["Data Imports", "Accounts &amp; Access", "Team Activity"]),
    }
    for path, (aria, children) in cases.items():
        region = _region(client.get(path).text, aria)
        assert region, f"no secondary row for {path}"
        for c in children:
            assert c in region, f"{c} missing from {aria}"


# --------------------------------------------------------------- active states on list / detail
def test_detail_pages_activate_correct_parent_and_child(ctx):
    client, _, ids = ctx
    _login(client, "admin@t.local")
    checks = [
        (f"/leads/{ids['lead']}", "Network", "Buyers &amp; Prospects", "Network pages"),
        (f"/quotes/{ids['quote']}", "Commercial", "Quotes", "Commercial pages"),
        (f"/deals/{ids['deal']}", "Commercial", "Deals", "Commercial pages"),
        ("/suppliers", "Products", "Suppliers", "Products pages"),
    ]
    for path, parent, child, aria in checks:
        body = client.get(path).text
        assert ('aria-current="page">' + parent) in body, f"{path}: {parent} not active in primary"
        sec = _region(body, aria)
        assert ('aria-current="page">' + child) in sec, f"{path}: {child} not active in secondary"


def test_country_pages_activate_intelligence_markets(ctx):
    client, _, _ = ctx
    _login(client, "admin@t.local")
    for path in ["/georgia", "/uae", "/markets"]:
        body = client.get(path).text
        assert 'aria-current="page">Intelligence' in body, f"{path}: Intelligence not active"
        sec = _region(body, "Intelligence pages")
        assert 'aria-current="page">Markets' in sec, f"{path}: Markets child not active"


# --------------------------------------------------------------- Markets landing page
def test_markets_landing_lists_country_pages(ctx):
    client, _, _ = ctx
    _login(client, "admin@t.local")
    body = client.get("/markets").text
    assert "Georgia" in body and "United Arab Emirates" in body
    assert 'href="/georgia"' in body and 'href="/uae"' in body     # links to the existing pages
    # existing country routes + bookmarks still work
    assert client.get("/georgia").status_code == 200
    assert client.get("/uae").status_code == 200


# --------------------------------------------------------------- non-admin isolation + backend authz
def test_non_admin_gets_no_admin_navigation(ctx):
    client, _, _ = ctx
    _login(client, "seller@t.local")
    body = client.get("/").text
    for admin_label in ["Work Queue", "Network", "Outreach", "Commercial", "Operations", "Intelligence",
                        "Data Imports", "Buyers &amp; Prospects", "Accounts &amp; Access"]:
        assert admin_label not in body, f"seller saw admin nav: {admin_label}"
    assert "My Requests" in body and "Services" in body            # seller nav unchanged


def test_backend_authorization_unchanged(ctx):
    client, _, _ = ctx
    _login(client, "seller@t.local")
    for path in ADMIN_ONLY:
        assert client.get(path, follow_redirects=False).status_code == 403, f"{path} not protected"


# --------------------------------------------------------------- forms / query params still work
def test_existing_forms_and_query_params_still_work(ctx):
    client, _, _ = ctx
    _login(client, "admin@t.local")
    r = client.get("/leads?q=Acorp&sort=new")
    assert r.status_code == 200 and "Acorp" in r.text            # filter query still honored
    assert client.get("/campaign?source=iran-export-honey-royaljelly").status_code == 200
    assert client.get("/quotes").status_code == 200 and client.get("/deals").status_code == 200


# --------------------------------------------------------------- no migration / no data mutation
def test_navigation_does_not_mutate_data(ctx):
    client, engine, _ = ctx
    with Session(engine) as s:
        before = (s.exec(select(func.count(Lead.id))).one(),
                  s.exec(select(func.count(Quote.id))).one(),
                  s.exec(select(func.count(Deal.id))).one(),
                  s.exec(select(func.count(WorkItem.id))).one())
    _login(client, "admin@t.local")
    for path in ["/", "/leads", "/quotes", "/markets", "/georgia", "/ingest",
                 "/admin/work-queue", "/admin/requests"]:
        client.get(path)
    with Session(engine) as s:
        after = (s.exec(select(func.count(Lead.id))).one(),
                 s.exec(select(func.count(Quote.id))).one(),
                 s.exec(select(func.count(Deal.id))).one(),
                 s.exec(select(func.count(WorkItem.id))).one())
    assert before == after
