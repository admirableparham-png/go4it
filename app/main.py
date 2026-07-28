"""go4it web app — catalog + buyer leads + matching + quotation."""
import hashlib
import json
import logging
from datetime import datetime
from pathlib import Path

from fastapi import FastAPI, File, Form, Request, UploadFile
from fastapi.responses import HTMLResponse, PlainTextResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlmodel import Session, select

from .config import MATCH_THRESHOLD
from .csv_import import parse_products
from .db import engine, init_db
from .matching import score_lead_product
from .models import CostParam, FxRate, Lead, Match, Product, Quote, RateCard, Supplier
from .quote_service import create_quote
from .telegram import notify_lead_matches

logger = logging.getLogger("go4it")
BASE_DIR = Path(__file__).parent
app = FastAPI(title="go4it")
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))
app.mount("/static", StaticFiles(directory=str(BASE_DIR / "static")), name="static")

SAMPLE_CSV = (
    "name,category,spec,hs_code,exw_price,currency,unit,weight_kg_per_unit,"
    "cbm_per_unit,packaging,min_order_qty,origin_region,supplier\n"
    "Steel rebar 12mm,metals,A3 / B500B,7214,590,USD,ton,1000,0.13,bundled,25,Isfahan,Isfahan Steel Co\n"
    "Portland cement 42.5,construction,Type II,2523,55,USD,ton,1000,0.7,50kg bags,100,Tehran,Tehran Cement\n"
    "Bitumen 60/70,petrochemicals,penetration 60/70,2713,380,USD,ton,1000,1.0,steel drums,20,Tabriz,Pasargad Oil\n"
)


@app.on_event("startup")
def on_startup() -> None:
    init_db()


# ----------------------------------------------------------------------------- helpers

def _content_hash(lead: Lead) -> str:
    parts = [lead.product, lead.category, lead.spec, lead.quantity, lead.unit,
             lead.target_price, lead.currency, lead.dest_country,
             lead.buyer_company, lead.contact_name, lead.email]
    key = "|".join(str(p).strip().lower() for p in parts)
    return hashlib.sha1(key.encode("utf-8")).hexdigest()


def _match_lead(session, lead: Lead):
    """Score a new lead against the active catalog, persist matches, alert once,
    and auto-draft a quote for the best match."""
    saved = []
    for product in session.exec(select(Product).where(Product.active == True)).all():  # noqa: E712
        score, reasons = score_lead_product(lead, product)
        if score >= MATCH_THRESHOLD:
            session.add(Match(lead_id=lead.id, product_id=product.id,
                              score=score, reasons=reasons))
            saved.append((product, score, reasons))
    session.commit()
    if saved:
        saved.sort(key=lambda t: t[1], reverse=True)
        notify_lead_matches(lead, saved[:5])
        try:
            create_quote(session, lead, saved[0][0])   # draft quote for top match
        except Exception:
            logger.warning("auto-quote failed for lead %s", lead.id, exc_info=True)
    return saved


def _get_or_create_supplier(session, name: str):
    name = (name or "").strip()
    if not name:
        return None
    norm = name.lower()
    supplier = session.exec(
        select(Supplier).where(Supplier.name_normalized == norm)
    ).first()
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


# ----------------------------------------------------------------------------- dashboard

@app.get("/", response_class=HTMLResponse)
def dashboard(request: Request):
    with Session(engine) as session:
        leads = session.exec(select(Lead).order_by(Lead.id.desc())).all()
        products = {p.id: p for p in session.exec(select(Product)).all()}
        quotes = session.exec(select(Quote).order_by(Quote.id.desc())).all()
        quotes_by_lead = {}
        for q in quotes:
            quotes_by_lead.setdefault(q.lead_id, []).append(q)
        lead_rows = []
        for lead in leads:
            ms = session.exec(
                select(Match).where(Match.lead_id == lead.id).order_by(Match.score.desc())
            ).all()
            lead_rows.append({
                "lead": lead,
                "matches": [{"m": m, "product": products.get(m.product_id)} for m in ms],
                "quotes": quotes_by_lead.get(lead.id, []),
            })
        product_count = len(products)
    return templates.TemplateResponse(
        "index.html",
        {"request": request, "lead_rows": lead_rows, "product_count": product_count},
    )


