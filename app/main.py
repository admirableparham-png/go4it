"""go4it web app — catalog + leads + matching + quotation + team CRM."""
import json
import logging
from datetime import datetime
from pathlib import Path

from fastapi import FastAPI, File, Form, Request, UploadFile
from fastapi.responses import HTMLResponse, PlainTextResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlmodel import Session, select
from starlette.middleware.sessions import SessionMiddleware

from .auth import current_user, role_at_least, verify_password
from .config import INBOX_DIR, SECRET_KEY
from .csv_import import parse_products
from .db import engine, init_db
from .ingest import ingest_source
from .lead_service import create_lead
from .models import (Activity, CostParam, FxRate, IngestionRun, Lead, Match,
                     Product, Quote, RateCard, Supplier, User)
from .quote_service import create_quote
from .sources.go4world_csv import Go4WorldCsvSource
from .telegram import notify_quote_ready, notify_status_change

logger = logging.getLogger("go4it")
BASE_DIR = Path(__file__).parent
app = FastAPI(title="go4it")
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))
app.mount("/static", StaticFiles(directory=str(BASE_DIR / "static")), name="static")

PUBLIC_PREFIXES = ("/login", "/logout", "/static")

# Allowed pipeline transitions. Lost requires a reason (enforced in the route).
TRANSITIONS = {
    "new": {"quoted", "negotiating", "lost"},
    "quoted": {"negotiating", "won", "lost"},
    "negotiating": {"won", "lost"},
    "won": set(),
    "lost": {"negotiating"},
}
STAGES = ["new", "quoted", "negotiating", "won", "lost"]

SAMPLE_CSV = (
    "name,category,spec,hs_code,exw_price,currency,unit,weight_kg_per_unit,"
    "cbm_per_unit,packaging,min_order_qty,origin_region,supplier\n"
    "Steel rebar 12mm,metals,A3 / B500B,7214,590,USD,ton,1000,0.13,bundled,25,Isfahan,Isfahan Steel Co\n"
    "Portland cement 42.5,construction,Type II,2523,55,USD,ton,1000,0.7,50kg bags,100,Tehran,Tehran Cement\n"
    "Bitumen 60/70,petrochemicals,penetration 60/70,2713,380,USD,ton,1000,1.0,steel drums,20,Tabriz,Pasargad Oil\n"
)


@app.middleware("http")
async def auth_gate(request: Request, call_next):
    """Require a logged-in session for everything except public paths."""
    path = request.url.path
    if not any(path.startswith(p) for p in PUBLIC_PREFIXES) and not request.session.get("user_id"):
        return RedirectResponse("/login", status_code=303)
    return await call_next(request)


# SessionMiddleware added last -> outermost -> request.session ready in auth_gate.
app.add_middleware(SessionMiddleware, secret_key=SECRET_KEY, same_site="lax")


@app.on_event("startup")
def on_startup() -> None:
    init_db()


# ----------------------------------------------------------------------------- helpers

def _forbidden():
    return HTMLResponse("Forbidden", status_code=403)


def _log(session, lead: Lead, user, kind: str, body: str = ""):
    """Append a timeline entry; stamp first_response_at on the first real action."""
    session.add(Activity(lead_id=lead.id, user_id=user.id if user else None,
                         kind=kind, body=body))
    if kind in ("note", "call", "status_change", "quote_sent") and lead.first_response_at is None:
        lead.first_response_at = datetime.utcnow()
        session.add(lead)


def _get_or_create_supplier(session, name: str):
    name = (name or "").strip()
    if not name:
        return None
    norm = name.lower()
    supplier = session.exec(select(Supplier).where(Supplier.name_normalized == norm)).first()
    if supplier is None:
        supplier = Supplier(name=name, name_normalized=norm)
        session.add(supplier)
        session.commit()
        session.refresh(supplier)
    return supplier


def _set_param(session, key, value, unit=""):
    cp = session.exec(select(CostParam).where(CostParam.key == key)).first()
    if cp is None:
        cp = CostParam(key=key, unit=unit)
    cp.value = value
    session.add(cp)


def _set_card(session, leg, rate_per_truck, lane_to="", capacity=25.0):
    card = session.exec(
        select(RateCard).where(RateCard.leg == leg, RateCard.active == True)  # noqa: E712
    ).first()
    if card is None:
        card = RateCard(leg=leg, active=True)
    card.rate_per_truck = rate_per_truck
    card.truck_capacity_t = capacity
    if lane_to:
        card.lane_to = lane_to
    session.add(card)


