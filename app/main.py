"""go4it web app — catalog + leads + matching + quotation + team CRM."""
import hmac
import json
import logging
import os
from datetime import datetime, timedelta
from pathlib import Path
from typing import List

from fastapi import (BackgroundTasks, FastAPI, File, Form, Header, Request,
                     UploadFile)
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import (FileResponse, HTMLResponse, JSONResponse,
                               PlainTextResponse, RedirectResponse)
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel
from sqlalchemy import func
from sqlmodel import Session, select
from starlette.middleware.sessions import SessionMiddleware

from .auth import current_user, role_at_least, verify_password
from .command_service import parse_command, run_command_job
from .config import DEBUG_DIR, INBOX_DIR, INGEST_API_KEY, SECRET_KEY, SMTP_ENABLED
from .csv_import import parse_products
from .db import engine, init_db
from .deal_service import (DEAL_STAGES, DOC_TYPES, REQUIRED_DOCS, create_deal,
                           missing_docs_for, next_stage)
from .ingest import ingest_source
from .lead_service import create_lead, run_matching
from .models import (Activity, CommandJob, ComplianceDoc, CostParam, Deal,
                     FxRate, IngestionRun, Lead, Match, Outreach, Product,
                     Quote, RateCard, Supplier, User)
from .outreach import default_message, send_email
from .quote_service import create_quote
from .research_engine import (PARTNERS, country_options, market_report,
                              product_options, rank_opportunities, resolve_query)
from .sources.go4world_csv import Go4WorldCsvSource
from .telegram import notify_quote_ready, notify_status_change

logger = logging.getLogger("go4it")
BASE_DIR = Path(__file__).parent
app = FastAPI(title="go4it")
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))
app.mount("/static", StaticFiles(directory=str(BASE_DIR / "static")), name="static")

PUBLIC_PREFIXES = ("/login", "/logout", "/static", "/api", "/go4it-capture.user.js")

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


# SessionMiddleware is added before CORS/PNA below, so it stays OUTSIDE auth_gate
# (request.session is ready) but INSIDE the CORS layer.
app.add_middleware(SessionMiddleware, secret_key=SECRET_KEY, same_site="lax")

# --- Reachability for the in-browser capture helper --------------------------
# The Tampermonkey helper runs on https://www.go4worldbusiness.com and POSTs to
# http://localhost:8400. Chromium (Brave/Chrome) treats http://localhost as
# trustworthy, but it still requires (a) CORS headers and (b) a Private Network
# Access opt-in on the preflight. Without both, the browser silently drops the
# request and the helper panel shows "can't reach go4it". Auth on the capture
# endpoints is the X-API-Key header (not the cookie), so opening CORS here is
# safe: a caller still needs the key, and the server only listens on localhost.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
    max_age=3600,
)


@app.middleware("http")
async def private_network_access(request: Request, call_next):
    """Echo back Chrome/Brave's Private Network Access preflight opt-in so a
    public https page is allowed to reach this local server. Outermost layer, so
    it tags even the CORS preflight response on the way out."""
    resp = await call_next(request)
    if request.headers.get("access-control-request-private-network"):
        resp.headers["Access-Control-Allow-Private-Network"] = "true"
    return resp


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
    if kind in ("note", "call", "status_change", "quote_sent", "outreach") and lead.first_response_at is None:
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