@app.post("/leads")
def add_lead(
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
        lead = Lead(
            product=product, category=category, spec=spec, quantity=quantity,
            unit=unit, target_price=target_price, currency=currency,
            dest_country=dest_country.strip().upper(), dest_city=dest_city,
            buyer_company=buyer_company, contact_name=contact_name,
            email=email, phone=phone, notes=notes, source="manual",
        )
        lead.content_hash = _content_hash(lead)
        dup = session.exec(select(Lead).where(Lead.content_hash == lead.content_hash)).first()
        if dup is None:
            session.add(lead)
            session.commit()
            session.refresh(lead)
            lead.tracking_code = f"G4-{datetime.utcnow():%Y%m}-{lead.id:04d}"
            session.add(lead)
            session.commit()
            session.refresh(lead)
            _match_lead(session, lead)
    return RedirectResponse("/", status_code=303)


# ----------------------------------------------------------------------------- catalog

@app.get("/catalog", response_class=HTMLResponse)
def catalog(request: Request, imported: int = 0, updated: int = 0, errors: int = 0):
    with Session(engine) as session:
        products = session.exec(select(Product).order_by(Product.id.desc())).all()
        suppliers = {s.id: s for s in session.exec(select(Supplier)).all()}
    return templates.TemplateResponse(
        "catalog.html",
        {"request": request, "products": products, "suppliers": suppliers,
         "imported": imported, "updated": updated, "errors": errors},
    )


@app.post("/catalog/products")
def add_product(
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
def import_catalog(file: UploadFile = File(...)):
    text = file.file.read().decode("utf-8-sig", errors="replace")
    rows, errors = parse_products(text)
    imported = updated = 0
    fields = ("category", "spec", "hs_code", "exw_price", "currency", "unit",
              "weight_kg_per_unit", "cbm_per_unit", "packaging", "min_order_qty",
              "origin_region")
    with Session(engine) as session:
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
        f"/catalog?imported={imported}&updated={updated}&errors={len(errors)}",
        status_code=303,
    )


@app.get("/catalog/sample.csv", response_class=PlainTextResponse)
def sample_csv():
    return PlainTextResponse(
        SAMPLE_CSV,
        headers={"Content-Disposition": "attachment; filename=go4it_products_sample.csv"},
    )


# ----------------------------------------------------------------------------- quotes

@app.post("/leads/{lead_id}/quote/{product_id}")
def quote_match(lead_id: int, product_id: int):
    with Session(engine) as session:
        lead = session.get(Lead, lead_id)
        product = session.get(Product, product_id)
        if not lead or not product:
            return HTMLResponse("Not found", status_code=404)
        quote = create_quote(session, lead, product)
        quote_id = quote.id
    return RedirectResponse(f"/quotes/{quote_id}", status_code=303)


@app.get("/quotes", response_class=HTMLResponse)
def quotes_list(request: Request):
    with Session(engine) as session:
        quotes = session.exec(select(Quote).order_by(Quote.id.desc())).all()
        leads = {l.id: l for l in session.exec(select(Lead)).all()}
        products = {p.id: p for p in session.exec(select(Product)).all()}
        rows = [{"q": q, "lead": leads.get(q.lead_id), "product": products.get(q.product_id)}
                for q in quotes]
    return templates.TemplateResponse("quotes_list.html", {"request": request, "rows": rows})


@app.get("/quotes/{quote_id}", response_class=HTMLResponse)
def quote_detail(request: Request, quote_id: int):
    with Session(engine) as session:
        q = session.get(Quote, quote_id)
        if not q:
            return HTMLResponse("Not found", status_code=404)
        lead = session.get(Lead, q.lead_id)
        product = session.get(Product, q.product_id)
        breakdown = json.loads(q.breakdown or "[]")
        fx = json.loads(q.fx_snapshot or "{}")
    return templates.TemplateResponse(
        "quote_detail.html",
        {"request": request, "q": q, "lead": lead, "product": product,
         "breakdown": breakdown, "fx": fx},
    )


@app.post("/quotes/{quote_id}/approve")
def approve_quote(quote_id: int):
    with Session(engine) as session:
        q = session.get(Quote, quote_id)
        if q and q.status == "draft":
            q.status = "approved"
            q.approved_by = "manager"
            session.add(q)
            session.commit()
    return RedirectResponse(f"/quotes/{quote_id}", status_code=303)


@app.post("/quotes/{quote_id}/send")
def send_quote(quote_id: int):
    with Session(engine) as session:
        q = session.get(Quote, quote_id)
        if q and q.status == "approved":
            q.status = "sent"
            session.add(q)
            lead = session.get(Lead, q.lead_id)
            if lead and lead.status == "new":
                lead.status = "quoted"
                session.add(lead)
            session.commit()
    return RedirectResponse(f"/quotes/{quote_id}", status_code=303)


# ----------------------------------------------------------------------------- rates admin

@app.get("/rates", response_class=HTMLResponse)
def rates_page(request: Request):
    with Session(engine) as session:
        pvals = {cp.key: cp.value for cp in session.exec(select(CostParam)).all()}
        inland = session.exec(
            select(RateCard).where(RateCard.leg == "inland", RateCard.active == True)  # noqa: E712
        ).first()
        intl = session.exec(
            select(RateCard).where(RateCard.leg == "international", RateCard.active == True)  # noqa: E712
        ).first()
        fxs = session.exec(select(FxRate)).all()
    return templates.TemplateResponse(
        "rates.html",
        {"request": request, "p": pvals, "inland": inland, "intl": intl, "fxs": fxs},
    )


@app.post("/rates/params")
def update_params(
    export_clearance: float = Form(0, ge=0),
    coo_fee: float = Form(0, ge=0),
    insurance_pct: float = Form(0, ge=0),
    financing_pct: float = Form(0, ge=0),
    margin_pct: float = Form(0, ge=0),
    margin_floor_pct: float = Form(0, ge=0),
):
    with Session(engine) as session:
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
    inland_per_truck: float = Form(0, ge=0),
    intl_per_truck: float = Form(0, ge=0),
    truck_capacity: float = Form(25, ge=1),
    dest_border: str = Form(""),
):
    with Session(engine) as session:
        _set_card(session, "inland", inland_per_truck, capacity=truck_capacity)
        _set_card(session, "international", intl_per_truck, lane_to=dest_border,
                  capacity=truck_capacity)
        session.commit()
    return RedirectResponse("/rates", status_code=303)


@app.post("/rates/fx")
def update_fx(base: str = Form(...), rate: float = Form(..., gt=0)):
    with Session(engine) as session:
        base = base.strip().upper()
        fx = session.exec(
            select(FxRate).where(FxRate.base == base, FxRate.quote == "USD")
        ).first()
        if fx is None:
            fx = FxRate(base=base, quote="USD")
        fx.rate = rate
        session.add(fx)
        session.commit()
    return RedirectResponse("/rates", status_code=303)