# ----------------------------------------------------------------------------- auth

@app.get("/login", response_class=HTMLResponse)
def login_page(request: Request, error: str = ""):
    return templates.TemplateResponse("login.html", {"request": request, "error": error})


@app.post("/login")
def login(request: Request, email: str = Form(...), password: str = Form(...)):
    with Session(engine) as session:
        user = session.exec(select(User).where(User.email == email.strip().lower())).first()
        if user and user.active and verify_password(password, user.password_hash):
            request.session["user_id"] = user.id
            return RedirectResponse("/", status_code=303)
    return RedirectResponse("/login?error=1", status_code=303)


@app.get("/logout")
def logout(request: Request):
    request.session.clear()
    return RedirectResponse("/login", status_code=303)


# ----------------------------------------------------------------------------- dashboard

@app.get("/", response_class=HTMLResponse)
def dashboard(request: Request):
    with Session(engine) as session:
        user = current_user(request, session)
        leads = session.exec(select(Lead).order_by(Lead.id.desc())).all()
        products = {p.id: p for p in session.exec(select(Product)).all()}
        users = {u.id: u for u in session.exec(select(User)).all()}
        quotes_by_lead = {}
        for q in session.exec(select(Quote).order_by(Quote.id.desc())).all():
            quotes_by_lead.setdefault(q.lead_id, []).append(q)
        lead_rows = []
        for lead in leads:
            ms = session.exec(
                select(Match).where(Match.lead_id == lead.id).order_by(Match.score.desc())
            ).all()
            lead_rows.append({
                "lead": lead,
                "owner": users.get(lead.owner_id),
                "matches": [{"m": m, "product": products.get(m.product_id)} for m in ms],
                "quotes": quotes_by_lead.get(lead.id, []),
            })
        product_count = len(products)
    return templates.TemplateResponse(
        "index.html",
        {"request": request, "user": user, "lead_rows": lead_rows, "product_count": product_count},
    )


@app.post("/leads")
def add_lead(
    request: Request,
    product: str = Form(...),
    category: str = Form(""),
    spec: str = Form(""),
    quantity: float = Form(0, ge=0),
    unit: str = Form(""),
    target_price: float = Form(0, ge=0),
    currency: str = Form("USD"),
    dest_country: str = Form(""),
    dest_city: str = Form(""),
    buyer_company: str = Form(""),
    contact_name: str = Form(""),
    email: str = Form(""),
    phone: str = Form(""),
    notes: str = Form(""),
):
    with Session(engine) as session:
        user = current_user(request, session)
        if not role_at_least(user, "agent"):
            return _forbidden()
        lead = Lead(
            product=product, category=category, spec=spec, quantity=quantity,
            unit=unit, target_price=target_price, currency=currency,
            dest_country=dest_country.strip().upper(), dest_city=dest_city,
            buyer_company=buyer_company, contact_name=contact_name,
            email=email, phone=phone, notes=notes, source="manual",
            owner_id=user.id,
        )
        create_lead(session, lead)   # dedup + tracking code + match/quote/alert
    return RedirectResponse("/", status_code=303)


# ----------------------------------------------------------------------------- lead detail + pipeline

@app.get("/leads/{lead_id}", response_class=HTMLResponse)
def lead_detail(request: Request, lead_id: int):
    with Session(engine) as session:
        user = current_user(request, session)
        lead = session.get(Lead, lead_id)
        if not lead:
            return HTMLResponse("Not found", status_code=404)
        products = {p.id: p for p in session.exec(select(Product)).all()}
        users = session.exec(select(User).where(User.active == True)).all()  # noqa: E712
        matches = session.exec(
            select(Match).where(Match.lead_id == lead_id).order_by(Match.score.desc())
        ).all()
        quotes = session.exec(
            select(Quote).where(Quote.lead_id == lead_id).order_by(Quote.id.desc())
        ).all()
        acts = session.exec(
            select(Activity).where(Activity.lead_id == lead_id).order_by(Activity.id.desc())
        ).all()
        umap = {u.id: u for u in session.exec(select(User)).all()}
        owner = umap.get(lead.owner_id)
        timeline = [{"a": a, "user": umap.get(a.user_id)} for a in acts]
    return templates.TemplateResponse(
        "lead_detail.html",
        {"request": request, "user": user, "lead": lead, "owner": owner,
         "users": users, "products": products,
         "matches": [{"m": m, "product": products.get(m.product_id)} for m in matches],
         "quotes": quotes, "timeline": timeline,
         "next_stages": sorted(TRANSITIONS.get(lead.status, set()))},
    )