def _bars(counts: dict, top=None, drop_empty=False):
    """Turn a {label: count} dict into sorted bar rows with a 0-100 width pct."""
    items = [(k or "—", v) for k, v in counts.items() if not (drop_empty and not k)]
    items.sort(key=lambda x: -x[1])
    if top:
        items = items[:top]
    mx = max((v for _, v in items), default=1) or 1
    return [{"label": k, "count": v, "pct": round(v / mx * 100)} for k, v in items]


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
    """Analytics overview: KPIs + distributions + recent activity, all via
    COUNT/GROUP-BY aggregates (never loads every lead — the full list lives on /leads)."""
    with Session(engine) as session:
        user = current_user(request, session)

        def group(col):
            return {k: v for k, v in session.exec(select(col, func.count(Lead.id)).group_by(col)).all()}

        by_stage = group(Lead.status)
        by_source = group(Lead.source)
        by_dest = group(Lead.dest_country)
        by_cat = group(Lead.category)
        total = sum(by_stage.values())

        won, lost = by_stage.get("won", 0), by_stage.get("lost", 0)
        win_rate = round(won / (won + lost) * 100) if (won + lost) else None
        open_pipeline = (by_stage.get("new", 0) + by_stage.get("quoted", 0)
                         + by_stage.get("negotiating", 0))
        needs_enrichment = session.exec(
            select(func.count(Lead.id)).where(
                ((Lead.email == None) | (Lead.email == "")) &        # noqa: E711
                ((Lead.phone == None) | (Lead.phone == "")))         # noqa: E711
        ).one()

        qcounts = {k: v for k, v in session.exec(
            select(Quote.status, func.count(Quote.id)).group_by(Quote.status)).all()}
        deals_total = session.exec(select(func.count(Deal.id))).one()
        deals_open = session.exec(
            select(func.count(Deal.id)).where(Deal.closed_at == None)).one()   # noqa: E711
        products_total = session.exec(select(func.count(Product.id))).one()

        # Pipeline value: best quote per lead still in play (quoted/negotiating).
        active_ids = session.exec(
            select(Lead.id).where(Lead.status.in_(["quoted", "negotiating"]))).all()
        pipeline_value = 0.0
        if active_ids:
            rows = session.exec(
                select(func.max(Quote.delivered_total)).where(
                    Quote.lead_id.in_(active_ids)).group_by(Quote.lead_id)).all()
            pipeline_value = sum((r or 0) for r in rows)

        acts = session.exec(select(Activity).order_by(Activity.id.desc()).limit(10)).all()
        lead_map = {ld.id: ld for ld in session.exec(
            select(Lead).where(Lead.id.in_([a.lead_id for a in acts] or [0]))).all()}
        user_map = {u.id: u for u in session.exec(select(User)).all()}
        recent_leads = session.exec(select(Lead).order_by(Lead.id.desc()).limit(8)).all()
        due_cut = datetime.utcnow().replace(hour=23, minute=59, second=59, microsecond=0)
        due_leads = session.exec(
            select(Lead).where(Lead.next_action_at != None,             # noqa: E711
                               Lead.next_action_at <= due_cut,
                               Lead.status.notin_(["won", "lost"]))
            .order_by(Lead.next_action_at.asc()).limit(10)).all()

        ctx = {
            "request": request, "user": user, "active": "dashboard",
            "total": total, "contactable": total - needs_enrichment,
            "needs_enrichment": needs_enrichment,
            "open_pipeline": open_pipeline, "won": won, "lost": lost, "win_rate": win_rate,
            "quotes_total": sum(qcounts.values()), "quotes_sent": qcounts.get("sent", 0),
            "quotes_draft": qcounts.get("draft", 0),
            "deals_total": deals_total, "deals_open": deals_open,
            "products_total": products_total, "pipeline_value": pipeline_value,
            "stage_bars": _bars(by_stage),
            "source_bars": _bars(by_source, top=8),
            "dest_bars": _bars(by_dest, top=8, drop_empty=True),
            "cat_bars": _bars(by_cat, top=8, drop_empty=True),
            "acts": acts, "lead_map": lead_map, "user_map": user_map,
            "recent_leads": recent_leads, "market_cards": _intel_cards(),
            "due_leads": due_leads, "today": datetime.utcnow().date(),
        }
    return templates.TemplateResponse("index.html", ctx)


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
    return RedirectResponse("/leads", status_code=303)


LEAD_SORTS = {
    "new": Lead.id.desc(), "old": Lead.id.asc(),
    "buyer": Lead.buyer_company.asc(), "product": Lead.product.asc(),
    "posted": Lead.posted_at.desc(),   # freshest RFQ/customs date first (NULLs last in SQLite)
}


