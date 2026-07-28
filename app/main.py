"""go4it web app — catalog + buyer leads + geography-aware matching."""
import hashlib
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
from .models import Lead, Match, Product, Supplier
from .telegram import notify_lead_matches

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
    """Stable hash of a lead's identifying content, for dedup."""
    parts = [lead.product, lead.category, lead.spec, lead.quantity, lead.unit,
             lead.target_price, lead.currency, lead.dest_country,
             lead.buyer_company, lead.contact_name, lead.email]
    key = "|".join(str(p).strip().lower() for p in parts)
    return hashlib.sha1(key.encode("utf-8")).hexdigest()


def _tracking_code(session, lead_id: int) -> str:
    return f"G4-{datetime.utcnow():%Y%m}-{lead_id:04d}"


def _match_lead(session, lead: Lead):
    """Score a new lead against the active catalog, persist matches, alert once."""
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


# ----------------------------------------------------------------------------- dashboard

@app.get("/", response_class=HTMLResponse)
def dashboard(request: Request):
    with Session(engine) as session:
        leads = session.exec(select(Lead).order_by(Lead.id.desc())).all()
        products = {p.id: p for p in session.exec(select(Product)).all()}
        lead_rows = []
        for lead in leads:
            ms = session.exec(
                select(Match).where(Match.lead_id == lead.id).order_by(Match.score.desc())
            ).all()
            lead_rows.append({
                "lead": lead,
                "matches": [{"m": m, "product": products.get(m.product_id)} for m in ms],
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
        # Skip an identical re-submission (double-click, retry).
        dup = session.exec(
            select(Lead).where(Lead.content_hash == lead.content_hash)
        ).first()
        if dup is None:
            session.add(lead)
            session.commit()
            session.refresh(lead)
            lead.tracking_code = _tracking_code(session, lead.id)
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
        {
            "request": request, "products": products, "suppliers": suppliers,
            "imported": imported, "updated": updated, "errors": errors,
        },
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
        product = Product(
            name=name, category=category, spec=spec, hs_code=hs_code,
            exw_price=exw_price, currency=currency, unit=unit,
            weight_kg_per_unit=weight_kg_per_unit, cbm_per_unit=cbm_per_unit,
            packaging=packaging, min_order_qty=min_order_qty,
            origin_region=origin_region,
            supplier_id=sup.id if sup else None,
        )
        session.add(product)
        session.commit()
    return RedirectResponse("/catalog", status_code=303)


@app.post("/catalog/import")
def import_catalog(file: UploadFile = File(...)):
    text = file.file.read().decode("utf-8-sig", errors="replace")
    rows, errors = parse_products(text)
    imported = updated = 0
    _PRODUCT_FIELDS = ("category", "spec", "hs_code", "exw_price", "currency",
                       "unit", "weight_kg_per_unit", "cbm_per_unit", "packaging",
                       "min_order_qty", "origin_region")
    with Session(engine) as session:
        for rec in rows:
            sup = _get_or_create_supplier(session, rec.get("supplier", ""))
            existing = session.exec(
                select(Product).where(Product.name == rec["name"])
            ).first()
            product = existing or Product(name=rec["name"])
            for field in _PRODUCT_FIELDS:
                if field in rec:
                    setattr(product, field, rec[field])
            if sup:
                product.supplier_id = sup.id
            product.updated_at = datetime.utcnow()
            session.add(product)
            if existing:
                updated += 1
            else:
                imported += 1
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