@app.post("/leads/{lead_id}/assign")
def assign_lead(request: Request, lead_id: int, owner_id: int = Form(...)):
    with Session(engine) as session:
        user = current_user(request, session)
        if not role_at_least(user, "agent"):
            return _forbidden()
        lead = session.get(Lead, lead_id)
        new_owner = session.get(User, owner_id)
        if lead and new_owner:
            lead.owner_id = new_owner.id
            session.add(lead)
            _log(session, lead, user, "assignment", f"assigned to {new_owner.name or new_owner.email}")
            session.commit()
    return RedirectResponse(f"/leads/{lead_id}", status_code=303)


@app.post("/leads/{lead_id}/stage")
def change_stage(request: Request, lead_id: int, status: str = Form(...), reason: str = Form("")):
    with Session(engine) as session:
        user = current_user(request, session)
        if not role_at_least(user, "agent"):
            return _forbidden()
        lead = session.get(Lead, lead_id)
        if not lead:
            return HTMLResponse("Not found", status_code=404)
        old = lead.status
        if status not in TRANSITIONS.get(old, set()):
            return RedirectResponse(f"/leads/{lead_id}?error=transition", status_code=303)
        if status == "lost" and not reason.strip():
            return RedirectResponse(f"/leads/{lead_id}?error=reason", status_code=303)
        lead.status = status
        if status == "lost":
            lead.lost_reason = reason.strip()
        session.add(lead)
        _log(session, lead, user, "status_change",
             f"{old} -> {status}" + (f" ({reason.strip()})" if reason.strip() else ""))
        session.commit()
        session.refresh(lead)
        notify_status_change(lead, old, status, user.name or user.email)
    return RedirectResponse(f"/leads/{lead_id}", status_code=303)


@app.post("/leads/{lead_id}/note")
def add_note(request: Request, lead_id: int, body: str = Form(...)):
    with Session(engine) as session:
        user = current_user(request, session)
        if not role_at_least(user, "agent"):
            return _forbidden()
        lead = session.get(Lead, lead_id)
        if lead and body.strip():
            _log(session, lead, user, "note", body.strip())
            session.commit()
    return RedirectResponse(f"/leads/{lead_id}", status_code=303)


# ----------------------------------------------------------------------------- catalog

@app.get("/catalog", response_class=HTMLResponse)
def catalog(request: Request, imported: int = 0, updated: int = 0, errors: int = 0):
    with Session(engine) as session:
        user = current_user(request, session)
        products = session.exec(select(Product).order_by(Product.id.desc())).all()
        suppliers = {s.id: s for s in session.exec(select(Supplier)).all()}
    return templates.TemplateResponse(
        "catalog.html",
        {"request": request, "user": user, "products": products, "suppliers": suppliers,
         "imported": imported, "updated": updated, "errors": errors},
    )


@app.post("/catalog/products")
def add_product(
    request: Request,
    name: str = Form(...),
    category: str = Form(""),
    spec: str = Form(""),
    hs_code: str = Form(""),
    exw_price: float = Form(0, ge=0),
    currency: str = Form("USD"),
    unit: str = Form(""),
    weight_kg_per_unit: float = Form(0, ge=0),
    cbm_per_unit: float = Form(0, ge=0),
    packaging: str = Form(""),
    min_order_qty: float = Form(0, ge=0),
    origin_region: str = Form(""),
    supplier: str = Form(""),
):
    with Session(engine) as session:
        if not role_at_least(current_user(request, session), "agent"):
            return _forbidden()
        sup = _get_or_create_supplier(session, supplier)
        session.add(Product(
            name=name, category=category, spec=spec, hs_code=hs_code,
            exw_price=exw_price, currency=currency, unit=unit,
            weight_kg_per_unit=weight_kg_per_unit, cbm_per_unit=cbm_per_unit,
            packaging=packaging, min_order_qty=min_order_qty,
            origin_region=origin_region, supplier_id=sup.id if sup else None,
        ))
        session.commit()
    return RedirectResponse("/catalog", status_code=303)