@app.get("/leads", response_class=HTMLResponse)
def leads_list(request: Request, q: str = "", stage: str = "", source: str = "",
               category: str = "", dest: str = "", owner: str = "", contact: str = "",
               due: str = "", sort: str = "new", page: int = 1):
    """The dedicated, filterable, paginated lead workspace (the dashboard no longer
    lists every lead)."""
    per = 50
    with Session(engine) as session:
        user = current_user(request, session)
        stmt = select(Lead)
        if q:
            like = f"%{q.strip()}%"
            stmt = stmt.where(Lead.product.ilike(like) | Lead.buyer_company.ilike(like)
                              | Lead.tracking_code.ilike(like))
        if stage:
            stmt = stmt.where(Lead.status == stage)
        if source:
            stmt = stmt.where(Lead.source == source)
        if category:
            stmt = stmt.where(Lead.category == category)
        if dest:
            stmt = stmt.where(Lead.dest_country == dest)
        if owner == "me" and user:
            stmt = stmt.where(Lead.owner_id == user.id)
        elif owner == "none":
            stmt = stmt.where(Lead.owner_id == None)                    # noqa: E711
        if contact == "yes":
            stmt = stmt.where((Lead.email != "") | (Lead.phone != ""))
        elif contact == "no":
            stmt = stmt.where(((Lead.email == None) | (Lead.email == ""))    # noqa: E711
                              & ((Lead.phone == None) | (Lead.phone == "")))  # noqa: E711
        if due:
            _now = datetime.utcnow()
            stmt = stmt.where(Lead.next_action_at != None)                    # noqa: E711
            if due == "overdue":
                stmt = stmt.where(Lead.next_action_at < _now.replace(hour=0, minute=0, second=0, microsecond=0))
            elif due == "today":
                stmt = stmt.where(Lead.next_action_at <= _now.replace(hour=23, minute=59, second=59, microsecond=0))

        total = session.exec(select(func.count()).select_from(stmt.subquery())).one()
        pages = max(1, (total + per - 1) // per)
        page = min(max(1, page), pages)
        order = LEAD_SORTS.get(sort, LEAD_SORTS["new"])
        leads = session.exec(stmt.order_by(order).offset((page - 1) * per).limit(per)).all()

        ids = [ld.id for ld in leads] or [0]
        mcounts = {lid: c for lid, c in session.exec(
            select(Match.lead_id, func.count(Match.id)).where(
                Match.lead_id.in_(ids)).group_by(Match.lead_id)).all()}
        user_map = {u.id: u for u in session.exec(select(User)).all()}
        sources = sorted({s for s in session.exec(select(Lead.source).distinct()).all() if s})
        categories = sorted({c for c in session.exec(select(Lead.category).distinct()).all() if c})
        dests = sorted({d for d in session.exec(select(Lead.dest_country).distinct()).all() if d})
        product_count = session.exec(select(func.count(Product.id))).one()

        ctx = {
            "request": request, "user": user, "active": "leads",
            "leads": leads, "mcounts": mcounts, "user_map": user_map,
            "total": total, "page": page, "pages": pages,
            "sources": sources, "categories": categories, "dests": dests,
            "stages": STAGES, "product_count": product_count,
            "users": [u for u in user_map.values() if u.active],
            "f": {"q": q, "stage": stage, "source": source, "category": category,
                  "dest": dest, "owner": owner, "contact": contact, "due": due, "sort": sort},
        }
    return templates.TemplateResponse("leads.html", ctx)


@app.post("/leads/bulk")
def leads_bulk(request: Request, action: str = Form(""), owner_id: str = Form(""),
               stage: str = Form(""), ids: List[int] = Form(default=[])):
    """Apply one action (assign / set stage / set-or-clear follow-up) to many selected leads."""
    with Session(engine) as session:
        user = current_user(request, session)
        if not role_at_least(user, "agent"):
            return _forbidden()
        leads = session.exec(select(Lead).where(Lead.id.in_(ids or [0]))).all()
        for lead in leads:
            if action == "assign_me" and user:
                lead.owner_id = user.id
            elif action == "assign":
                lead.owner_id = int(owner_id) if owner_id else None
            elif action == "stage" and stage in STAGES and stage != "lost":
                lead.status = stage
            elif action == "followup_today":
                lead.next_action_at = datetime.utcnow()
            elif action == "followup_week":
                lead.next_action_at = datetime.utcnow() + timedelta(days=7)
            elif action == "followup_clear":
                lead.next_action_at, lead.next_action_note = None, ""
            session.add(lead)
        session.commit()
    return RedirectResponse(request.headers.get("referer") or "/leads", status_code=303)


# ----------------------------------------------------------------------------- suppliers + intel

@app.get("/suppliers", response_class=HTMLResponse)
def suppliers_list(request: Request):
    """Simple view of the Supplier catalog (auto-created today with no page)."""
    with Session(engine) as session:
        user = current_user(request, session)
        suppliers = session.exec(select(Supplier).order_by(Supplier.country, Supplier.name)).all()
        pcounts = {sid: c for sid, c in session.exec(
            select(Product.supplier_id, func.count(Product.id)).group_by(Product.supplier_id)).all()}
        ctx = {"request": request, "user": user, "active": "suppliers",
               "suppliers": suppliers, "pcounts": pcounts}
    return templates.TemplateResponse("suppliers.html", ctx)


RESEARCH_DIR = BASE_DIR.parent / "docs" / "research"
_HS_PKEY = {"2715": "cold-asphalt", "4016": "rubber-tiles", "4004": "pour-in-place-rubber"}
_PROD_META = {
    "cold-asphalt": ("Cold asphalt (bagged cold-mix)", "HS 2715"),
    "rubber-tiles": ("Rubber tiles (gym / outdoor)", "HS 4016"),
    "pour-in-place-rubber": ("Pour-in-place rubber flooring", "HS 4004"),
}


def _load_research(name):
    p = RESEARCH_DIR / name
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return {}


def _intel_cards():
    """Build the good/watch/bad market verdict cards from committed customs data (no fetch).
    Used by both the dashboard 'market opportunities' strip and the /intel tab."""
    stats = _load_research("trade_stats_georgia.json")
    by_hs = {c["hs_code"]: c for c in stats.get("commodities", [])}
    cards = []
    for hs, pkey in _HS_PKEY.items():
        c = by_hs.get(hs)
        if not c:
            continue
        name, hslabel = _PROD_META[pkey]
        yrs = c.get("years", {})
        last = max(yrs) if yrs else None
        latest = yrs.get(last, {}) if last else {}
        cheapest = c.get("cheapest_source") or {}
        iran = c.get("iran_present")
        if iran and cheapest.get("country") == "Iran":
            verdict, tone = "Iran is already the CHEAPEST supplier to Georgia — proven live lane.", "good"
        elif iran:
            verdict, tone = "Iran already present and competitive.", "good"
        else:
            verdict, tone = "Iran absent; cheap regional bulk owns it — marginal for us.", "bad"
        cards.append({
            "name": name, "hs": hslabel, "tone": tone, "verdict": verdict,
            "year": last, "tonnes": latest.get("tonnes"), "cif": latest.get("cif_usd"),
            "price": latest.get("unit_price_usd_kg"), "trend": c.get("trend_pct_first_to_last"),
            "cheapest": cheapest, "iran": iran,
        })
    return cards


@app.get("/intel", response_class=HTMLResponse)
def intel(request: Request):
    """Surface the harvested market intelligence in-app (customs market reality,
    live Georgian tenders, gym/venue demand, UAE supply) — read-only, no fetching."""
    with Session(engine) as session:
        user = current_user(request, session)

    tenders = _load_research("ge_tenders.json")
    businesses = _load_research("ge_businesses.json")
    suppliers = _load_research("suppliers_uae_iran.json")
    cards = _intel_cards()

    biz = [b for b in businesses.get("businesses", []) if b.get("product_key") == "rubber-tiles"]
    biz_contact = [b for b in biz if not b.get("needs_enrichment")]
    uae = [s for s in suppliers.get("suppliers", []) if s.get("country") == "AE"]
    ctx = {
        "request": request, "user": user, "active": "intel",
        "cards": cards, "tenders": tenders.get("tenders", []),
        "biz_total": len(biz), "biz_contact": biz_contact[:24],
        "uae": uae, "has_data": bool(cards or tenders.get("tenders") or uae),
    }
    return templates.TemplateResponse("intel.html", ctx)


@app.get("/georgia", response_class=HTMLResponse)
def georgia(request: Request):
    """Georgia chemical-buyer research (Section A potential buyers + Section B live
    procurement RFQs + customs), read from the committed harvest JSONs."""
    with Session(engine) as session:
        user = current_user(request, session)
    buyers = _load_research("ge_chem_buyers.json").get("buyers", [])
    tenders = _load_research("ge_chem_rfqs.json").get("tenders", [])
    customs = _load_research("ge_customs.json").get("records", [])
    ctx = {
        "request": request, "user": user, "active": "georgia",
        "buyers": buyers,
        "anchors": [b for b in buyers if b.get("source_tier") == "anchor"],
        "directory": [b for b in buyers if b.get("source_tier") != "anchor"],
        "with_contact": sum(1 for b in buyers if not b.get("needs_enrichment")),
        "tenders": tenders, "open_rfq": sum(1 for t in tenders if t.get("open")),
        "customs": customs,
        "has_data": bool(buyers or tenders or customs),
    }
    return templates.TemplateResponse("georgia.html", ctx)


@app.get("/uae", response_class=HTMLResponse)
def uae(request: Request):
    """UAE buyers for the Decora Store home-decor range (what we offer + who buys it)."""
    with Session(engine) as session:
        user = current_user(request, session)
    data = _load_research("uae_decor_buyers.json")
    prod = _load_research("decora_products.json")
    buyers = data.get("buyers", [])
    order = ["Home-decor retailer", "Tableware / serveware", "Crockery", "Housewares",
             "Handicrafts", "Lighting shop", "Gift / corporate-gift", "Gift shop",
             "Hotel & hospitality supplier"]
    groups = {}
    for b in buyers:
        groups.setdefault((b.get("categories") or ["Other"])[0], []).append(b)
    for c in groups:
        groups[c].sort(key=lambda x: -x.get("match_score", 0))
    grouped = ([(c, groups[c]) for c in order if c in groups]
               + [(c, groups[c]) for c in groups if c not in order])
    ranked = sorted([b for b in buyers if b.get("match_score", 0) >= 75],
                    key=lambda x: -x.get("match_score", 0))
    rfqs = _load_research("uae_decor_rfqs.json").get("rfqs", [])
    ctx = {
        "request": request, "user": user, "active": "uae",
        "store": prod.get("store", {}), "families": prod.get("families", []),
        "buyers": buyers, "grouped": grouped,
        "ranked": ranked[:60], "high_n": len(ranked), "rfqs": rfqs,
        "with_phone": data.get("with_phone", 0), "with_email": data.get("with_email", 0),
        "with_website": data.get("with_website", 0), "has_data": bool(buyers),
    }
    return templates.TemplateResponse("uae.html", ctx)


# ----------------------------------------------------------------------------- research console

# M49 reporter code -> ISO2, for cross-referencing buyers we've already harvested in a market.
M49_ISO = {
    268: "GE", 784: "AE", 364: "IR", 792: "TR", 634: "QA", 682: "SA", 51: "AM", 31: "AZ",
    398: "KZ", 860: "UZ", 795: "TM", 368: "IQ", 4: "AF", 643: "RU", 156: "CN", 804: "UA",
    414: "KW", 512: "OM", 48: "BH", 400: "JO", 422: "LB", 818: "EG",
}


def _sources_tuple(sources):
    return {"iran": (364,), "uae": (784,), "both": (364, 784)}.get(sources, (364, 784))


def _our_buyers(session, code):
    """Buyers we've already harvested for this destination market (ties research -> pipeline)."""
    iso = M49_ISO.get(code, "")
    if not iso:
        return [], 0
    rows = session.exec(select(Lead).where(Lead.dest_country == iso)
                        .order_by(Lead.posted_at.desc(), Lead.id.desc())).all()
    return rows[:15], len(rows)


@app.get("/research", response_class=HTMLResponse)
def research(request: Request):
    """The Research console: analyse any product -> destination country from REAL UN Comtrade
    customs data (no LLM), and rank a country's best import opportunities for Iran/UAE supply."""
    with Session(engine) as session:
        user = current_user(request, session)
    ctx = {"request": request, "user": user, "active": "research",
           "countries": country_options(), "products": product_options(),
           "default_reporter": 268}
    return templates.TemplateResponse("research.html", ctx)


@app.post("/research/run", response_class=HTMLResponse)
def research_run(request: Request, reporter: str = Form(...), product: str = Form(""),
                 hs: str = Form(""), sources: str = Form("both"), refresh: str = Form("")):
    """Directional report for one product into one country (HTMX fragment)."""
    label, hs_codes, _ = resolve_query(product, hs)
    if not hs_codes:
        return templates.TemplateResponse("partials/research_result.html",
            {"request": request, "error": "Pick a product from the list or type an HS code."})
    try:
        code = int(reporter)
    except ValueError:
        code = 0
    if code == 0 or code not in PARTNERS:
        return templates.TemplateResponse("partials/research_result.html",
            {"request": request, "error": "That destination country isn't recognized."})
    report = market_report(code, hs_codes, our_sources=_sources_tuple(sources),
                           refresh=bool(refresh))
    with Session(engine) as session:
        current_user(request, session)
        our_leads, our_lead_n = _our_buyers(session, code)
        ctx = {"request": request, "report": report, "query_label": label,
               "our_leads": our_leads, "our_lead_n": our_lead_n,
               "our_iso": M49_ISO.get(code, "")}
    return templates.TemplateResponse("partials/research_result.html", ctx)


@app.post("/research/scan", response_class=HTMLResponse)
def research_scan(request: Request, reporter: str = Form(...), sources: str = Form("both"),
                  refresh: str = Form("")):
    """Rank a country's best import opportunities for Iran/UAE supply (HTMX fragment)."""
    try:
        code = int(reporter)
    except ValueError:
        code = 0
    if code == 0 or code not in PARTNERS:
        return templates.TemplateResponse("partials/research_result.html",
            {"request": request, "error": "That destination country isn't recognized."})
    rows = rank_opportunities(code, our_sources=_sources_tuple(sources), refresh=bool(refresh))
    with Session(engine) as session:
        current_user(request, session)
        our_leads, our_lead_n = _our_buyers(session, code)
        ctx = {"request": request, "scan": rows, "reporter_name": PARTNERS.get(code, ""),
               "our_leads": our_leads, "our_lead_n": our_lead_n,
               "our_iso": M49_ISO.get(code, "")}
    return templates.TemplateResponse("partials/research_result.html", ctx)


# ----------------------------------------------------------------------------- command box

@app.get("/command", response_class=HTMLResponse)
def command_page(request: Request):
    """The dashboard command box: type what to find, it harvests real buyers into leads."""
    with Session(engine) as session:
        user = current_user(request, session)
        jobs = session.exec(select(CommandJob).order_by(CommandJob.id.desc()).limit(25)).all()
    ctx = {"request": request, "user": user, "active": "command", "jobs": jobs,
           "can_run": role_at_least(user, "agent")}
    return templates.TemplateResponse("command.html", ctx)


@app.post("/command/run", response_class=HTMLResponse)
def command_run(request: Request, background: BackgroundTasks, prompt: str = Form("")):
    """Parse the prompt, create a queued CommandJob, kick off the harvest in the background,
    and return the job card (which self-polls until done)."""
    with Session(engine) as session:
        user = current_user(request, session)
        if not role_at_least(user, "agent"):
            return HTMLResponse('<div class="text-rose-300 text-sm p-2">Agent role required to run commands.</div>',
                                status_code=403)
        prompt = (prompt or "").strip()
        if not prompt:
            return HTMLResponse("", status_code=204)
        parsed = parse_command(prompt)
        job = CommandJob(prompt=prompt[:300], action=parsed["action"],
                         params=json.dumps(parsed), status="queued",
                         note=parsed.get("note", ""), owner_id=user.id)
        session.add(job)
        session.commit()
        session.refresh(job)
        jid = job.id
    background.add_task(run_command_job, jid)
    with Session(engine) as session:
        job = session.get(CommandJob, jid)
        return templates.TemplateResponse("partials/command_job.html", {"request": request, "job": job})


@app.get("/command/{job_id}/status", response_class=HTMLResponse)
def command_status(request: Request, job_id: int):
    """HTMX poll target: re-render the job card (it stops polling once terminal)."""
    with Session(engine) as session:
        current_user(request, session)
        job = session.get(CommandJob, job_id)
        if not job:
            return HTMLResponse("", status_code=404)
        return templates.TemplateResponse("partials/command_job.html", {"request": request, "job": job})


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
        quotable = sorted(
            (p for p in products.values() if _quotable(p)),
            key=lambda p: p.name)
        latest_q = quotes[0] if quotes else None
        latest_p = products.get(latest_q.product_id) if latest_q else None
        default_subject, default_body = default_message(lead, latest_q, latest_p)
        outreach = session.exec(
            select(Outreach).where(Outreach.lead_id == lead_id).order_by(Outreach.id.desc())
        ).all()
    return templates.TemplateResponse(
        "lead_detail.html",
        {"request": request, "user": user, "lead": lead, "owner": owner,
         "users": users, "products": products, "quotable": quotable,
         "matches": [{"m": m, "product": products.get(m.product_id)} for m in matches],
         "quotes": quotes, "timeline": timeline, "outreach": outreach,
         "default_subject": default_subject, "default_body": default_body,
         "smtp_enabled": SMTP_ENABLED, "today": datetime.utcnow().date(),
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
        # A won lead becomes a Deal (once), seeded from its accepted quote.
        if status == "won" and not session.exec(select(Deal).where(Deal.lead_id == lead.id)).first():
            quote = (session.exec(select(Quote).where(Quote.lead_id == lead.id, Quote.status == "sent")
                                  .order_by(Quote.id.desc())).first()
                     or session.exec(select(Quote).where(Quote.lead_id == lead.id)
                                     .order_by(Quote.id.desc())).first())
            deal = create_deal(session, lead, quote)
            _log(session, lead, user, "note", f"deal {deal.tracking_code} opened")
            session.commit()
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

def _quotable(product):
    """A product can be quoted only if it has a real price and shipping weight."""
    return bool(product) and (product.exw_price or 0) > 0 and (product.weight_kg_per_unit or 0) > 0


@app.post("/leads/{lead_id}/quote/{product_id}")
def quote_match(request: Request, lead_id: int, product_id: int):
    with Session(engine) as session:
        if not role_at_least(current_user(request, session), "agent"):
            return _forbidden()
        lead = session.get(Lead, lead_id)
        product = session.get(Product, product_id)
        if not lead or not product:
            return HTMLResponse("Not found", status_code=404)
        if not _quotable(product):
            return RedirectResponse(f"/leads/{lead_id}?error=unpriced", status_code=303)
        quote = create_quote(session, lead, product)
        notify_quote_ready(quote, lead, product)
        quote_id = quote.id
    return RedirectResponse(f"/quotes/{quote_id}", status_code=303)


@app.post("/leads/{lead_id}/quote")
def quote_manual(request: Request, lead_id: int, product_id: int = Form(...)):
    """Quote a lead against ANY chosen catalog product (for the ~2551 leads with no auto-match)."""
    with Session(engine) as session:
        if not role_at_least(current_user(request, session), "agent"):
            return _forbidden()
        lead = session.get(Lead, lead_id)
        product = session.get(Product, product_id)
        if not lead or not product:
            return HTMLResponse("Not found", status_code=404)
        if not _quotable(product):
            return RedirectResponse(f"/leads/{lead_id}?error=unpriced", status_code=303)
        quote = create_quote(session, lead, product)
        notify_quote_ready(quote, lead, product)
        quote_id = quote.id
    return RedirectResponse(f"/quotes/{quote_id}", status_code=303)


@app.post("/leads/{lead_id}/rematch")
def lead_rematch(request: Request, lead_id: int):
    """Re-run catalog matching for one lead (matches only, no auto-quote/alert)."""
    with Session(engine) as session:
        if not role_at_least(current_user(request, session), "agent"):
            return _forbidden()
        lead = session.get(Lead, lead_id)
        if not lead:
            return HTMLResponse("Not found", status_code=404)
        run_matching(session, lead, auto_quote=False)
    return RedirectResponse(f"/leads/{lead_id}", status_code=303)


@app.post("/leads/{lead_id}/outreach")
def lead_outreach(request: Request, lead_id: int, channel: str = Form("email"),
                  recipient: str = Form(""), subject: str = Form(""), body: str = Form(""),
                  send: str = Form("")):
    """Record a buyer contact (and SMTP-send it when 'send' is set + SMTP is configured).
    Stamps first_response_at + adds a timeline entry so outreach + response speed are tracked."""
    with Session(engine) as session:
        user = current_user(request, session)
        if not role_at_least(user, "agent"):
            return _forbidden()
        lead = session.get(Lead, lead_id)
        if not lead:
            return HTMLResponse("Not found", status_code=404)
        status, error = "logged", ""
        if send and channel == "email":
            ok, error = send_email(recipient or lead.email, subject, body)
            status = "sent" if ok else "failed"
        session.add(Outreach(
            lead_id=lead.id, channel=channel,
            recipient=(recipient or lead.email or lead.phone or "")[:200],
            subject=subject[:200], body=body[:4000], status=status, error=error,
            user_id=user.id if user else None))
        _log(session, lead, user, "outreach",
             f"{channel} to {recipient or lead.email or lead.phone or '?'} - {status}")
        session.commit()
    return RedirectResponse(f"/leads/{lead_id}", status_code=303)


@app.post("/leads/{lead_id}/followup")
def lead_followup(request: Request, lead_id: int, next_action_at: str = Form(""),
                  next_action_note: str = Form(""), clear: str = Form("")):
    """Set/clear a follow-up date + note (feeds the 'contact today' queue)."""
    with Session(engine) as session:
        user = current_user(request, session)
        if not role_at_least(user, "agent"):
            return _forbidden()
        lead = session.get(Lead, lead_id)
        if not lead:
            return HTMLResponse("Not found", status_code=404)
        if clear:
            lead.next_action_at, lead.next_action_note = None, ""
            _log(session, lead, user, "note", "follow-up cleared")
        else:
            dt = None
            if next_action_at:
                try:
                    dt = datetime.strptime(next_action_at, "%Y-%m-%d")
                except ValueError:
                    dt = None
            lead.next_action_at = dt
            lead.next_action_note = (next_action_note or "")[:200]
            _log(session, lead, user, "note",
                 f"follow-up {next_action_at or 'set'}: {next_action_note}"[:200])
        session.add(lead)
        session.commit()
    return RedirectResponse(f"/leads/{lead_id}", status_code=303)


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
         "breakdown": breakdown, "fx": fx, "can_approve": role_at_least(user, "agent")},
    )


@app.post("/quotes/{quote_id}/approve")
def approve_quote(request: Request, quote_id: int):
    with Session(engine) as session:
        user = current_user(request, session)
        if not role_at_least(user, "agent"):    # agent+ can approve (solo-operator friendly)
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
        if not role_at_least(user, "agent"):     # agent+ can send (solo-operator friendly)
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


# ----------------------------------------------------------------------------- deals (post-win)

@app.get("/deals", response_class=HTMLResponse)
def deals_list(request: Request):
    with Session(engine) as session:
        user = current_user(request, session)
        deals = session.exec(select(Deal).order_by(Deal.id.desc())).all()
        leads = {l.id: l for l in session.exec(select(Lead)).all()}
        rows = [{"d": d, "lead": leads.get(d.lead_id)} for d in deals]
    return templates.TemplateResponse("deals_list.html", {"request": request, "user": user, "rows": rows})


@app.get("/deals/{deal_id}", response_class=HTMLResponse)
def deal_detail(request: Request, deal_id: int):
    with Session(engine) as session:
        user = current_user(request, session)
        deal = session.get(Deal, deal_id)
        if not deal:
            return HTMLResponse("Not found", status_code=404)
        lead = session.get(Lead, deal.lead_id)
        docs = session.exec(
            select(ComplianceDoc).where(ComplianceDoc.deal_id == deal_id).order_by(ComplianceDoc.id.desc())
        ).all()
        nxt = next_stage(deal.stage)
        missing = missing_docs_for(session, deal, nxt) if nxt else []
    return templates.TemplateResponse(
        "deal_detail.html",
        {"request": request, "user": user, "deal": deal, "lead": lead, "docs": docs,
         "stages": DEAL_STAGES, "next": nxt, "missing": missing,
         "required_for": REQUIRED_DOCS, "doc_types": DOC_TYPES,
         "can_settle": role_at_least(user, "manager"), "today": datetime.utcnow()},
    )


@app.post("/deals/{deal_id}/advance")
def advance_deal(request: Request, deal_id: int):
    with Session(engine) as session:
        if not role_at_least(current_user(request, session), "agent"):
            return _forbidden()
        deal = session.get(Deal, deal_id)
        if not deal:
            return HTMLResponse("Not found", status_code=404)
        nxt = next_stage(deal.stage)
        if not nxt:
            return RedirectResponse(f"/deals/{deal_id}", status_code=303)
        missing = missing_docs_for(session, deal, nxt)
        if missing:      # non-bypassable compliance gate
            return RedirectResponse(f"/deals/{deal_id}?error=docs&need={','.join(missing)}", status_code=303)
        deal.stage = nxt
        deal.updated_at = datetime.utcnow()
        if nxt == "closed":
            deal.closed_at = datetime.utcnow()
        session.add(deal)
        lead = session.get(Lead, deal.lead_id)
        user = current_user(request, session)
        if lead:
            _log(session, lead, user, "status_change", f"deal -> {nxt}")
        session.commit()
    return RedirectResponse(f"/deals/{deal_id}", status_code=303)


@app.post("/deals/{deal_id}/docs")
def add_doc(request: Request, deal_id: int, doc_type: str = Form(...),
            reference_no: str = Form(""), issued_by: str = Form(""), expires_at: str = Form("")):
    with Session(engine) as session:
        if not role_at_least(current_user(request, session), "agent"):
            return _forbidden()
        if not session.get(Deal, deal_id):
            return HTMLResponse("Not found", status_code=404)
        exp = None
        if expires_at.strip():
            try:
                exp = datetime.strptime(expires_at.strip(), "%Y-%m-%d")
            except ValueError:
                exp = None
        session.add(ComplianceDoc(deal_id=deal_id, doc_type=doc_type, reference_no=reference_no,
                                  issued_by=issued_by, expires_at=exp, status="received"))
        session.commit()
    return RedirectResponse(f"/deals/{deal_id}", status_code=303)


@app.post("/deals/{deal_id}/docs/{doc_id}/verify")
def verify_doc(request: Request, deal_id: int, doc_id: int):
    with Session(engine) as session:
        if not role_at_least(current_user(request, session), "agent"):
            return _forbidden()
        doc = session.get(ComplianceDoc, doc_id)
        if doc and doc.deal_id == deal_id:
            doc.status = "verified"
            session.add(doc)
            session.commit()
    return RedirectResponse(f"/deals/{deal_id}", status_code=303)


@app.post("/deals/{deal_id}/settle")
def settle_deal(request: Request, deal_id: int,
                actual_revenue: float = Form(0, ge=0), actual_cost: float = Form(0, ge=0)):
    with Session(engine) as session:
        if not role_at_least(current_user(request, session), "manager"):
            return _forbidden()
        deal = session.get(Deal, deal_id)
        if deal:
            deal.actual_revenue = actual_revenue
            deal.actual_cost = actual_cost
            deal.realized_margin = round(actual_revenue - actual_cost, 2)
            deal.stage = "settled"                 # settling advances the pipeline...
            deal.closed_at = datetime.utcnow()     # ...and closes the deal (no longer "open")
            deal.updated_at = datetime.utcnow()
            session.add(deal)
            session.commit()
    return RedirectResponse(f"/deals/{deal_id}", status_code=303)


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
def ingest_run(request: Request, background: BackgroundTasks):
    with Session(engine) as session:
        if not role_at_least(current_user(request, session), "manager"):
            return _forbidden()
    background.add_task(ingest_source, Go4WorldCsvSource(INBOX_DIR))   # non-blocking
    return RedirectResponse("/ingest", status_code=303)


@app.post("/ingest/upload")
async def ingest_upload(request: Request, background: BackgroundTasks, file: UploadFile = File(...)):
    """Browser CSV upload: save into the inbox then kick off ingestion in the background."""
    with Session(engine) as session:
        if not role_at_least(current_user(request, session), "manager"):
            return _forbidden()
    if file.filename and file.filename.lower().endswith(".csv"):
        Path(INBOX_DIR).mkdir(parents=True, exist_ok=True)
        (Path(INBOX_DIR) / Path(file.filename).name).write_bytes(await file.read())
        background.add_task(ingest_source, Go4WorldCsvSource(INBOX_DIR))
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


# ----------------------------------------------------------------------------- browser-helper API
# The in-browser userscript reads buy-leads the user is already viewing (their real
# logged-in session) and POSTs them here — no bot, no extra portal requests.

class RawLeadIn(BaseModel):
    product: str
    external_id: str = ""
    category: str = ""
    spec: str = ""
    quantity: float = 0
    unit: str = ""
    target_price: float = 0
    currency: str = "USD"
    dest_country: str = ""
    dest_city: str = ""
    buyer_company: str = ""
    contact_name: str = ""
    email: str = ""
    phone: str = ""
    source_url: str = ""


class RawLeadBatch(BaseModel):
    leads: List[RawLeadIn]


def _ingest_browser_leads(items):
    with Session(engine) as session:
        for it in items:
            if not (it.product or "").strip():
                continue
            lead = Lead(
                source="go4world_browser", external_id=it.external_id,
                product=it.product, category=it.category, spec=it.spec,
                quantity=max(it.quantity, 0.0), unit=it.unit,
                target_price=max(it.target_price, 0.0), currency=it.currency or "USD",
                dest_country=(it.dest_country or "").strip().upper(), dest_city=it.dest_city,
                buyer_company=it.buyer_company, contact_name=it.contact_name,
                email=it.email, phone=it.phone, source_url=it.source_url,
            )
            try:
                create_lead(session, lead)   # dedup + match + auto-quote + Telegram
            except Exception:
                logger.warning("browser lead ingest failed for %r", it.product, exc_info=True)


@app.get("/api/health")
def api_health():
    return {"ok": True, "service": "go4it"}


@app.get("/go4it-capture.user.js", include_in_schema=False)
def userscript_file():
    """Serve the capture userscript so Tampermonkey offers a one-click install
    when you open this URL in the browser."""
    return FileResponse(BASE_DIR.parent / "docs" / "userscript" / "go4it-capture.user.js",
                        media_type="text/javascript")


@app.post("/api/leads/raw")
def api_leads_raw(batch: RawLeadBatch, background: BackgroundTasks,
                  x_api_key: str = Header(default="")):
    if not hmac.compare_digest(x_api_key or "", INGEST_API_KEY):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    background.add_task(_ingest_browser_leads, batch.leads)
    return JSONResponse({"accepted": len(batch.leads)}, status_code=202)


class DomDump(BaseModel):
    url: str = ""
    html: str = ""


@app.post("/api/debug/dom")
def api_debug_dom(dump: DomDump, x_api_key: str = Header(default="")):
    """Receive the buy-leads page HTML from the browser helper so the lead
    extractor can be tuned to the real logged-in DOM."""
    if not hmac.compare_digest(x_api_key or "", INGEST_API_KEY):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    os.makedirs(DEBUG_DIR, exist_ok=True)
    with open(os.path.join(DEBUG_DIR, "browser-dom.html"), "w", encoding="utf-8") as f:
        f.write(dump.html or "")
    logger.info("browser DOM captured from %s (%d bytes)", dump.url, len(dump.html or ""))
    return JSONResponse({"saved": len(dump.html or ""), "url": dump.url})