@app.post("/catalog/import")
def import_catalog(request: Request, file: UploadFile = File(...)):
    with Session(engine) as session:
        if not role_at_least(current_user(request, session), "agent"):
            return _forbidden()
        text = file.file.read().decode("utf-8-sig", errors="replace")
        rows, errors = parse_products(text)
        imported = updated = 0
        fields = ("category", "spec", "hs_code", "exw_price", "currency", "unit",
                  "weight_kg_per_unit", "cbm_per_unit", "packaging", "min_order_qty",
                  "origin_region")
        for rec in rows:
            sup = _get_or_create_supplier(session, rec.get("supplier", ""))
            existing = session.exec(select(Product).where(Product.name == rec["name"])).first()
            product = existing or Product(name=rec["name"])
            for field in fields:
                if field in rec:
                    setattr(product, field, rec[field])
            if sup:
                product.supplier_id = sup.id
            product.updated_at = datetime.utcnow()
            session.add(product)
            updated += 1 if existing else 0
            imported += 0 if existing else 1
        session.commit()
    return RedirectResponse(
        f"/catalog?imported={imported}&updated={updated}&errors={len(errors)}", status_code=303)


@app.get("/catalog/sample.csv", response_class=PlainTextResponse)
def sample_csv():
    return PlainTextResponse(
        SAMPLE_CSV,
        headers={"Content-Disposition": "attachment; filename=go4it_products_sample.csv"})


# ----------------------------------------------------------------------------- quotes

@app.post("/leads/{lead_id}/quote/{product_id}")
def quote_match(request: Request, lead_id: int, product_id: int):
    with Session(engine) as session:
        if not role_at_least(current_user(request, session), "agent"):
            return _forbidden()
        lead = session.get(Lead, lead_id)
        product = session.get(Product, product_id)
        if not lead or not product:
            return HTMLResponse("Not found", status_code=404)
        quote = create_quote(session, lead, product)
        notify_quote_ready(quote, lead, product)
        quote_id = quote.id
    return RedirectResponse(f"/quotes/{quote_id}", status_code=303)


@app.get("/quotes", response_class=HTMLResponse)
def quotes_list(request: Request):
    with Session(engine) as session:
        user = current_user(request, session)
        quotes = session.exec(select(Quote).order_by(Quote.id.desc())).all()
        leads = {l.id: l for l in session.exec(select(Lead)).all()}
        products = {p.id: p for p in session.exec(select(Product)).all()}
        rows = [{"q": q, "lead": leads.get(q.lead_id), "product": products.get(q.product_id)}
                for q in quotes]
    return templates.TemplateResponse("quotes_list.html", {"request": request, "user": user, "rows": rows})


@app.get("/quotes/{quote_id}", response_class=HTMLResponse)
def quote_detail(request: Request, quote_id: int):
    with Session(engine) as session:
        user = current_user(request, session)
        q = session.get(Quote, quote_id)
        if not q:
            return HTMLResponse("Not found", status_code=404)
        lead = session.get(Lead, q.lead_id)
        product = session.get(Product, q.product_id)
        breakdown = json.loads(q.breakdown or "[]")
        fx = json.loads(q.fx_snapshot or "{}")
    return templates.TemplateResponse(
        "quote_detail.html",
        {"request": request, "user": user, "q": q, "lead": lead, "product": product,
         "breakdown": breakdown, "fx": fx, "can_approve": role_at_least(user, "manager")},
    )


@app.post("/quotes/{quote_id}/approve")
def approve_quote(request: Request, quote_id: int):
    with Session(engine) as session:
        user = current_user(request, session)
        if not role_at_least(user, "manager"):    # only manager+ can approve
            return _forbidden()
        q = session.get(Quote, quote_id)
        if q and q.status == "draft":
            q.status = "approved"
            q.approved_by = user.email
            session.add(q)
            session.commit()
    return RedirectResponse(f"/quotes/{quote_id}", status_code=303)


@app.post("/quotes/{quote_id}/send")
def send_quote(request: Request, quote_id: int):
    with Session(engine) as session:
        user = current_user(request, session)
        if not role_at_least(user, "manager"):     # only manager+ can send
            return _forbidden()
        q = session.get(Quote, quote_id)
        if q and q.status == "approved":
            q.status = "sent"
            session.add(q)
            lead = session.get(Lead, q.lead_id)
            if lead and lead.status == "new":
                lead.status = "quoted"
                _log(session, lead, user, "status_change", "new -> quoted (quote sent)")
                session.add(lead)
            if lead:
                _log(session, lead, user, "quote_sent", f"sent {q.tracking_code}")
            session.commit()
    return RedirectResponse(f"/quotes/{quote_id}", status_code=303)


# ----------------------------------------------------------------------------- ingestion

@app.get("/ingest", response_class=HTMLResponse)
def ingest_page(request: Request):
    with Session(engine) as session:
        user = current_user(request, session)
        runs = session.exec(
            select(IngestionRun).order_by(IngestionRun.id.desc()).limit(20)
        ).all()
    pending = len(list(Path(INBOX_DIR).glob("*.csv"))) if Path(INBOX_DIR).exists() else 0
    return templates.TemplateResponse(
        "ingest.html",
        {"request": request, "user": user, "runs": runs, "pending": pending, "inbox": INBOX_DIR})


@app.post("/ingest/run")
def ingest_run(request: Request):
    with Session(engine) as session:
        if not role_at_least(current_user(request, session), "manager"):
            return _forbidden()
    ingest_source(Go4WorldCsvSource(INBOX_DIR))   # opens its own session
    return RedirectResponse("/ingest", status_code=303)


# ----------------------------------------------------------------------------- rates admin

@app.get("/rates", response_class=HTMLResponse)
def rates_page(request: Request):
    with Session(engine) as session:
        user = current_user(request, session)
        pvals = {cp.key: cp.value for cp in session.exec(select(CostParam)).all()}
        inland = session.exec(
            select(RateCard).where(RateCard.leg == "inland", RateCard.active == True)).first()  # noqa: E712
        intl = session.exec(
            select(RateCard).where(RateCard.leg == "international", RateCard.active == True)).first()  # noqa: E712
        fxs = session.exec(select(FxRate)).all()
    return templates.TemplateResponse(
        "rates.html",
        {"request": request, "user": user, "p": pvals, "inland": inland, "intl": intl, "fxs": fxs})


@app.post("/rates/params")
def update_params(
    request: Request,
    export_clearance: float = Form(0, ge=0),
    coo_fee: float = Form(0, ge=0),
    insurance_pct: float = Form(0, ge=0),
    financing_pct: float = Form(0, ge=0),
    margin_pct: float = Form(0, ge=0),
    margin_floor_pct: float = Form(0, ge=0),
):
    with Session(engine) as session:
        if not role_at_least(current_user(request, session), "manager"):
            return _forbidden()
        _set_param(session, "export_clearance", export_clearance, "USD/shipment")
        _set_param(session, "coo_fee", coo_fee, "USD/shipment")
        _set_param(session, "insurance_pct", insurance_pct, "%")
        _set_param(session, "financing_pct", financing_pct, "%")
        _set_param(session, "margin_pct", margin_pct, "%")
        _set_param(session, "margin_floor_pct", margin_floor_pct, "%")
        session.commit()
    return RedirectResponse("/rates", status_code=303)


@app.post("/rates/cards")
def update_cards(
    request: Request,
    inland_per_truck: float = Form(0, ge=0),
    intl_per_truck: float = Form(0, ge=0),
    truck_capacity: float = Form(25, ge=1),
    dest_border: str = Form(""),
):
    with Session(engine) as session:
        if not role_at_least(current_user(request, session), "manager"):
            return _forbidden()
        _set_card(session, "inland", inland_per_truck, capacity=truck_capacity)
        _set_card(session, "international", intl_per_truck, lane_to=dest_border, capacity=truck_capacity)
        session.commit()
    return RedirectResponse("/rates", status_code=303)


@app.post("/rates/fx")
def update_fx(request: Request, base: str = Form(...), rate: float = Form(..., gt=0)):
    with Session(engine) as session:
        if not role_at_least(current_user(request, session), "manager"):
            return _forbidden()
        base = base.strip().upper()
        fx = session.exec(select(FxRate).where(FxRate.base == base, FxRate.quote == "USD")).first()
        if fx is None:
            fx = FxRate(base=base, quote="USD")
        fx.rate = rate
        session.add(fx)
        session.commit()
    return RedirectResponse("/rates", status_code=303)
