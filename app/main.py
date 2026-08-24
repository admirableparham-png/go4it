"""go4it web app — catalog + leads + matching + quotation + team CRM."""
import hmac
import json
import logging
import os
import secrets
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
from sqlalchemy import func, or_
from sqlmodel import Session, select
from starlette.middleware.sessions import SessionMiddleware

from .auth import current_user, hash_password, role_at_least, verify_password
from .command_service import parse_command, run_command_job
from .config import (BASE_URL, CORS_ORIGINS, DEBUG_DIR, FOLLOWUP_DAYS_1, FOLLOWUP_ENABLED, INBOX_DIR,
                     INGEST_API_KEY, IS_LOCAL, SECRET_KEY, SMTP_ENABLED, insecure_default_secrets)
from .csv_import import parse_products
from .db import engine, init_db
from .enrich_service import clean_site, enrich_lead
from .deal_service import (DEAL_STAGES, DOC_TYPES, REQUIRED_DOCS, create_deal,
                           missing_docs_for, next_stage)
from .ingest import ingest_source
from .lead_service import create_lead, run_matching
from .line_spec import all_specs
from . import pipeline
from .models import (Activity, AuditLog, CommandJob, Company, CompanyRole, ComplianceDoc, Contact,
                     CostParam, Deal, DuplicateCandidate, FxRate, IngestionRun, Lead, MailAccount, Match,
                     Outreach, Product, Provenance, Quote, RateCard, RequestDeliverable, RequestMessage,
                     RequestStatusEvent, SellerUpdate, ServiceRequest, StageEvent, Supplier, User, WorkItem)
from . import company_service as CS
from . import tradenet as TN
from . import work_queue as WQ
from . import request_service as RS
from . import suppression as SUP
from . import send_guard as SG
from . import campaign_service as CAMP
from .models import (BounceRecord, Campaign, CampaignRecipient, CampaignStep, EmailTemplate, OutreachControl,
                     Suppression)
from .outreach import (build_parts, default_message, honey_message, mail_decrypt, mail_encrypt,
                       plain_parts, quotation_data, send_bulk_via_account, send_email, send_via_account,
                       verify_smtp, zinc_message)
from .quote_service import create_quote
from .research_engine import (PARTNERS, country_options, market_report,
                              product_options, rank_opportunities, recommend_destinations,
                              resolve_query)
from .sources.go4world_csv import Go4WorldCsvSource
from .telegram import (notify_outreach_sent, notify_quote_ready, notify_request_message, notify_request_update,
                       notify_send_failed, notify_service_request, notify_status_change, send_message)
from .tenant import is_admin, owns, scoped

logger = logging.getLogger("go4it")
BASE_DIR = Path(__file__).parent
app = FastAPI(title="go4it")
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))
app.mount("/static", StaticFiles(directory=str(BASE_DIR / "static")), name="static")

# Central admin navigation (display-only, generated from one config). Exposed to every template so the shared
# header renders both levels without duplicating markup; returns None for non-admins (they keep their nav).
from . import adminnav  # noqa: E402
templates.env.globals["admin_nav"] = adminnav.build_nav
from .send_guard import mail_auth_display  # noqa: E402
templates.env.globals["mail_auth_display"] = mail_auth_display

PUBLIC_PREFIXES = ("/login", "/logout", "/static", "/api", "/go4it-capture.user.js", "/p/")

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
app.add_middleware(SessionMiddleware, secret_key=SECRET_KEY, same_site="lax",
                   https_only=not IS_LOCAL)   # secure cookie once deployed (public BASE_URL)

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
    allow_origins=CORS_ORIGINS,          # capture-helper + localhost by default; lock down via CORS_ORIGINS
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
    # Fail-fast: never run on a PUBLIC BASE_URL with the shipped default secrets (forgeable session /
    # open ingest key). Localhost dev is exempt; there it's just a warning.
    bad = insecure_default_secrets()
    if bad and not IS_LOCAL:
        raise RuntimeError(
            f"Refusing to start: BASE_URL={BASE_URL} is public but these secrets are still the "
            f"shipped defaults: {', '.join(bad)}. Set them in .env before deploying "
            f"(see docs/PRODUCTION.md).")
    if bad:
        logger.warning("Using DEFAULT %s - fine on localhost, MUST be set before deploy.",
                       ", ".join(bad))


# ----------------------------------------------------------------------------- helpers

def _forbidden():
    return HTMLResponse("Forbidden", status_code=403)


def _not_found():
    # Cross-tenant reads return 404 (not 403) so a trader can't even confirm another tenant's row exists.
    return HTMLResponse("Not found", status_code=404)


def _require_admin(user):
    """Return None if the user is the admin/founder, else a 403 — for gatekeeper-only routes
    (shared config, buyer search, user management, cross-tenant assignment)."""
    return None if is_admin(user) else _forbidden()


def _cascade_owner(session, lead):
    """Keep the tenant invariant: a lead's Quotes and Deals ALWAYS share its owner_id. Call after an
    admin reassigns lead ownership, so children don't stay with the former owner (which would let the
    old owner still reach them and lock the new owner out). Caller commits."""
    for q in session.exec(select(Quote).where(Quote.lead_id == lead.id)).all():
        q.owner_id = lead.owner_id
        session.add(q)
    for d in session.exec(select(Deal).where(Deal.lead_id == lead.id)).all():
        d.owner_id = lead.owner_id
        session.add(d)


def _delete_lead(session, lead):
    """Remove a lead and everything hanging off it (matches, quotes, outreach, activity, deals + their
    docs) so nothing is orphaned. Caller commits. Used for 'I added this by mistake'."""
    lid = lead.id
    for m in session.exec(select(Match).where(Match.lead_id == lid)).all():
        session.delete(m)
    for o in session.exec(select(Outreach).where(Outreach.lead_id == lid)).all():
        session.delete(o)
    for a in session.exec(select(Activity).where(Activity.lead_id == lid)).all():
        session.delete(a)
    for q in session.exec(select(Quote).where(Quote.lead_id == lid)).all():
        session.delete(q)
    for d in session.exec(select(Deal).where(Deal.lead_id == lid)).all():
        for cd in session.exec(select(ComplianceDoc).where(ComplianceDoc.deal_id == d.id)).all():
            session.delete(cd)
        session.delete(d)
    session.delete(lead)


def _log(session, lead: Lead, user, kind: str, body: str = ""):
    """Append a timeline entry; stamp first_response_at on the first real action."""
    session.add(Activity(lead_id=lead.id, user_id=user.id if user else None,
                         kind=kind, body=body))
    if kind in ("note", "call", "status_change", "quote_sent", "outreach") and lead.first_response_at is None:
        lead.first_response_at = datetime.utcnow()
        session.add(lead)


def _link_supplier_tn(session, supplier):
    """Non-blocking Trade Network link for a supplier — a failure never affects the supplier write."""
    try:
        from .company_service import link_supplier_company_safe
        if link_supplier_company_safe(session, supplier):
            session.commit()
    except Exception:  # noqa: BLE001
        pass


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
        _link_supplier_tn(session, supplier)
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


_LOGIN_FAILS: dict = {}          # key -> (fail_count, first_fail_epoch); simple in-memory brute-force brake
_LOGIN_MAX = 8                   # allowed fails per window before lock
_LOGIN_WINDOW = 300              # seconds


def _login_key(request, email):
    ip = (request.client.host if request.client else "") or "?"
    return f"{ip}|{(email or '').strip().lower()}"


def _login_locked(key) -> bool:
    rec = _LOGIN_FAILS.get(key)
    if not rec:
        return False
    fails, first = rec
    if (datetime.utcnow().timestamp() - first) > _LOGIN_WINDOW:
        _LOGIN_FAILS.pop(key, None)     # window elapsed -> reset
        return False
    return fails >= _LOGIN_MAX


def _login_fail(key):
    now = datetime.utcnow().timestamp()
    fails, first = _LOGIN_FAILS.get(key, (0, now))
    if (now - first) > _LOGIN_WINDOW:
        fails, first = 0, now
    _LOGIN_FAILS[key] = (fails + 1, first)


@app.post("/login")
def login(request: Request, email: str = Form(...), password: str = Form(...)):
    key = _login_key(request, email)
    if _login_locked(key):
        return RedirectResponse("/login?error=locked", status_code=303)
    with Session(engine) as session:
        user = session.exec(select(User).where(User.email == email.strip().lower())).first()
        if user and user.active and verify_password(password, user.password_hash):
            _LOGIN_FAILS.pop(key, None)
            request.session["user_id"] = user.id
            return RedirectResponse("/", status_code=303)
    _login_fail(key)
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
            stmt = scoped(select(col, func.count(Lead.id)), Lead.owner_id, user).group_by(col)
            return {k: v for k, v in session.exec(stmt).all()}

        by_stage = group(Lead.status)
        by_source = group(Lead.source)
        by_dest = group(Lead.dest_country)
        by_cat = group(Lead.category)
        total = sum(by_stage.values())

        won, lost = by_stage.get("won", 0), by_stage.get("lost", 0)
        win_rate = round(won / (won + lost) * 100) if (won + lost) else None
        open_pipeline = (by_stage.get("new", 0) + by_stage.get("quoted", 0)
                         + by_stage.get("negotiating", 0))
        needs_enrichment = session.exec(scoped(
            select(func.count(Lead.id)).where(
                ((Lead.email == None) | (Lead.email == "")) &        # noqa: E711
                ((Lead.phone == None) | (Lead.phone == ""))),        # noqa: E711
            Lead.owner_id, user)).one()

        qcounts = {k: v for k, v in session.exec(scoped(
            select(Quote.status, func.count(Quote.id)), Quote.owner_id, user)
            .group_by(Quote.status)).all()}
        deals_total = session.exec(scoped(select(func.count(Deal.id)), Deal.owner_id, user)).one()
        deals_open = session.exec(scoped(
            select(func.count(Deal.id)).where(Deal.closed_at == None), Deal.owner_id, user)).one()  # noqa: E711
        products_total = session.exec(select(func.count(Product.id))).one()   # shared catalog (global)

        # Pipeline value: best quote per lead still in play (quoted/negotiating) — scoped to the viewer.
        active_ids = session.exec(scoped(
            select(Lead.id).where(Lead.status.in_(["quoted", "negotiating"])), Lead.owner_id, user)).all()
        pipeline_value = 0.0
        if active_ids:
            rows = session.exec(
                select(func.max(Quote.delivered_total)).where(
                    Quote.lead_id.in_(active_ids)).group_by(Quote.lead_id)).all()
            pipeline_value = sum((r or 0) for r in rows)

        recent_leads = session.exec(scoped(
            select(Lead), Lead.owner_id, user).order_by(Lead.id.desc()).limit(8)).all()
        due_cut = datetime.utcnow().replace(hour=23, minute=59, second=59, microsecond=0)
        due_leads = session.exec(scoped(
            select(Lead).where(Lead.next_action_at != None,             # noqa: E711
                               Lead.next_action_at <= due_cut,
                               Lead.status.notin_(["won", "lost"])),
            Lead.owner_id, user).order_by(Lead.next_action_at.asc()).limit(10)).all()

        # Recent activity — admin sees all; a trader sees only actions on the leads they own.
        if is_admin(user):
            acts = session.exec(select(Activity).order_by(Activity.id.desc()).limit(10)).all()
            user_map = {u.id: u for u in session.exec(select(User)).all()}
        else:
            own_ids = session.exec(select(Lead.id).where(Lead.owner_id == user.id)).all() if user else []
            acts = session.exec(select(Activity).where(Activity.lead_id.in_(own_ids or [0]))
                                .order_by(Activity.id.desc()).limit(10)).all()
            user_map = {user.id: user} if user else {}
        lead_map = {ld.id: ld for ld in session.exec(
            select(Lead).where(Lead.id.in_([a.lead_id for a in acts] or [0]))).all()}

        # a trader's own concierge requests (for the trader dashboard card); admin doesn't need it here
        my_requests = [] if is_admin(user) else session.exec(
            scoped(select(ServiceRequest), ServiceRequest.owner_id, user)
            .order_by(ServiceRequest.id.desc()).limit(6)).all()

        ctx = {
            "request": request, "user": user, "active": "dashboard", "is_admin": is_admin(user),
            "my_requests": my_requests, "request_types": REQUEST_TYPES,
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
            "offer_lines": _offer_lines(),
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


SAVED_VIEWS = {  # (label, engagement_class filter | special)
    "prospects": ("All buyer prospects", ("prospect", "")),
    "engaged": ("Engaged buyers (incl. negative)", ("engaged", "qualified", "customer")),
    "qualified": ("Qualified buyers", ("qualified", "customer")),
    "rejected": ("Replied but rejected", "_rejected"),
    "customers": ("Customers", ("customer",)),
    "invalid": ("Invalid contact", ("invalid",)),
    "never": ("Never contacted", "_never"),
    "enrich": ("Needs enrichment", "_enrich"),
    "archived": ("Archived", "_archived"),
}


@app.get("/leads", response_class=HTMLResponse)
def leads_list(request: Request, q: str = "", stage: str = "", source: str = "",
               category: str = "", dest: str = "", owner: str = "", contact: str = "",
               due: str = "", listed: str = "yes", sort: str = "new", page: int = 1,
               view: str = "", engagement: str = "", replied: str = "", outcome: str = "",
               source_type: str = ""):
    """The dedicated, filterable, paginated lead workspace (the dashboard no longer
    lists every lead) — now the admin Buyers & Prospects database (Trade Network)."""
    per = 50
    with Session(engine) as session:
        user = current_user(request, session)
        stmt = scoped(select(Lead), Lead.owner_id, user)     # traders see ONLY their own leads
        if listed == "no":
            stmt = stmt.where(Lead.active == False)           # noqa: E712  (only unlisted / hidden)
        elif listed != "all":
            stmt = stmt.where(Lead.active == True)            # noqa: E712  (default: only active buyers)
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
        if is_admin(user):        # only the admin can slice across owners; traders are already hard-scoped
            if owner == "me":
                stmt = stmt.where(Lead.owner_id == user.id)
            elif owner == "none":
                stmt = stmt.where(Lead.owner_id == None)                # noqa: E711
            elif owner.isdigit():
                stmt = stmt.where(Lead.owner_id == int(owner))
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

        # --- Trade Network filters (engagement class, reply, source type, saved views) ---
        if engagement:
            stmt = stmt.where(Lead.engagement_class == engagement)
        if replied == "yes":
            stmt = stmt.where(Lead.buyer_replied_at != None)                   # noqa: E711
        elif replied == "no":
            stmt = stmt.where(Lead.buyer_replied_at == None)                   # noqa: E711
        if outcome:
            stmt = stmt.where(Lead.reply_outcome == outcome)
        if source_type:      # map_source is Python — resolve to the matching raw source slugs, then filter in SQL
            matching = [s for s in session.exec(scoped(select(Lead.source), Lead.owner_id, user).distinct()).all()
                        if s and CS.map_source(s, "")[0] == source_type]
            stmt = stmt.where(Lead.source.in_(matching or ["__none__"]))
        if view and view in SAVED_VIEWS:
            spec = SAVED_VIEWS[view][1]
            if spec == "_rejected":
                stmt = stmt.where(Lead.reply_outcome == "negative")
            elif spec == "_never":
                stmt = stmt.where(Lead.engagement_class.in_(("prospect", "")), Lead.first_response_at == None)  # noqa: E711
            elif spec == "_enrich":
                stmt = stmt.where(((Lead.email == None) | (Lead.email == "")) &                                 # noqa: E711
                                  ((Lead.phone == None) | (Lead.phone == "")))                                  # noqa: E711
            elif spec == "_archived":
                stmt = stmt.where((Lead.engagement_class == "archived") | (Lead.active == False))               # noqa: E712
            elif isinstance(spec, tuple):
                stmt = stmt.where(Lead.engagement_class.in_(spec))

        total = session.exec(select(func.count()).select_from(stmt.subquery())).one()
        pages = max(1, (total + per - 1) // per)
        page = min(max(1, page), pages)
        order = LEAD_SORTS.get(sort, LEAD_SORTS["new"])
        leads = session.exec(stmt.order_by(order).offset((page - 1) * per).limit(per)).all()

        ids = [ld.id for ld in leads] or [0]
        mcounts = {lid: c for lid, c in session.exec(
            select(Match.lead_id, func.count(Match.id)).where(
                Match.lead_id.in_(ids)).group_by(Match.lead_id)).all()}
        user_map = ({u.id: u for u in session.exec(select(User)).all()} if is_admin(user)
                    else ({user.id: user} if user else {}))     # traders never see other users
        sources = sorted({s for s in session.exec(
            scoped(select(Lead.source), Lead.owner_id, user).distinct()).all() if s})
        categories = sorted({c for c in session.exec(
            scoped(select(Lead.category), Lead.owner_id, user).distinct()).all() if c})
        dests = sorted({d for d in session.exec(
            scoped(select(Lead.dest_country), Lead.owner_id, user).distinct()).all() if d})
        product_count = session.exec(select(func.count(Product.id))).one()

        # --- Trade Network enrichment (admin buyers database) ---
        source_map = {ld.id: TN.source_label(ld.source)[0] for ld in leads}
        page_company_ids = [ld.company_id for ld in leads if ld.company_id]
        verif_map = {c.id: c.verification_status for c in session.exec(
            select(Company).where(Company.id.in_(page_company_ids))).all()} if page_company_ids else {}
        dupe_ids = set()
        if page_company_ids:
            for dc in session.exec(select(DuplicateCandidate).where(
                    DuplicateCandidate.status == "open",
                    (DuplicateCandidate.left_id.in_(page_company_ids)) |
                    (DuplicateCandidate.right_id.in_(page_company_ids)))).all():
                dupe_ids.update((dc.left_id, dc.right_id))
        # response-rate stats over the FULL filtered set (not just this page)
        stats = TN.response_stats(session, session.exec(stmt).all()) if is_admin(user) else None

        ctx = {
            "request": request, "user": user, "active": "leads", "is_admin": is_admin(user),
            "leads": leads, "mcounts": mcounts, "user_map": user_map,
            "total": total, "page": page, "pages": pages,
            "sources": sources, "categories": categories, "dests": dests,
            "stages": STAGES, "product_count": product_count,
            "users": [u for u in user_map.values() if u.active] if is_admin(user) else [],
            "source_map": source_map, "verif_map": verif_map, "dupe_ids": dupe_ids,
            "stats": stats, "saved_views": SAVED_VIEWS,
            "f": {"q": q, "stage": stage, "source": source, "category": category,
                  "dest": dest, "owner": owner, "contact": contact, "due": due,
                  "listed": listed, "sort": sort, "view": view, "engagement": engagement,
                  "replied": replied, "outcome": outcome, "source_type": source_type},
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
        # scoped: a trader can only bulk-act on leads they OWN (can't touch another tenant's rows by id)
        leads = session.exec(scoped(select(Lead), Lead.owner_id, user).where(Lead.id.in_(ids or [0]))).all()
        if action == "email":
            if not is_admin(user):          # buyer outreach is admin-mediated only (confidential model)
                return _forbidden()
            # Don't mutate — hand off to the compose screen with the selected buyers + the sender's mailboxes.
            accounts = session.exec(select(MailAccount).where(MailAccount.user_id == user.id,
                                                              MailAccount.active == True)  # noqa: E712
                                    .order_by(MailAccount.is_default.desc(), MailAccount.id)).all()
            with_email = [ld for ld in leads if (ld.email or "").strip()]
            return templates.TemplateResponse("mail_compose.html", {
                "request": request, "user": user, "active": "leads",
                "leads": leads, "with_email": with_email, "accounts": accounts})
        for lead in leads:
            if action == "delete":
                _delete_lead(session, lead)         # scoped select above => only own leads deletable
                continue
            if action == "unlist":
                lead.active = False                 # reversible hide from the buyer list
            elif action == "relist":
                lead.active = True
            elif action in ("assign", "assign_me") and not is_admin(user):
                continue                            # reassigning ownership is the admin's tool only
            elif action == "assign_me" and user:
                lead.owner_id = user.id
                _cascade_owner(session, lead)       # quotes + deals follow the lead's new owner
            elif action == "assign":
                lead.owner_id = int(owner_id) if owner_id else None
                _cascade_owner(session, lead)
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


@app.post("/leads/{lead_id}/unlist")
def unlist_lead(request: Request, lead_id: int, relist: str = ""):
    """Per-row reversible unlist: hide a buyer from the list (?relist=1 brings it back). Owner/admin only."""
    with Session(engine) as session:
        user = current_user(request, session)
        lead = session.get(Lead, lead_id)
        if not lead or not role_at_least(user, "agent") or not owns(lead.owner_id, user):
            return _not_found()
        lead.active = (relist == "1")
        session.add(lead); session.commit()
    return RedirectResponse(request.headers.get("referer") or "/leads", status_code=303)


@app.post("/leads/bulk/email")
def leads_bulk_email(request: Request, account_id: int = Form(0), subject: str = Form(""),
                     body: str = Form(""), ids: List[int] = Form(default=[])):
    """Admin-only buyer outreach: send from a Go4it-controlled mailbox to the selected buyers."""
    with Session(engine) as session:
        user = current_user(request, session)
        if not is_admin(user):              # confidential model: only the admin contacts buyers
            return _forbidden()
        acct = session.get(MailAccount, account_id)
        if not acct or acct.user_id != user.id or not acct.active:
            _flash(request, "Pick one of your connected email accounts to send from.", "rose")
            return RedirectResponse("/mail", status_code=303)
        subject = SG.sanitize_header((subject or "").strip()[:200])   # block header injection
        if not subject or not (body or "").strip():
            _flash(request, "Add a subject and a message before sending.", "rose")
            return RedirectResponse("/leads", status_code=303)
        if SG.outreach_paused(session):        # global Pause-all kill switch
            _flash(request, "All outreach is paused — resume it before sending.", "rose")
            return RedirectResponse("/leads", status_code=303)
        leads = session.exec(scoped(select(Lead), Lead.owner_id, user).where(Lead.id.in_(ids or [0]))).all()
        # suppression check + seller-identity redaction, per recipient, immediately before sending
        prepared, suppressed = [], 0
        for ld in leads:
            em = (ld.email or "").strip()
            if not em:
                continue
            if SUP.is_suppressed(session, em, tenant_id=ld.seller_id):
                suppressed += 1
                continue
            if len(prepared) >= 60:            # cap the synchronous batch
                break
            guarded = SG.guard_buyer_text(session, body, ld.seller_id)   # buyers never learn the seller
            text, html = plain_parts(guarded)
            prepared.append((ld, em, guarded, text, html))
        items = [(em, subject, text, html) for (_ld, em, _g, text, html) in prepared]
        results = {r[0]: r for r in send_bulk_via_account(acct, items, reply_to=acct.email)}
        sent = fail = 0
        for ld, em, guarded, _t, _h in prepared:
            r = results.get(em)
            ok = bool(r and r[1])
            sent, fail = (sent + 1, fail) if ok else (sent, fail + 1)
            session.add(Outreach(lead_id=ld.id, direction="out", channel="email",
                                 recipient=em[:200], from_addr=acct.email[:200],
                                 subject=subject[:200], body=guarded[:4000],
                                 status="sent" if ok else "failed", error=(r[2] if r else "no result"),
                                 message_id=(r[3] if r else ""), user_id=user.id))
            if ok and ld.first_response_at is None:
                ld.first_response_at = datetime.utcnow(); session.add(ld)
        session.commit()
        skipped = len(leads) - len(prepared) - suppressed
        note = f"Sent {sent} email(s) from {acct.email}."
        if fail:
            note += f" {fail} failed."
        if suppressed:
            note += f" {suppressed} skipped (on the do-not-contact list)."
        if skipped:
            note += f" {skipped} skipped (no email address, or over the 60-per-send cap)."
        _flash(request, note, "emerald" if sent else "rose")
    return RedirectResponse("/leads", status_code=303)


# ------------------------------------------------------------ admin: Go4it-controlled outreach mailboxes
# Confidential model: only the admin contacts buyers, from Go4it-controlled mailboxes. Sellers never email
# buyers directly, so this whole surface is admin-only.

@app.get("/mail", response_class=HTMLResponse)
def mail_accounts(request: Request):
    """The Go4it-controlled sending mailboxes the admin uses for buyer outreach."""
    with Session(engine) as session:
        user = current_user(request, session)
        if not is_admin(user):
            return _forbidden()
        accounts = session.exec(select(MailAccount).where(MailAccount.user_id == user.id)
                                .order_by(MailAccount.is_default.desc(), MailAccount.id)).all()
    flashes = request.session.pop("_flash", [])
    return templates.TemplateResponse("mail_accounts.html", {
        "request": request, "user": user, "active": "mail", "accounts": accounts, "flashes": flashes})


@app.post("/mail")
def mail_add(request: Request, email: str = Form(""), from_name: str = Form(""),
             app_password: str = Form(""), provider: str = Form("gmail"),
             smtp_host: str = Form(""), smtp_port: str = Form("587")):
    """Connect a Go4it mailbox — verifies the SMTP login (App Password) before saving it encrypted."""
    with Session(engine) as session:
        user = current_user(request, session)
        if not is_admin(user):
            return _forbidden()
        email = (email or "").strip()
        pw = (app_password or "").strip()
        host = "smtp.gmail.com" if provider == "gmail" else ((smtp_host or "").strip() or "smtp.gmail.com")
        try:
            port = int(smtp_port or 587)
        except (TypeError, ValueError):
            port = 587
        if not email or not pw:
            _flash(request, "Enter your email and its app password.", "rose")
            return RedirectResponse("/mail", status_code=303)
        ok, err = verify_smtp(host, port, email, pw)
        if not ok:
            _flash(request, f"Couldn't connect: {err}  —  Gmail needs an App Password (not your login), "
                            "with 2-Step Verification on.", "rose")
            return RedirectResponse("/mail", status_code=303)
        first = session.exec(select(func.count(MailAccount.id))
                             .where(MailAccount.user_id == user.id)).one() == 0
        session.add(MailAccount(
            user_id=user.id, email=email, from_name=(from_name or "").strip()[:120],
            provider=provider if provider in ("gmail", "custom") else "custom",
            smtp_host=host, smtp_port=port, smtp_password_enc=mail_encrypt(pw),
            is_default=first, active=True, last_verified_at=datetime.utcnow()))
        session.commit()
        _flash(request, f"Connected {email} ✓")
    return RedirectResponse("/mail", status_code=303)


@app.post("/mail/{acct_id}/default")
def mail_default(request: Request, acct_id: int):
    with Session(engine) as session:
        user = current_user(request, session)
        acct = session.get(MailAccount, acct_id)
        if not user or not acct or acct.user_id != user.id:
            return _not_found()
        for a in session.exec(select(MailAccount).where(MailAccount.user_id == user.id)).all():
            a.is_default = (a.id == acct.id); session.add(a)
        session.commit()
    return RedirectResponse("/mail", status_code=303)


@app.post("/mail/{acct_id}/delete")
def mail_delete(request: Request, acct_id: int):
    with Session(engine) as session:
        user = current_user(request, session)
        acct = session.get(MailAccount, acct_id)
        if not user or not acct or acct.user_id != user.id:
            return _not_found()
        session.delete(acct); session.commit()
    return RedirectResponse("/mail", status_code=303)


# ----------------------------------------------------------------------------- suppliers + intel

@app.get("/suppliers", response_class=HTMLResponse)
def suppliers_list(request: Request, q: str = "", country: str = "", listed: str = "active"):
    """The supplier catalog — now searchable/filterable, with Trade Network company links."""
    with Session(engine) as session:
        user = current_user(request, session)
        if not is_admin(user):          # sourcing (supplier contacts/terms) is founder-internal
            return _forbidden()
        stmt = select(Supplier)
        if q:
            like = f"%{q.strip()}%"
            stmt = stmt.where(Supplier.name.ilike(like) | Supplier.contact.ilike(like)
                              | Supplier.email.ilike(like))
        if country:
            stmt = stmt.where(Supplier.country == country.upper())
        if listed == "active":
            stmt = stmt.where(Supplier.active == True)                        # noqa: E712
        elif listed == "archived":
            stmt = stmt.where(Supplier.active == False)                       # noqa: E712
        suppliers = session.exec(stmt.order_by(Supplier.country, Supplier.name)).all()
        pcounts = {sid: c for sid, c in session.exec(
            select(Product.supplier_id, func.count(Product.id)).group_by(Product.supplier_id)).all()}
        countries = sorted({s.country for s in session.exec(select(Supplier)).all() if s.country})
        ctx = {"request": request, "user": user, "active": "suppliers",
               "suppliers": suppliers, "pcounts": pcounts, "can_edit": True,
               "q": q, "country": country, "listed": listed, "countries": countries}
    return templates.TemplateResponse("suppliers.html", ctx)


def _clamp_reliability(v):
    try:
        return max(1, min(5, int(v)))
    except (TypeError, ValueError):
        return 3


@app.post("/suppliers")
def supplier_create(request: Request, name: str = Form(...), country: str = Form("IR"),
                    city: str = Form(""), contact: str = Form(""), email: str = Form(""),
                    phone: str = Form(""), reliability: int = Form(3),
                    payment_terms: str = Form("")):
    """Add a supplier (the sourcing side). Dedups on normalized name — re-adding an existing name
    updates its details instead of creating a duplicate."""
    with Session(engine) as session:
        if not is_admin(current_user(request, session)):     # shared catalog/suppliers = admin-write only
            return _forbidden()
        name = (name or "").strip()
        if not name:
            return RedirectResponse("/suppliers?error=name", status_code=303)
        norm = name.lower()
        sup = session.exec(select(Supplier).where(Supplier.name_normalized == norm)).first()
        if sup is None:
            sup = Supplier(name=name, name_normalized=norm)
        sup.country = (country or "IR").strip().upper()[:3]
        sup.city = (city or "").strip()
        sup.contact = (contact or "").strip()
        sup.email = (email or "").strip()
        sup.phone = (phone or "").strip()
        sup.reliability = _clamp_reliability(reliability)
        sup.payment_terms = (payment_terms or "").strip()
        session.add(sup)
        session.commit()
        session.refresh(sup)
        _link_supplier_tn(session, sup)          # non-blocking Trade Network link
    return RedirectResponse("/suppliers", status_code=303)


@app.post("/suppliers/{supplier_id}/edit")
def supplier_edit(request: Request, supplier_id: int, name: str = Form(...),
                  country: str = Form("IR"), city: str = Form(""), contact: str = Form(""),
                  email: str = Form(""), phone: str = Form(""), reliability: int = Form(3),
                  payment_terms: str = Form("")):
    """Update a supplier's details in place."""
    with Session(engine) as session:
        if not is_admin(current_user(request, session)):     # shared catalog/suppliers = admin-write only
            return _forbidden()
        sup = session.get(Supplier, supplier_id)
        if not sup:
            return HTMLResponse("Not found", status_code=404)
        name = (name or "").strip()
        if name:
            norm = name.lower()
            clash = session.exec(select(Supplier).where(
                Supplier.name_normalized == norm, Supplier.id != supplier_id)).first()
            if clash:            # don't create two rows sharing a normalized name (breaks dedup)
                return RedirectResponse("/suppliers?error=dupe", status_code=303)
            sup.name = name
            sup.name_normalized = norm
        sup.country = (country or "IR").strip().upper()[:3]
        sup.city = (city or "").strip()
        sup.contact = (contact or "").strip()
        sup.email = (email or "").strip()
        sup.phone = (phone or "").strip()
        sup.reliability = _clamp_reliability(reliability)
        sup.payment_terms = (payment_terms or "").strip()
        session.add(sup)
        session.commit()
        session.refresh(sup)
        _link_supplier_tn(session, sup)          # non-blocking Trade Network link
    return RedirectResponse("/suppliers", status_code=303)


@app.post("/suppliers/{supplier_id}/toggle")
def supplier_toggle(request: Request, supplier_id: int):
    """Deactivate / reactivate a supplier (kept for history; hidden from active sourcing)."""
    with Session(engine) as session:
        if not is_admin(current_user(request, session)):     # shared catalog/suppliers = admin-write only
            return _forbidden()
        sup = session.get(Supplier, supplier_id)
        if not sup:
            return HTMLResponse("Not found", status_code=404)
        sup.active = not sup.active
        session.add(sup)
        session.commit()
    return RedirectResponse("/suppliers", status_code=303)


# ============================================================================
# Trade Network (Phase 2) — ADMIN-ONLY. Sellers DB, Company detail, Data Quality,
# Duplicate Review, and audited/CSV-safe exports. Never reachable by sellers.
# ============================================================================

def _csv_safe(v):
    """Neutralize CSV formula injection: prefix a cell that starts with a formula trigger with an apostrophe."""
    s = "" if v is None else str(v)
    if s[:1] in ("=", "+", "-", "@", "\t", "\r"):
        s = "'" + s
    return s


def _export_csv(session, user, kind, cols, rows):
    """Audited, formula-injection-safe CSV. `rows` = list of value-lists aligned to `cols`."""
    import csv
    import io
    buf = io.StringIO()
    w = csv.writer(buf)
    stamp = datetime.utcnow().isoformat(timespec="seconds")
    w.writerow(list(cols) + ["exported_at", "exported_by"])
    for r in rows:
        w.writerow([_csv_safe(x) for x in r] + [stamp, _csv_safe(user.email)])
    pipeline.audit(session, user, kind, None, "pii_export", {"kind": kind, "rows": len(rows)})
    session.commit()
    fname = f"go4it-{kind}-{stamp[:10]}.csv"
    return PlainTextResponse(buf.getvalue(), media_type="text/csv",
                             headers={"Content-Disposition": f'attachment; filename="{fname}"'})


@app.get("/sellers", response_class=HTMLResponse)
def sellers_list(request: Request, q: str = "", country: str = "", status: str = "active"):
    """Admin seller database (canonical Company, role=seller). Sellers may have a platform account or be
    login-less/external."""
    with Session(engine) as session:
        user = current_user(request, session)
        if not is_admin(user):
            return _forbidden()
        seller_ids = {r.company_id for r in session.exec(
            select(CompanyRole).where(CompanyRole.role == "seller")).all()}
        stmt = select(Company).where(Company.id.in_(seller_ids)) if seller_ids else select(Company).where(Company.id == -1)
        if status in ("active", "archived"):
            stmt = stmt.where(Company.status == status)
        if q:
            stmt = stmt.where(Company.name.ilike(f"%{q}%"))
        if country:
            stmt = stmt.where(Company.country == country.upper())
        sellers = session.exec(stmt.order_by(Company.name)).all()
        umap = {u.id: u for u in session.exec(select(User)).all()}
        # per-seller request/quote/deal counts via seller_id on managed leads / requester on requests
        rows = []
        for c in sellers:
            reqs = 0
            if c.account_user_id:
                reqs = session.exec(select(func.count(ServiceRequest.id)).where(
                    ServiceRequest.requester_id == c.account_user_id)).one()
            rows.append({"c": c, "account": umap.get(c.account_user_id),
                         "contacts": session.exec(select(func.count(Contact.id)).where(
                             Contact.company_id == c.id)).one(), "requests": reqs})
        flashes = request.session.pop("_flash", [])
    return templates.TemplateResponse("sellers.html", {
        "request": request, "user": user, "active": "sellers", "rows": rows, "q": q,
        "country": country, "status": status, "flashes": flashes})


@app.post("/sellers")
def seller_create(request: Request, name: str = Form(...), country: str = Form(""), city: str = Form(""),
                  website: str = Form(""), contact_name: str = Form(""), email: str = Form(""),
                  phone: str = Form(""), account_user_id: str = Form(""), notes: str = Form("")):
    with Session(engine) as session:
        user = current_user(request, session)
        if not is_admin(user):
            return _forbidden()
        if not (name or "").strip():
            return RedirectResponse("/sellers?error=name", status_code=303)
        co = CS.get_or_create_company(session, None, name, country, website, role="seller",
                                      email=email, phone=phone)
        co.primary_role = "seller"
        co.city = (city or "").strip()
        co.notes = (notes or "").strip()
        if account_user_id.isdigit():
            co.account_user_id = int(account_user_id)
        session.add(co)
        session.flush()
        if contact_name or email or phone:
            CS.get_or_create_contact(session, co, name=contact_name, email=email, phone=phone, is_primary=True)
        CS.add_provenance(session, "company", co.id, None, "manual", "Manual entry",
                          source_ref=f"seller:{co.id}")
        pipeline.audit(session, user, "company", co.id, "seller_create", {"name": name})
        session.commit()
        _flash(request, "Seller added ✓")
    return RedirectResponse("/sellers", status_code=303)


@app.post("/sellers/{company_id}/archive")
def seller_archive(request: Request, company_id: int, restore: str = Form("")):
    with Session(engine) as session:
        user = current_user(request, session)
        if not is_admin(user):
            return _forbidden()
        co = session.get(Company, company_id)
        if not co:
            return _not_found()
        co.status = "active" if restore == "1" else "archived"   # never hard-delete (history preserved)
        session.add(co)
        pipeline.audit(session, user, "company", co.id, "seller_archive", {"status": co.status})
        session.commit()
    return RedirectResponse("/sellers", status_code=303)


@app.get("/companies/{company_id}", response_class=HTMLResponse)
def company_detail(request: Request, company_id: int):
    """Admin-only canonical company view. Never shows seller-facing anon refs as an identifier."""
    with Session(engine) as session:
        user = current_user(request, session)
        if not is_admin(user):
            return _forbidden()
        co = session.get(Company, company_id)
        if not co:
            return _not_found()
        ctx = TN.company_detail(session, co)
        umap = {u.id: u for u in session.exec(select(User)).all()}
        flashes = request.session.pop("_flash", [])
        ctx.update({"request": request, "user": user, "active": "leads", "umap": umap, "flashes": flashes})
        resp = templates.TemplateResponse("company_detail.html", ctx)   # render while the session is open
        pipeline.audit(session, user, "company", co.id, "pii_view", {"via": "company_detail"},
                       tenant_id=co.tenant_id)
        session.commit()
        return resp


@app.post("/companies/{company_id}/verify")
def company_verify(request: Request, company_id: int, verification_status: str = Form("verified"),
                   verification_method: str = Form(""), verification_confidence: str = Form("0"),
                   verification_notes: str = Form("")):
    with Session(engine) as session:
        user = current_user(request, session)
        if not is_admin(user):
            return _forbidden()
        co = session.get(Company, company_id)
        if not co:
            return _not_found()
        frm = co.verification_status
        co.verification_status = verification_status if verification_status in (
            "unverified", "partial", "verified", "rejected") else "unverified"
        co.verification_method = (verification_method or "").strip()[:40]
        try:
            co.verification_confidence = max(0, min(100, int(verification_confidence or 0)))
        except ValueError:
            co.verification_confidence = 0
        co.verification_notes = (verification_notes or "").strip()[:500]
        co.verified_at = datetime.utcnow()
        co.verified_by = user.email
        session.add(co)
        pipeline.audit(session, user, "company", co.id, "verification_change",
                       {"from": frm, "to": co.verification_status, "method": co.verification_method,
                        "confidence": co.verification_confidence}, tenant_id=co.tenant_id)
        session.commit()
        _flash(request, "Verification updated ✓")
    return RedirectResponse(f"/companies/{company_id}", status_code=303)


@app.get("/data-quality", response_class=HTMLResponse)
def data_quality(request: Request, queue: str = ""):
    with Session(engine) as session:
        user = current_user(request, session)
        if not is_admin(user):
            return _forbidden()
        metrics = TN.data_quality_metrics(session)
        sources = TN.source_quality(session)
        flashes = request.session.pop("_flash", [])
    return templates.TemplateResponse("data_quality.html", {
        "request": request, "user": user, "active": "dataquality", "m": metrics, "sources": sources,
        "queue": queue, "flashes": flashes})


@app.get("/duplicates", response_class=HTMLResponse)
def duplicates_list(request: Request, status: str = "open"):
    with Session(engine) as session:
        user = current_user(request, session)
        if not is_admin(user):
            return _forbidden()
        stmt = select(DuplicateCandidate)
        if status:
            stmt = stmt.where(DuplicateCandidate.status == status)
        cands = session.exec(stmt.order_by(DuplicateCandidate.strength.desc(), DuplicateCandidate.id.desc())
                             .limit(200)).all()
        rows = []
        for dc in cands:
            a, b = session.get(Company, dc.left_id), session.get(Company, dc.right_id)
            if not a or not b:
                continue
            rows.append({"dc": dc, "a": a, "b": b, "signals": json.loads(dc.signals or "[]"),
                         "a_contacts": session.exec(select(Contact).where(Contact.company_id == a.id)).all(),
                         "b_contacts": session.exec(select(Contact).where(Contact.company_id == b.id)).all()})
        flashes = request.session.pop("_flash", [])
    return templates.TemplateResponse("duplicates.html", {
        "request": request, "user": user, "active": "duplicates", "rows": rows, "status": status,
        "flashes": flashes})


@app.post("/duplicates/{cand_id}/dispose")
def duplicate_dispose(request: Request, cand_id: int, action: str = Form(...)):
    with Session(engine) as session:
        user = current_user(request, session)
        if not is_admin(user):
            return _forbidden()
        dc = session.get(DuplicateCandidate, cand_id)
        if not dc:
            return _not_found()
        if action == "merge":
            ok, err = CS.merge_companies(session, dc.left_id, dc.right_id, user)
            _flash(request, "Merged ✓" if ok else f"Merge blocked — {err}", "emerald" if ok else "rose")
        elif action in ("not_duplicate", "confirmed", "deferred", "linked"):
            dc.status = {"not_duplicate": "not_duplicate", "confirmed": "confirmed",
                         "deferred": "deferred", "linked": "linked"}[action]
            dc.reviewer = user.email
            dc.reviewed_at = datetime.utcnow()
            session.add(dc)
            pipeline.audit(session, user, "company", dc.left_id, "dup_dispose",
                           {"cand": cand_id, "action": action}, tenant_id=dc.tenant_id)
        session.commit()
    return RedirectResponse("/duplicates", status_code=303)


@app.post("/companies/{company_id}/unmerge")
def company_unmerge(request: Request, company_id: int):
    with Session(engine) as session:
        user = current_user(request, session)
        if not is_admin(user):
            return _forbidden()
        ok, err = CS.unmerge_companies(session, company_id, user)
        session.commit()
        _flash(request, "Unmerged ✓" if ok else f"Unmerge failed — {err}", "emerald" if ok else "rose")
    return RedirectResponse(f"/companies/{company_id}", status_code=303)


@app.post("/duplicates/rescan")
def duplicates_rescan(request: Request):
    with Session(engine) as session:
        user = current_user(request, session)
        if not is_admin(user):
            return _forbidden()
        n = CS.scan_duplicates(session)
        session.commit()
        _flash(request, f"Rescan complete — {n} new candidate(s).")
    return RedirectResponse("/duplicates", status_code=303)


_ENGAGEMENT_CLASSES = ("prospect", "contacted", "engaged", "qualified", "customer", "invalid", "archived")
_REPLY_OUTCOMES = ("none", "positive", "negative", "neutral", "bounced", "auto_reply")


@app.post("/leads/{lead_id}/classify")
def lead_classify(request: Request, lead_id: int, engagement_class: str = Form(""),
                  reply_outcome: str = Form("")):
    """Admin correction of a buyer's engagement / reply outcome. Never deletes reply history (Outreach rows
    stay); just overrides the derived label + audits it."""
    with Session(engine) as session:
        user = current_user(request, session)
        if not is_admin(user):
            return _forbidden()
        lead = session.get(Lead, lead_id)
        if not lead:
            return _not_found()
        frm = lead.engagement_class
        if engagement_class in _ENGAGEMENT_CLASSES:
            lead.engagement_class = engagement_class
        if reply_outcome in _REPLY_OUTCOMES:
            lead.reply_outcome = reply_outcome
        session.add(lead)
        pipeline.audit(session, user, "lead", lead.id, "classify_override",
                       {"from": frm, "to": lead.engagement_class, "outcome": lead.reply_outcome},
                       tenant_id=lead.seller_id)
        session.commit()
        _flash(request, "Classification updated ✓")
    return RedirectResponse(request.headers.get("referer") or f"/leads/{lead_id}", status_code=303)


@app.get("/export/{kind}.csv")
def export_csv(request: Request, kind: str):
    """Audited, admin-only, CSV-injection-safe export of a Trade Network slice. NEVER seller-reachable."""
    with Session(engine) as session:
        user = current_user(request, session)
        if not is_admin(user):
            return _forbidden()
        if kind == "buyers":
            leads = session.exec(select(Lead).where(Lead.buyer_company != "").order_by(Lead.id)).all()
            cols = ["company", "contact", "email", "phone", "country", "product", "engagement",
                    "reply_outcome", "source", "created_at"]
            rows = [[l.buyer_company, l.contact_name, l.email, l.phone, l.dest_country, l.product,
                     l.engagement_class, l.reply_outcome, TN.source_label(l.source)[0],
                     (l.created_at or "")] for l in leads]
        elif kind in ("sellers", "suppliers"):
            role = "seller" if kind == "sellers" else "supplier"
            ids = {r.company_id for r in session.exec(select(CompanyRole).where(CompanyRole.role == role)).all()}
            comps = session.exec(select(Company).where(Company.id.in_(ids))).all() if ids else []
            cols = ["name", "country", "city", "website", "verification", "confidence", "status"]
            rows = [[c.name, c.country, c.city, c.website, c.verification_status,
                     c.verification_confidence, c.status] for c in comps]
        else:
            return _not_found()
        return _export_csv(session, user, kind, cols, rows)


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
        if not is_admin(user):          # market-intel research is founder-internal
            return _forbidden()

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
        if not is_admin(user):          # research surface — admin only
            return _forbidden()
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


@app.get("/markets", response_class=HTMLResponse)
def markets(request: Request):
    """Read-only Markets landing page — the single entry point under Intelligence that ORGANIZES access to
    the existing country pages (Georgia, UAE, future). It never combines, moves or rewrites the underlying
    market datasets; it only lists them with links resolved from their existing named routes. Country pages
    and bookmarks keep working exactly as before."""
    with Session(engine) as session:
        user = current_user(request, session)
        if not is_admin(user):          # market intelligence surface — admin only (same gate as the pages)
            return _forbidden()
    cards = [{"name": m["name"], "iso": m["iso"], "summary": m["summary"],
              "href": request.url_for(m["endpoint"]).path} for m in adminnav.MARKETS]
    return templates.TemplateResponse("markets.html", {
        "request": request, "user": user, "active": "markets", "cards": cards})


# ---------------------------------------------------------------------- product-line hub (generic)
# Each product line is ONE spec file in docs/research/lines/<slug>.json (families + meta + harvested
# buyers file + RFQs file + display order). Adding a line = drop a spec + run harvest_line ->
# enrich_line -> load_line; the /lines hub, the /uae alias, and the dashboard "what we offer" strip
# all pick it up automatically — no code change. See app/line_spec.py.


def _line_ctx(spec):
    """Render context for one product line, from its spec (families/meta/order) + buyers/rfqs JSON."""
    data = _load_research(spec.get("buyers_file", ""))
    buyers = data.get("buyers", [])
    order = spec.get("display_order", [])
    groups = {}
    for b in buyers:
        groups.setdefault((b.get("categories") or ["Other"])[0], []).append(b)
    for c in groups:
        groups[c].sort(key=lambda x: -x.get("match_score", 0))
    grouped = ([(c, groups[c]) for c in order if c in groups]
               + [(c, groups[c]) for c in groups if c not in order])
    ranked = sorted([b for b in buyers if b.get("match_score", 0) >= 75],
                    key=lambda x: -x.get("match_score", 0))
    return {
        "head": dict(spec.get("meta", {})),
        "families": spec.get("families", []),
        "buyers": buyers, "grouped": grouped,
        "ranked": ranked[:60], "high_n": len(ranked),
        "rfqs": _load_research(spec.get("rfqs_file", "")).get("rfqs", []),
        "with_phone": data.get("with_phone", 0), "with_email": data.get("with_email", 0),
        "with_website": data.get("with_website", 0), "has_data": bool(buyers),
        "bulk_n": data.get("bulk_likely_count", 0),
    }


def _offer_lines():
    """Compact 'what we offer' family cards per line, for the dashboard strip (no fetch)."""
    out = []
    for spec in all_specs():
        fams = [{"name": f.get("name", ""), "brands": f.get("brands", []),
                 "specs": f.get("specs", ""), "price": f.get("price_usd", "")}
                for f in spec.get("families", [])]
        if fams:
            out.append({"key": spec["slug"], "label": spec.get("label", spec["slug"]),
                        "families": fams})
    return out


def _line_switcher(specs, hub_base):
    """Switcher pills for the hub. hub_base '/uae' -> ?line= links; else /lines/<slug> links."""
    out = []
    for s in specs:
        key = s["slug"]
        url = f"/uae?line={key}" if hub_base == "/uae" else f"/lines/{key}"
        out.append({"key": key, "label": s.get("label", key), "url": url,
                    "count": len(_load_research(s.get("buyers_file", "")).get("buyers", []))})
    return out


def _render_line_hub(request, user, active, hub_base, want_slug, specs):
    """Shared renderer for /lines/<slug> and the /uae alias. Falls back to the first line with data."""
    empty = {"head": {}, "families": [], "buyers": [], "grouped": [], "ranked": [], "high_n": 0,
             "rfqs": [], "with_phone": 0, "with_email": 0, "with_website": 0, "bulk_n": 0,
             "has_data": False}
    if not specs:
        ctx = {"request": request, "user": user, "active": active, "hub_base": hub_base,
               "lines": [], "line": {"key": "", "label": ""}}
        ctx.update(empty)
        return templates.TemplateResponse("lines.html", ctx)
    current = next((s for s in specs if s["slug"] == want_slug), specs[0])
    if not _load_research(current.get("buyers_file", "")).get("buyers"):   # empty -> first with data
        current = next((s for s in specs if _load_research(s.get("buyers_file", "")).get("buyers")),
                       current)
    ctx = {"request": request, "user": user, "active": active, "hub_base": hub_base,
           "lines": _line_switcher(specs, hub_base),
           "line": {"key": current["slug"], "label": current.get("label", current["slug"])}}
    ctx.update(_line_ctx(current))
    return templates.TemplateResponse("lines.html", ctx)


@app.get("/lines/{slug}", response_class=HTMLResponse)
def lines_hub(request: Request, slug: str):
    """Generic product-line buyers hub — ANY line defined by a spec in docs/research/lines/."""
    with Session(engine) as session:
        user = current_user(request, session)
        if not is_admin(user):          # product-line research hub — admin only
            return _forbidden()
    return _render_line_hub(request, user, "lines", "/lines", slug, all_specs())


@app.get("/uae", response_class=HTMLResponse)
def uae(request: Request, line: str = "cd-dvd"):
    """UAE buyers hub (alias) — the UAE-destination product lines (CD & DVD, Decoration, ...)."""
    with Session(engine) as session:
        user = current_user(request, session)
        if not is_admin(user):          # product-line research hub — admin only
            return _forbidden()
    ae = [s for s in all_specs() if s.get("dest", {}).get("iso") == "AE"]
    return _render_line_hub(request, user, "uae", "/uae", line, ae)


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
        if not is_admin(user):          # the research console is the founder's moat — admin only
            return _forbidden()
    ctx = {"request": request, "user": user, "active": "research",
           "countries": country_options(), "products": product_options(),
           "default_reporter": 268}
    return templates.TemplateResponse("research.html", ctx)


@app.post("/research/run", response_class=HTMLResponse)
def research_run(request: Request, reporter: str = Form(...), product: str = Form(""),
                 hs: str = Form(""), sources: str = Form("both"), refresh: str = Form("")):
    """Directional report for one product into one country (HTMX fragment)."""
    with Session(engine) as session:
        if not is_admin(current_user(request, session)):
            return _forbidden()
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


@app.post("/research/recommend", response_class=HTMLResponse)
def research_recommend(request: Request, product: str = Form(""), hs: str = Form(""),
                       sources: str = Form("both"), refresh: str = Form("")):
    """Where should I SELL product X? Rank the best destination countries (HTMX fragment)."""
    with Session(engine) as session:
        if not is_admin(current_user(request, session)):
            return _forbidden()
    label, hs_codes, _ = resolve_query(product, hs)
    if not hs_codes:
        return templates.TemplateResponse("partials/research_result.html",
            {"request": request, "error": "Pick a product from the list or type an HS code."})
    rows = recommend_destinations(hs_codes, our_sources=_sources_tuple(sources), refresh=bool(refresh))
    with Session(engine) as session:
        current_user(request, session)
    return templates.TemplateResponse("partials/research_result.html",
        {"request": request, "recommend": rows, "query_label": label, "recommend_hs": hs_codes[0]})


@app.post("/research/scan", response_class=HTMLResponse)
def research_scan(request: Request, reporter: str = Form(...), sources: str = Form("both"),
                  refresh: str = Form("")):
    """Rank a country's best import opportunities for Iran/UAE supply (HTMX fragment)."""
    with Session(engine) as session:
        if not is_admin(current_user(request, session)):
            return _forbidden()
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
        if not is_admin(user):          # buyer search is the founder's moat — admin only
            return _forbidden()
        jobs = session.exec(select(CommandJob).order_by(CommandJob.id.desc()).limit(25)).all()
    ctx = {"request": request, "user": user, "active": "command", "jobs": jobs, "can_run": True}
    return templates.TemplateResponse("command.html", ctx)


@app.post("/command/run", response_class=HTMLResponse)
def command_run(request: Request, background: BackgroundTasks, prompt: str = Form("")):
    """Parse the prompt, create a queued CommandJob, kick off the harvest in the background,
    and return the job card (which self-polls until done)."""
    with Session(engine) as session:
        user = current_user(request, session)
        if not is_admin(user):
            return HTMLResponse('<div class="text-rose-300 text-sm p-2">Buyer search is admin-only.</div>',
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
        if not is_admin(current_user(request, session)):
            return HTMLResponse("", status_code=403)
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
        if not lead or not owns(lead.owner_id, user):     # IDOR guard: no cross-tenant lead access
            return _not_found()
        if lead.managed and is_admin(user):               # audit any view of confidential buyer PII
            pipeline.audit(session, user, "lead", lead.id, "pii_view", {"via": "lead_detail"},
                           tenant_id=lead.seller_id); session.commit()
        products = {p.id: p for p in session.exec(select(Product)).all()}
        users = (session.exec(select(User).where(User.active == True)).all()  # noqa: E712
                 if is_admin(user) else [])
        matches = session.exec(
            select(Match).where(Match.lead_id == lead_id).order_by(Match.score.desc())
        ).all()
        quotes = session.exec(
            select(Quote).where(Quote.lead_id == lead_id).order_by(Quote.id.desc())
        ).all()
        acts = session.exec(
            select(Activity).where(Activity.lead_id == lead_id).order_by(Activity.id.desc())
        ).all()
        umap = ({u.id: u for u in session.exec(select(User)).all()} if is_admin(user)
                else ({user.id: user} if user else {}))
        owner = umap.get(lead.owner_id)
        # Full audit timeline — EXCLUDE 'outreach' (those messages now live in the Conversation panel)
        timeline = [{"a": a, "user": umap.get(a.user_id)} for a in acts if a.kind != "outreach"]
        outreach = session.exec(
            select(Outreach).where(Outreach.lead_id == lead_id).order_by(Outreach.id.desc())
        ).all()
        # Conversation thread: messages (in/out) + key system events, oldest -> newest (chat order)
        thread = ([{"kind": "msg", "at": o.created_at, "o": o, "user": umap.get(o.user_id)}
                   for o in outreach]
                  + [{"kind": "event", "at": a.created_at, "a": a}
                     for a in acts if a.kind in ("status_change", "quote_sent")])
        thread.sort(key=lambda x: x["at"] or datetime.min)
        # Only the founder builds quotes (fully-gated) — and the quote picker exposes EXW cost.
        quotable = sorted(
            (p for p in products.values() if _quotable(p)),
            key=lambda p: p.name) if is_admin(user) else []
        latest_q = quotes[0] if quotes else None
        latest_p = products.get(latest_q.product_id) if latest_q else None
        # Product lines get a branded KIMIEL first-touch; everything else the generic template.
        src = (lead.source or "")
        cat = (lead.category or "").lower()
        is_honey = src.startswith("iran-export-honey") or "honey" in cat
        is_zinc = src.startswith("iran-export-zinc-sulfate") or "zinc" in cat
        if is_zinc and not latest_q:
            default_subject, default_body = zinc_message(lead)
        elif is_honey and not latest_q:
            default_subject, default_body = honey_message(lead)
        else:
            default_subject, default_body = default_message(lead, latest_q, latest_p)
        # newest buyer-safe quote link to drop into a message (approved/sent quotes only)
        share_q = next((q for q in quotes if q.status in ("approved", "sent") and q.share_token), None)
        latest_share_url = f"{BASE_URL}/p/{share_q.share_token}" if share_q else ""
    return templates.TemplateResponse(
        "lead_detail.html",
        {"request": request, "user": user, "is_admin": is_admin(user), "lead": lead, "owner": owner,
         "users": users, "products": products, "quotable": quotable,
         "matches": [{"m": m, "product": products.get(m.product_id)} for m in matches],
         "quotes": quotes, "timeline": timeline, "outreach": outreach, "thread": thread,
         "default_subject": default_subject, "default_body": default_body,
         "latest_share_url": latest_share_url,
         "smtp_enabled": SMTP_ENABLED, "today": datetime.utcnow().date(),
         "next_stages": sorted(TRANSITIONS.get(lead.status, set()))},
    )


@app.post("/leads/{lead_id}/assign")
def assign_lead(request: Request, lead_id: int, owner_id: int = Form(...)):
    with Session(engine) as session:
        user = current_user(request, session)
        if not is_admin(user):          # only the founder reassigns lead ownership (delivery tool)
            return _forbidden()
        lead = session.get(Lead, lead_id)
        new_owner = session.get(User, owner_id)
        if lead and new_owner:
            lead.owner_id = new_owner.id
            session.add(lead)
            _cascade_owner(session, lead)       # move this lead's quotes + deals to the new owner too
            _log(session, lead, user, "assignment", f"assigned to {new_owner.name or new_owner.email}")
            session.commit()
    return RedirectResponse(f"/leads/{lead_id}", status_code=303)


def _open_deal_if_won(session, lead, user):
    """A won lead becomes a Deal (once), seeded from its accepted/sent quote. Shared by the CRM stage route
    and the managed-pipeline 'won' transition so the two paths behave identically."""
    if session.exec(select(Deal).where(Deal.lead_id == lead.id)).first():
        return None
    quote = (session.exec(select(Quote).where(Quote.lead_id == lead.id, Quote.status == "sent")
                          .order_by(Quote.id.desc())).first()
             or session.exec(select(Quote).where(Quote.lead_id == lead.id)
                             .order_by(Quote.id.desc())).first())
    deal = create_deal(session, lead, quote)
    _log(session, lead, user, "note", f"deal {deal.tracking_code} opened")
    return deal


@app.post("/leads/{lead_id}/stage")
def change_stage(request: Request, lead_id: int, status: str = Form(...), reason: str = Form("")):
    with Session(engine) as session:
        user = current_user(request, session)
        if not role_at_least(user, "agent"):
            return _forbidden()
        lead = session.get(Lead, lead_id)
        if not lead or not owns(lead.owner_id, user):
            return _not_found()
        if lead.managed:            # managed buyers move only through the 13-stage pipeline board (no drift)
            return RedirectResponse(f"/admin/requests/{lead.request_id}/pipeline", status_code=303)
        old = lead.status
        if status not in TRANSITIONS.get(old, set()):
            return RedirectResponse(f"/leads/{lead_id}?error=transition", status_code=303)
        if status == "lost" and not reason.strip():
            return RedirectResponse(f"/leads/{lead_id}?error=reason", status_code=303)
        # Gate 'won' on a real buyer commitment: an on-platform acceptance (lead.accepted_at, set
        # when the buyer accepts the pro-forma at /p/) OR an explicit override note. No silent wins.
        if status == "won" and lead.accepted_at is None and not reason.strip():
            return RedirectResponse(f"/leads/{lead_id}?error=accept", status_code=303)
        lead.status = status
        if status == "lost":
            lead.lost_reason = reason.strip()
        session.add(lead)
        _log(session, lead, user, "status_change",
             f"{old} -> {status}" + (f" ({reason.strip()})" if reason.strip() else ""))
        session.commit(); session.refresh(lead)
        if status == "won":
            _open_deal_if_won(session, lead, user); session.commit()
        notify_status_change(lead, old, status, user.name or user.email)
    return RedirectResponse(f"/leads/{lead_id}", status_code=303)


@app.post("/leads/{lead_id}/note")
def add_note(request: Request, lead_id: int, body: str = Form(...)):
    with Session(engine) as session:
        user = current_user(request, session)
        if not role_at_least(user, "agent"):
            return _forbidden()
        lead = session.get(Lead, lead_id)
        if lead and not owns(lead.owner_id, user):
            return _not_found()
        if lead and body.strip():
            _log(session, lead, user, "note", body.strip())
            session.commit()
    return RedirectResponse(f"/leads/{lead_id}", status_code=303)


@app.post("/leads/{lead_id}/delete")
def delete_lead(request: Request, lead_id: int):
    """Remove a lead the owner (or admin) added — for fixing a mistaken entry. Cleans up dependents."""
    with Session(engine) as session:
        user = current_user(request, session)
        if not role_at_least(user, "agent"):
            return _forbidden()
        lead = session.get(Lead, lead_id)
        if not lead or not owns(lead.owner_id, user):
            return _not_found()
        _delete_lead(session, lead)
        session.commit()
    return RedirectResponse("/leads", status_code=303)


# ----------------------------------------------------------------------------- catalog

@app.get("/catalog", response_class=HTMLResponse)
def catalog(request: Request, imported: int = 0, updated: int = 0, errors: int = 0):
    with Session(engine) as session:
        user = current_user(request, session)
        if not is_admin(user):          # catalog exposes EXW buy-cost (founder margin) — admin only
            return _forbidden()
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
        if not is_admin(current_user(request, session)):     # shared catalog/suppliers = admin-write only
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
        if not is_admin(current_user(request, session)):     # shared catalog/suppliers = admin-write only
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
        user = current_user(request, session)
        if not role_at_least(user, "agent"):
            return _forbidden()
        lead = session.get(Lead, lead_id)
        product = session.get(Product, product_id)
        if not lead or not product:
            return HTMLResponse("Not found", status_code=404)
        if not owns(lead.owner_id, user):
            return _not_found()
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
        user = current_user(request, session)
        if not role_at_least(user, "agent"):
            return _forbidden()
        lead = session.get(Lead, lead_id)
        product = session.get(Product, product_id)
        if not lead or not product:
            return HTMLResponse("Not found", status_code=404)
        if not owns(lead.owner_id, user):
            return _not_found()
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
        user = current_user(request, session)
        if not role_at_least(user, "agent"):
            return _forbidden()
        lead = session.get(Lead, lead_id)
        if not lead or not owns(lead.owner_id, user):
            return _not_found()
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
        if not lead or not owns(lead.owner_id, user):
            return _not_found()
        # first email to this lead? (used to arm the follow-up sequence + label the alert)
        prior_sent = session.exec(select(func.count()).where(
            Outreach.lead_id == lead.id, Outreach.direction == "out",
            Outreach.channel == "email", Outreach.status == "sent")).one()
        status, error, mid = "logged", "", ""
        to = recipient or lead.email
        if send and channel == "email":
            if SG.outreach_paused(session):
                _flash(request, "All outreach is paused — resume it before sending.", "rose")
                return RedirectResponse(f"/leads/{lead_id}", status_code=303)
            if SUP.is_suppressed(session, to, tenant_id=lead.seller_id):
                _flash(request, "That address is on the do-not-contact list — not sent.", "rose")
                return RedirectResponse(f"/leads/{lead_id}", status_code=303)
            body = SG.guard_buyer_text(session, body, lead.seller_id)   # buyers never learn the seller
            subject = SG.sanitize_header(subject)
            text, html = build_parts(body)                # append the KIMIEL signature (HTML + plain)
            ok, error, mid = send_email(SG.sanitize_header(to), subject, text, html=html)
            status = "sent" if ok else "failed"
        session.add(Outreach(
            lead_id=lead.id, direction="out", channel=channel,
            recipient=(recipient or lead.email or lead.phone or "")[:200],
            subject=subject[:200], body=body[:4000], status=status, error=error,
            message_id=mid, user_id=user.id if user else None))
        # arm the auto follow-up on the FIRST successful email (worker sends FU#1 at +FOLLOWUP_DAYS_1)
        if status == "sent" and prior_sent == 0 and FOLLOWUP_ENABLED:
            lead.next_action_at = datetime.utcnow() + timedelta(days=FOLLOWUP_DAYS_1)
            lead.next_action_note = "followup-1"
            session.add(lead)
        _log(session, lead, user, "outreach",
             f"{channel} to {recipient or lead.email or lead.phone or '?'} - {status}")
        session.commit()
        # Telegram alerts (best-effort; never block the response)
        if send and channel == "email":
            try:
                if status == "sent":
                    notify_outreach_sent(lead, subject, "Email")
                else:
                    notify_send_failed(lead, to, error or "send failed")
            except Exception:  # noqa: BLE001
                pass
    return RedirectResponse(f"/leads/{lead_id}", status_code=303)


_CAMPAIGN_STATUS = {
    "replied": ("\U0001F4B0 Replied", "#34d399"), "to-call": ("\U0001F4DE To call", "#fb7185"),
    "fu2": ("Follow-up 2 due", "#fbbf24"), "fu1": ("Follow-up 1 due", "#fbbf24"),
    "sent": ("Emailed", "#38bdf8"), "bounced": ("⚠ Bad email", "#fb7185"),
    "not-sent": ("Not emailed", "#94a3b8")}
_CAMPAIGN_RANK = {"replied": 0, "to-call": 1, "fu2": 2, "fu1": 3, "sent": 4, "bounced": 5, "not-sent": 6}


@app.get("/campaign", response_class=HTMLResponse)
def campaign_dashboard(request: Request, source: str = "iran-export-honey-royaljelly"):
    """Live outreach-campaign board: every buyer's status (emailed / follow-up 1-2 / replied / call).
    Admin-only — it renders buyer emails/contacts, so sellers must never reach it."""
    from collections import Counter, defaultdict
    with Session(engine) as session:
        user = current_user(request, session)
        if not is_admin(user):
            return _forbidden()
        leads = session.exec(select(Lead).where(Lead.source == source)).all()
        ids = [L.id for L in leads]
        outs = session.exec(select(Outreach).where(Outreach.lead_id.in_(ids))).all() if ids else []
        by = defaultdict(list)
        for o in outs:
            by[o.lead_id].append(o)
        rows, summ = [], Counter()
        for L in leads:
            olist = by.get(L.id, [])
            sent = [o for o in olist if o.direction == "out" and o.channel == "email" and o.status == "sent"]
            ins = [o for o in olist if o.direction == "in"]
            note = L.next_action_note or ""
            if L.buyer_replied_at:
                status = "replied"
            elif note in ("bounced", "bad-email"):
                status = "bounced"
            elif not sent:
                status = "not-sent"
            elif note == "call":
                status = "to-call"
            elif note == "followup-2":
                status = "fu2"
            elif note == "followup-1":
                status = "fu1"
            else:
                status = "sent"
            summ[status] += 1
            label, color = _CAMPAIGN_STATUS[status]
            latest_in = max(ins, key=lambda o: o.created_at or datetime.min) if ins else None
            rows.append({"lead": L, "label": label, "color": color, "rank": _CAMPAIGN_RANK[status],
                         "emails": len(sent),
                         "last_out": max((o.created_at for o in sent), default=None),
                         "reply": (latest_in.body or "")[:90] if latest_in else "",
                         "next_at": L.next_action_at})
        rows.sort(key=lambda r: (r["rank"], -r["emails"]))
    contacted = len(rows) - summ.get("not-sent", 0) - summ.get("bounced", 0)
    tiles = [("Contacted", contacted), ("Replied", summ.get("replied", 0)),
             ("Awaiting FU#1", summ.get("fu1", 0)), ("Awaiting FU#2", summ.get("fu2", 0)),
             ("To call", summ.get("to-call", 0)), ("Bad email", summ.get("bounced", 0))]
    return templates.TemplateResponse("campaign.html", {
        "request": request, "user": user, "active": "campaign", "rows": rows, "tiles": tiles,
        "source": source, "total": len(rows)})


@app.get("/leads/{lead_id}/quotation", response_class=HTMLResponse)
def kimiel_quotation_view(request: Request, lead_id: int):
    """Printable KIMIEL branded quotation for this buyer (Cmd/Ctrl+P -> Save as PDF). Meant for the
    FOLLOW-UP once a buyer replies — not attached to the cold first email."""
    with Session(engine) as session:
        user = current_user(request, session)
        lead = session.get(Lead, lead_id)
        if not lead or not owns(lead.owner_id, user):
            return _not_found()
        q = quotation_data(lead)
    return templates.TemplateResponse("kimiel_quotation.html", {"request": request, "q": q, "user": user})


@app.post("/leads/{lead_id}/enrich")
def lead_enrich(request: Request, lead_id: int):
    """Scrape this buyer's own website for a role mailbox (+ tel: phone) and fill blank contacts.
    No third-party credits; only fills fields that are empty. Provenance is logged by enrich_lead."""
    with Session(engine) as session:
        user = current_user(request, session)
        if not role_at_least(user, "agent"):
            return _forbidden()
        lead = session.get(Lead, lead_id)
        if not lead or not owns(lead.owner_id, user):
            return _not_found()
        enrich_lead(session, lead, apply=True)
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
        if not lead or not owns(lead.owner_id, user):
            return _not_found()
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
        quotes = session.exec(scoped(select(Quote), Quote.owner_id, user).order_by(Quote.id.desc())).all()
        lead_ids = {q.lead_id for q in quotes} or {0}
        leads = {l.id: l for l in session.exec(select(Lead).where(Lead.id.in_(lead_ids))).all()}
        products = {p.id: p for p in session.exec(select(Product)).all()}
        rows = [{"q": q, "lead": leads.get(q.lead_id), "product": products.get(q.product_id)}
                for q in quotes]
    return templates.TemplateResponse("quotes_list.html", {"request": request, "user": user, "rows": rows})


@app.get("/quotes/sample", response_class=HTMLResponse)
def quote_sample(request: Request):
    """A print-ready SAMPLE pro-forma so a trader sees exactly what a buyer receives. Static, no data.
    Declared BEFORE /quotes/{quote_id} so 'sample' isn't matched as an int id."""
    today = datetime.utcnow().date()
    return templates.TemplateResponse("quote_sample.html", {
        "request": request, "issued": today.strftime("%d %b %Y"),
        "valid_until": (today + timedelta(days=14)).strftime("%d %b %Y")})


def _ensure_share_token(session, q) -> str:
    """Give the quote an unguessable public-link token if it doesn't have one yet."""
    if not (q.share_token or "").strip():
        q.share_token = secrets.token_urlsafe(12)
        session.add(q)
        session.commit()
    return q.share_token


@app.get("/quotes/{quote_id}", response_class=HTMLResponse)
def quote_detail(request: Request, quote_id: int):
    with Session(engine) as session:
        user = current_user(request, session)
        q = session.get(Quote, quote_id)
        if not q or not owns(q.owner_id, user):     # IDOR guard: no cross-tenant quote access
            return _not_found()
        lead = session.get(Lead, q.lead_id)
        product = session.get(Product, q.product_id)
        breakdown = json.loads(q.breakdown or "[]")
        fx = json.loads(q.fx_snapshot or "{}")
        # a shareable buyer link exists once the quote is a real offer (approved/sent). Read-only:
        # the token is MINTED in the approve/send POST handlers, never on this GET (no write-on-read,
        # so a read-only viewer can't trigger a commit or conjure a link).
        share_url = ""
        if q.status in ("approved", "sent") and (q.share_token or "").strip():
            share_url = f"{BASE_URL}/p/{q.share_token}"
    return templates.TemplateResponse(
        "quote_detail.html",
        {"request": request, "user": user, "q": q, "lead": lead, "product": product,
         "breakdown": breakdown, "fx": fx, "can_approve": role_at_least(user, "agent"),
         "share_url": share_url},
    )


@app.get("/p/{token}", response_class=HTMLResponse)
def public_proforma(request: Request, token: str):
    """Buyer-facing pro-forma at an unguessable link — NO internal costs (breakdown, EXW, or margin
    are never rendered here). Public (auth-exempt); only approved/sent quotes are viewable."""
    if not (token or "").strip():
        return HTMLResponse("Not found", status_code=404)
    with Session(engine) as session:
        q = session.exec(select(Quote).where(Quote.share_token == token)).first()
        if not q or q.status not in ("approved", "sent"):
            return HTMLResponse("This quotation link is not available.", status_code=404)
        lead = session.get(Lead, q.lead_id)
        product = session.get(Product, q.product_id)
        fx = json.loads(q.fx_snapshot or "{}")
        expires = None
        if q.created_at and q.validity_days:
            expires = q.created_at + timedelta(days=q.validity_days)
    return templates.TemplateResponse(
        "proforma_public.html",
        {"request": request, "q": q, "lead": lead, "product": product, "fx": fx,
         "expires": expires, "today": datetime.utcnow()},
    )


@app.post("/p/{token}/respond", response_class=HTMLResponse)
def public_proforma_respond(request: Request, token: str, action: str = Form(...),
                            message: str = Form("")):
    """Buyer ACCEPTS or requests CHANGES on the public pro-forma — captured ON-PLATFORM (this is the
    'buyer said yes' moment). Public (auth-exempt, under /p/). Records the response on the quote +
    lead, threads an inbound message into the Conversation, alerts the team. No internal data shown."""
    action = (action or "").strip().lower()
    if action not in ("accept", "changes"):
        return HTMLResponse("Bad request", status_code=400)
    with Session(engine) as session:
        q = session.exec(select(Quote).where(Quote.share_token == token)).first()
        if not q or q.status not in ("approved", "sent"):
            return HTMLResponse("This quotation link is not available.", status_code=404)
        lead = session.get(Lead, q.lead_id)
        now = datetime.utcnow()
        msg = (message or "").strip()[:2000]
        if action == "accept":
            q.buyer_response = "accepted"
            q.accepted_at = now
            if lead:
                lead.accepted_at = lead.accepted_at or now
                if lead.status in ("new", "quoted"):
                    lead.status = "negotiating"
            body = f"Buyer ACCEPTED pro-forma {q.tracking_code}." + (f" Note: {msg}" if msg else "")
            kind, alert = "quote_accepted", f"BUYER ACCEPTED {q.tracking_code}"
        else:
            q.buyer_response = "changes"
            if lead and lead.status in ("new", "quoted"):
                lead.status = "negotiating"
            body = f"Buyer requested CHANGES on {q.tracking_code}." + (f" {msg}" if msg else "")
            kind, alert = "quote_changes", f"Buyer requested changes on {q.tracking_code}"
        session.add(q)
        if lead:
            if lead.buyer_replied_at is None:
                lead.buyer_replied_at = now
            session.add(Outreach(lead_id=lead.id, direction="in", channel="portal",
                                 from_addr=(lead.email or "buyer"),
                                 subject=f"Pro-forma {q.tracking_code}", body=body, status="received"))
            _log(session, lead, None, kind, body)
            session.add(lead)
        code = q.tracking_code
        lead_company = lead.buyer_company if lead else ""
        lead_code = lead.tracking_code if lead else ""
        session.commit()
    try:
        send_message(f"{alert} - {lead_company or 'buyer'} ({lead_code})")
    except Exception:  # noqa: BLE001
        pass
    return templates.TemplateResponse("proforma_thanks.html",
        {"request": request, "accepted": action == "accept", "code": code})


@app.post("/quotes/{quote_id}/approve")
def approve_quote(request: Request, quote_id: int):
    with Session(engine) as session:
        user = current_user(request, session)
        if not role_at_least(user, "agent"):    # agent+ can approve (solo-operator friendly)
            return _forbidden()
        q = session.get(Quote, quote_id)
        if q and not owns(q.owner_id, user):
            return _not_found()
        if q and q.status == "draft":
            q.status = "approved"
            q.approved_by = user.email
            session.add(q)
            _ensure_share_token(session, q)     # buyer link ready to preview/share
            session.commit()
    return RedirectResponse(f"/quotes/{quote_id}", status_code=303)


@app.post("/quotes/{quote_id}/send")
def send_quote(request: Request, quote_id: int):
    with Session(engine) as session:
        user = current_user(request, session)
        if not role_at_least(user, "agent"):     # agent+ can send (solo-operator friendly)
            return _forbidden()
        q = session.get(Quote, quote_id)
        if q and not owns(q.owner_id, user):
            return _not_found()
        if q and q.status == "approved":
            q.status = "sent"
            _ensure_share_token(session, q)
            session.add(q)
            # Supersede any prior live quote for this lead so its public /p/ link stops serving an
            # outdated price (the public route only serves approved/sent — superseded ones 404).
            for old in session.exec(select(Quote).where(
                    Quote.lead_id == q.lead_id, Quote.id != q.id,
                    Quote.status.in_(["approved", "sent"]))).all():
                old.status = "superseded"
                session.add(old)
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
        deals = session.exec(scoped(select(Deal), Deal.owner_id, user).order_by(Deal.id.desc())).all()
        lead_ids = {d.lead_id for d in deals} or {0}
        leads = {l.id: l for l in session.exec(select(Lead).where(Lead.id.in_(lead_ids))).all()}
        rows = [{"d": d, "lead": leads.get(d.lead_id)} for d in deals]
    return templates.TemplateResponse("deals_list.html", {"request": request, "user": user, "rows": rows})


@app.get("/deals/{deal_id}", response_class=HTMLResponse)
def deal_detail(request: Request, deal_id: int):
    with Session(engine) as session:
        user = current_user(request, session)
        deal = session.get(Deal, deal_id)
        if not deal or not owns(deal.owner_id, user):     # IDOR guard: no cross-tenant deal access
            return _not_found()
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
        user = current_user(request, session)
        if not role_at_least(user, "agent"):
            return _forbidden()
        deal = session.get(Deal, deal_id)
        if not deal or not owns(deal.owner_id, user):
            return _not_found()
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
        if lead:
            _log(session, lead, user, "status_change", f"deal -> {nxt}")
        session.commit()
    return RedirectResponse(f"/deals/{deal_id}", status_code=303)


# Trade-document storage. Files live OUTSIDE the repo tree's tracked area (deal_docs/ is gitignored);
# they are served only to agent+ via the guarded download route below, never publicly.
DEAL_DOCS_DIR = BASE_DIR.parent / "deal_docs"
MAX_DOC_BYTES = 20 * 1024 * 1024


def _safe_name(name):
    base = os.path.basename(name or "")
    keep = "".join(ch if (ch.isalnum() or ch in "._-") else "_" for ch in base).strip("._")
    return (keep or "file")[:80]


def _save_deal_doc(deal_id, doc_id, upload):
    """Persist an uploaded trade document under deal_docs/<deal_id>/<doc_id>_<name>. Returns the
    stored relative path, or '' when empty/too large (the doc then stays metadata-only)."""
    data = upload.file.read(MAX_DOC_BYTES + 1)
    if not data or len(data) > MAX_DOC_BYTES:
        return ""
    dest_dir = DEAL_DOCS_DIR / str(deal_id)
    dest_dir.mkdir(parents=True, exist_ok=True)
    fname = f"{doc_id}_{_safe_name(upload.filename)}"
    (dest_dir / fname).write_bytes(data)
    return f"{deal_id}/{fname}"


@app.post("/deals/{deal_id}/docs")
def add_doc(request: Request, deal_id: int, doc_type: str = Form(...),
            reference_no: str = Form(""), issued_by: str = Form(""), expires_at: str = Form(""),
            file: UploadFile = File(None)):
    with Session(engine) as session:
        user = current_user(request, session)
        if not role_at_least(user, "agent"):
            return _forbidden()
        deal = session.get(Deal, deal_id)
        if not deal or not owns(deal.owner_id, user):
            return _not_found()
        exp = None
        if expires_at.strip():
            try:
                exp = datetime.strptime(expires_at.strip(), "%Y-%m-%d")
            except ValueError:
                exp = None
        doc = ComplianceDoc(deal_id=deal_id, doc_type=doc_type, reference_no=reference_no,
                            issued_by=issued_by, expires_at=exp, status="received")
        session.add(doc)
        session.commit()
        session.refresh(doc)
        if file is not None and (file.filename or "").strip():
            rel = _save_deal_doc(deal_id, doc.id, file)
            if rel:
                doc.file_path = rel
                session.add(doc)
                session.commit()
    return RedirectResponse(f"/deals/{deal_id}", status_code=303)


@app.get("/deals/{deal_id}/docs/{doc_id}/file")
def download_doc(request: Request, deal_id: int, doc_id: int):
    """Download a trade document's attached file — agent+ only, never public. Path is built by us
    (deal_docs/<deal>/<doc>_<name>), and re-validated to stay inside DEAL_DOCS_DIR (no traversal)."""
    with Session(engine) as session:
        user = current_user(request, session)
        if not role_at_least(user, "agent"):
            return _forbidden()
        deal = session.get(Deal, deal_id)
        if not deal or not owns(deal.owner_id, user):     # check the PARENT deal's owner, not just the FK
            return _not_found()
        doc = session.get(ComplianceDoc, doc_id)
        if not doc or doc.deal_id != deal_id or not doc.file_path:
            return HTMLResponse("Not found", status_code=404)
        path = (DEAL_DOCS_DIR / doc.file_path).resolve()
        if not str(path).startswith(str(DEAL_DOCS_DIR.resolve()) + os.sep) or not path.exists():
            return HTMLResponse("Not found", status_code=404)
        fname = os.path.basename(doc.file_path)
    return FileResponse(str(path), filename=fname)


@app.post("/deals/{deal_id}/docs/{doc_id}/verify")
def verify_doc(request: Request, deal_id: int, doc_id: int):
    with Session(engine) as session:
        user = current_user(request, session)
        if not role_at_least(user, "agent"):
            return _forbidden()
        deal = session.get(Deal, deal_id)
        if not deal or not owns(deal.owner_id, user):
            return _not_found()
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
        user = current_user(request, session)
        if not role_at_least(user, "manager"):
            return _forbidden()
        deal = session.get(Deal, deal_id)
        if deal and not owns(deal.owner_id, user):
            return _not_found()
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
        if not is_admin(user):          # data ingestion is an admin operation
            return _forbidden()
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
        if not is_admin(current_user(request, session)):     # rates / FX / ingest = admin only
            return _forbidden()
    background.add_task(ingest_source, Go4WorldCsvSource(INBOX_DIR))   # non-blocking
    return RedirectResponse("/ingest", status_code=303)


@app.post("/ingest/upload")
async def ingest_upload(request: Request, background: BackgroundTasks, file: UploadFile = File(...)):
    """Browser CSV upload: save into the inbox then kick off ingestion in the background."""
    with Session(engine) as session:
        if not is_admin(current_user(request, session)):     # rates / FX / ingest = admin only
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
        if not is_admin(user):          # pricing params / margins / FX are founder-internal
            return _forbidden()
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
        if not is_admin(current_user(request, session)):     # rates / FX / ingest = admin only
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
        if not is_admin(current_user(request, session)):     # rates / FX / ingest = admin only
            return _forbidden()
        _set_card(session, "inland", inland_per_truck, capacity=truck_capacity)
        _set_card(session, "international", intl_per_truck, lane_to=dest_border, capacity=truck_capacity)
        session.commit()
    return RedirectResponse("/rates", status_code=303)


@app.post("/rates/fx")
def update_fx(request: Request, base: str = Form(...), rate: float = Form(..., gt=0)):
    with Session(engine) as session:
        if not is_admin(current_user(request, session)):     # rates / FX / ingest = admin only
            return _forbidden()
        base = base.strip().upper()
        fx = session.exec(select(FxRate).where(FxRate.base == base, FxRate.quote == "USD")).first()
        if fx is None:
            fx = FxRate(base=base, quote="USD")
        fx.rate = rate
        session.add(fx)
        session.commit()
    return RedirectResponse("/rates", status_code=303)


# ----------------------------------------------------------------------------- concierge requests
# A trader submits a request (buyer search first); the founder is pinged, approves, fulfils, and the
# result (delivered buyers) lands in the trader's OWN account (owner_id = requester). Fully isolated.

# The concierge service catalog. buyer_hunt delivers leads (via scripts/deliver_request.py); the other
# services deliver a file/summary the founder uploads. All share the one request lifecycle.
SERVICES = [
    {"key": "buyer_hunt", "icon": "\U0001F3AF", "label": "Find buyers",
     "blurb": "We research and deliver real, contactable buyers for your product.",
     "p_label": "Your product", "p_ph": "e.g. Zinc sulphate, saffron, handmade tiles…",
     "m_label": "Target market", "m_ph": "e.g. Iraq, GCC, Turkey, EU…",
     "d_ph": "Grade/spec, monthly quantity, and the kind of buyer you want (distributor, factory, importer)…"},
    {"key": "remittance", "icon": "\U0001F4B1", "label": "Remittance / Sarafi",
     "blurb": "Get paid across borders the safe way — crypto, hawala, LC or third-country settlement.",
     "p_label": "Amount to move", "p_ph": "e.g. USD 15,000 from a buyer",
     "m_label": "From → To", "m_ph": "e.g. buyer in Iraq → you in Iran",
     "d_ph": "Currency, who is sending & receiving, the deadline, and your preferred method (crypto / hawala / bank / LC)…"},
    {"key": "contract", "icon": "\U0001F4DD", "label": "Contract drafting",
     "blurb": "A professional sales contract / agreement, drafted and delivered as a PDF.",
     "p_label": "What's the deal for", "p_ph": "e.g. 20 MT zinc sulphate to a buyer in Iraq",
     "m_label": "Buyer's country", "m_ph": "e.g. Iraq",
     "d_ph": "Parties (names/companies), quantity & price, Incoterm (EXW/CPT…), payment terms, delivery date…"},
    {"key": "freight", "icon": "\U0001F69A", "label": "Freight & shipping",
     "blurb": "A lane quote and booking to move your goods to the buyer.",
     "p_label": "What are we shipping", "p_ph": "e.g. 25 MT zinc sulphate in 25 kg bags",
     "m_label": "From → To", "m_ph": "e.g. Bandar Abbas → Basra, Iraq",
     "d_ph": "Total weight/volume, packaging, pickup & delivery addresses, and your target date…"},
    {"key": "docs", "icon": "\U0001F4C4", "label": "Documentation",
     "blurb": "Certificate of Origin, commercial invoice, packing list and other trade documents.",
     "p_label": "Shipment / product", "p_ph": "e.g. Zinc sulphate shipment to Iraq",
     "m_label": "Destination", "m_ph": "e.g. Iraq",
     "d_ph": "Which documents you need (CoO, invoice, packing list…), the consignee, values, and HS code…"},
    # Phase 3: additive request types (backend keys are new; existing values are untouched). find_supplier is
    # the buy-side counterpart of buyer_hunt; market_research + other round out the concierge menu.
    {"key": "find_supplier", "icon": "\U0001F50E", "label": "Find a product or supplier",
     "blurb": "We source real, vetted suppliers/manufacturers for a product you want to buy.",
     "p_label": "Product you want", "p_ph": "e.g. Copper cathode, urea, ceramic tiles…",
     "m_label": "Preferred origin", "m_ph": "e.g. Turkey, China, GCC, any…",
     "d_ph": "Grade/spec, quantity, target price, delivery terms, and the kind of supplier you want…"},
    {"key": "market_research", "icon": "\U0001F4CA", "label": "Market research",
     "blurb": "A focused market/opportunity brief for a product and destination.",
     "p_label": "Product / sector", "p_ph": "e.g. Saffron in the GCC",
     "m_label": "Target market", "m_ph": "e.g. UAE, Iraq, EU…",
     "d_ph": "What decision this informs, the market(s), and any competitors or price points to check…"},
    {"key": "other", "icon": "\U0001F91D", "label": "Other concierge service",
     "blurb": "Any other operational trade service — tell us what you need.",
     "p_label": "What do you need", "p_ph": "e.g. Inspection, warehousing, introductions…",
     "m_label": "Where / route", "m_ph": "e.g. Bandar Abbas, Iraq…",
     "d_ph": "Describe the service, the parties involved, and your deadline…"},
]
REQUEST_TYPES = {s["key"]: s["label"] for s in SERVICES}

# Delivered service files (contract PDFs, remittance confirmations, ...) live outside the repo tree
# (request_files/ is gitignored) and are served only to the owning trader via the guarded route below.
REQUEST_FILES_DIR = BASE_DIR.parent / "request_files"


def _save_request_file(req_id, upload):
    """Persist a delivered file under request_files/<req_id>/<name>. Returns the stored relative path,
    or '' when empty/too large. Reuses the deal-doc size cap + safe-name sanitizer."""
    data = upload.file.read(MAX_DOC_BYTES + 1)
    if not data or len(data) > MAX_DOC_BYTES:
        return ""
    dest_dir = REQUEST_FILES_DIR / str(req_id)
    dest_dir.mkdir(parents=True, exist_ok=True)
    fname = _safe_name(upload.filename)
    (dest_dir / fname).write_bytes(data)
    return f"{req_id}/{fname}"


def _save_deliverable_file(req_id, deliverable_id, upload):
    """Persist a re-deliverable file named <deliverable_id>_<name> so repeated deliveries never collide."""
    data = upload.file.read(MAX_DOC_BYTES + 1)
    if not data or len(data) > MAX_DOC_BYTES:
        return ""
    dest_dir = REQUEST_FILES_DIR / str(req_id)
    dest_dir.mkdir(parents=True, exist_ok=True)
    fname = f"{deliverable_id}_{_safe_name(upload.filename)}"
    (dest_dir / fname).write_bytes(data)
    return f"{req_id}/{fname}"


def _deliverables_for(session, req_ids):
    """{request_id: [RequestDeliverable…]} oldest-first, for rendering the deliverables list on a card."""
    out = {}
    if not req_ids:
        return out
    for d in session.exec(select(RequestDeliverable).where(RequestDeliverable.request_id.in_(req_ids))
                          .order_by(RequestDeliverable.id)).all():
        out.setdefault(d.request_id, []).append(d)
    return out


def _messages_for(session, req_ids):
    """{request_id: [RequestMessage…]} oldest-first, for the per-request chat thread."""
    out = {}
    if not req_ids:
        return out
    for m in session.exec(select(RequestMessage).where(RequestMessage.request_id.in_(req_ids))
                          .order_by(RequestMessage.id)).all():
        out.setdefault(m.request_id, []).append(m)
    return out


def _flash(request, msg, level="emerald"):
    request.session.setdefault("_flash", []).append({"msg": msg, "level": level})


@app.post("/requests")
def submit_request(request: Request, product: str = Form(""), market: str = Form(""),
                   details: str = Form(""), request_type: str = Form("buyer_hunt")):
    with Session(engine) as session:
        user = current_user(request, session)
        if not role_at_least(user, "agent"):
            return _forbidden()
        product = (product or "").strip()
        if not product:
            _flash(request, "Please enter the product you want buyers for.", "rose")
            return RedirectResponse("/requests", status_code=303)
        rtype = request_type if request_type in REQUEST_TYPES else "buyer_hunt"
        sr = ServiceRequest(request_type=rtype, product=product[:200], market=(market or "").strip()[:120],
                            details=(details or "").strip()[:2000], status="submitted",
                            direction=RS.direction_for_type(rtype), workflow_status="submitted",
                            last_activity_at=datetime.utcnow(), requester_id=user.id, owner_id=user.id)
        session.add(sr); session.commit(); session.refresh(sr)
        sr.tracking_code = f"SR-{datetime.utcnow():%Y%m}-{sr.id:04d}"
        sr.result_source_tag = f"req-{sr.id}"
        session.add(sr); session.commit(); session.refresh(sr)
        # Phase 3: non-blocking Work Queue item — the request is already committed; task creation can NEVER
        # break submission (mirrors the Trade Network link hook).
        WQ.create_work_item_safe(session, actor=None, type="review_new_request",
                                 title=f"Review new request {sr.tracking_code}",
                                 description="A new concierge request is awaiting review.",
                                 tenant_id=sr.owner_id, related_request_id=sr.id,
                                 idempotency_key=f"review_new_request:req:{sr.id}", condition_version="submitted")
        session.commit()
        try:
            notify_service_request(sr, user)
        except Exception:  # noqa: BLE001
            pass
        _flash(request, f"Request {sr.tracking_code} sent to admin. Your buyers will appear here once it's done.")
    return RedirectResponse("/requests", status_code=303)


@app.get("/requests", response_class=HTMLResponse)
def my_requests(request: Request):
    with Session(engine) as session:
        user = current_user(request, session)
        reqs = session.exec(scoped(select(ServiceRequest), ServiceRequest.owner_id, user)
                            .order_by(ServiceRequest.id.desc())).all()
        ids = [r.id for r in reqs]
        deliv_map, msg_map = _deliverables_for(session, ids), _messages_for(session, ids)
        if not is_admin(user):      # sellers only ever see files the admin marked PII-free
            deliv_map = {rid: [d for d in dvs if d.seller_safe] for rid, dvs in deliv_map.items()}
        # anonymized pipeline view (never raw Leads): funnel + anon prospects + published updates
        funnel_map, prospects_map, updates_map = {}, {}, {}
        for r in reqs:
            funnel_map[r.id] = pipeline.request_funnel(session, r)
            mleads = session.exec(select(Lead).where(
                Lead.request_id == r.id, Lead.managed == True,               # noqa: E712
                Lead.seller_id == r.owner_id).order_by(Lead.pipeline_stage, Lead.id)).all()
            prospects_map[r.id] = [pipeline.anon_prospect(m) for m in mleads]
            updates_map[r.id] = session.exec(select(SellerUpdate).where(
                SellerUpdate.request_id == r.id, SellerUpdate.published == True)  # noqa: E712
                .order_by(SellerUpdate.id.desc())).all()
    flashes = request.session.pop("_flash", [])
    return templates.TemplateResponse("requests.html", {
        "request": request, "user": user, "active": "requests", "reqs": reqs,
        "types": REQUEST_TYPES, "flashes": flashes,
        "deliv_map": deliv_map, "msg_map": msg_map, "me": user,
        "funnel_map": funnel_map, "prospects_map": prospects_map, "updates_map": updates_map})


@app.get("/requests/{req_id}/status", response_class=HTMLResponse)
def request_status(request: Request, req_id: int):
    with Session(engine) as session:
        user = current_user(request, session)
        sr = session.get(ServiceRequest, req_id)
        if not sr or not owns(sr.owner_id, user):
            return HTMLResponse("", status_code=404)
        deliv_map, msg_map = _deliverables_for(session, [sr.id]), _messages_for(session, [sr.id])
        return templates.TemplateResponse("partials/request_card.html", {
            "request": request, "r": sr, "deliv_map": deliv_map, "msg_map": msg_map, "me": user})


@app.get("/requests/{req_id}/thread", response_class=HTMLResponse)
def request_thread(request: Request, req_id: int):
    """Just the chat message bubbles for a request — the 5s poll target (owning trader or admin)."""
    with Session(engine) as session:
        user = current_user(request, session)
        sr = session.get(ServiceRequest, req_id)
        if not sr or not owns(sr.owner_id, user):
            return HTMLResponse("", status_code=404)
        msgs = _messages_for(session, [sr.id]).get(sr.id, [])
        return templates.TemplateResponse("partials/request_thread_messages.html", {
            "request": request, "msgs": msgs, "me": user})


@app.get("/services", response_class=HTMLResponse)
def services_page(request: Request):
    """The concierge menu: a card per service, each opening a request form. Any logged-in user."""
    with Session(engine) as session:
        user = current_user(request, session)
    flashes = request.session.pop("_flash", [])
    return templates.TemplateResponse("services.html", {
        "request": request, "user": user, "active": "services", "services": SERVICES, "flashes": flashes})


@app.get("/requests/{req_id}/result/file")
def request_result_file(request: Request, req_id: int):
    """Download a delivered service file — the owning trader (or admin) only, never public. Path is
    built by us and re-validated to stay inside REQUEST_FILES_DIR (no traversal)."""
    with Session(engine) as session:
        user = current_user(request, session)
        sr = session.get(ServiceRequest, req_id)
        if not sr or not owns(sr.owner_id, user):
            return _not_found()
        rel = sr.result_file_path
        if not is_admin(user):     # sellers only ever get the latest deliverable the admin marked PII-free
            dv = session.exec(select(RequestDeliverable).where(
                RequestDeliverable.request_id == sr.id, RequestDeliverable.seller_safe == True,  # noqa: E712
                RequestDeliverable.file_path != "").order_by(RequestDeliverable.id.desc())).first()
            rel = dv.file_path if dv else ""
        if not rel:
            return _not_found()
        path = (REQUEST_FILES_DIR / rel).resolve()
        if not str(path).startswith(str(REQUEST_FILES_DIR.resolve()) + os.sep) or not path.exists():
            return _not_found()
        fname = os.path.basename(rel)
    return FileResponse(str(path), filename=fname)


REQUEST_VIEWS = {"new": "New", "needs_review": "Needs review", "my_active": "My active",
                 "unassigned": "Unassigned", "waiting_requester": "Waiting requester",
                 "waiting_external": "Waiting external", "ready": "Ready to deliver", "overdue": "Overdue",
                 "completed": "Completed", "closed": "Rejected/cancelled"}


@app.get("/admin/requests", response_class=HTMLResponse)
def admin_requests(request: Request, view: str = "", q: str = "", direction: str = "", rtype: str = "",
                   status: str = "", priority: str = "", assignee: str = "", action: str = "",
                   overdue: str = "", page: int = 1):
    with Session(engine) as session:
        user = current_user(request, session)
        if not is_admin(user):
            return _forbidden()
        now = datetime.utcnow()
        stmt = select(ServiceRequest)
        q = (q or "").strip()
        if q:
            like = f"%{q}%"
            stmt = stmt.where(or_(ServiceRequest.product.ilike(like), ServiceRequest.market.ilike(like),
                                  ServiceRequest.details.ilike(like), ServiceRequest.tracking_code.ilike(like)))
        if direction in ("sell", "buy", "service"):
            stmt = stmt.where(ServiceRequest.direction == direction)
        if rtype in REQUEST_TYPES:
            stmt = stmt.where(ServiceRequest.request_type == rtype)
        if status in ("submitted", "approved", "running", "done", "rejected"):
            stmt = stmt.where(ServiceRequest.status == status)
        if priority in ("low", "normal", "high", "urgent"):
            stmt = stmt.where(ServiceRequest.priority == priority)
        if assignee == "me":
            stmt = stmt.where(ServiceRequest.assigned_admin_id == user.id)
        elif assignee == "none":
            stmt = stmt.where(ServiceRequest.assigned_admin_id.is_(None))
        elif assignee.isdigit():
            stmt = stmt.where(ServiceRequest.assigned_admin_id == int(assignee))
        if action == "admin":
            stmt = stmt.where(ServiceRequest.action_required_admin == True)   # noqa: E712
        elif action == "requester":
            stmt = stmt.where(ServiceRequest.action_required_requester == True)   # noqa: E712
        if overdue == "1":
            stmt = stmt.where(ServiceRequest.due_at.is_not(None), ServiceRequest.due_at < now,
                              ServiceRequest.status.not_in(("done", "rejected")))
        # saved views (built on reliably-populated columns)
        if view in ("new", "needs_review"):
            stmt = stmt.where(ServiceRequest.status == "submitted")
        elif view == "my_active":
            stmt = stmt.where(ServiceRequest.assigned_admin_id == user.id,
                              ServiceRequest.status.in_(("approved", "running")))
        elif view == "unassigned":
            stmt = stmt.where(ServiceRequest.assigned_admin_id.is_(None),
                              ServiceRequest.status.in_(("submitted", "approved", "running")))
        elif view == "waiting_requester":
            stmt = stmt.where(ServiceRequest.action_required_requester == True)   # noqa: E712
        elif view == "waiting_external":
            stmt = stmt.where(ServiceRequest.workflow_status == "waiting_external")
        elif view == "ready":
            stmt = stmt.where(ServiceRequest.workflow_status == "ready_for_delivery")
        elif view == "overdue":
            stmt = stmt.where(ServiceRequest.due_at.is_not(None), ServiceRequest.due_at < now,
                              ServiceRequest.status.not_in(("done", "rejected")))
        elif view == "completed":
            stmt = stmt.where(ServiceRequest.status == "done")
        elif view == "closed":
            stmt = stmt.where(ServiceRequest.status == "rejected")
        per = 40
        total = session.exec(select(func.count()).select_from(stmt.subquery())).one()
        pages = max(1, (total + per - 1) // per)
        page = max(1, min(page, pages))
        reqs = session.exec(stmt.order_by(ServiceRequest.id.desc()).offset((page - 1) * per).limit(per)).all()
        umap = {u.id: u for u in session.exec(select(User)).all()}
        ids = [r.id for r in reqs]
        deliv_map, msg_map = _deliverables_for(session, ids), _messages_for(session, ids)
        wq_counts = (dict(session.exec(select(WorkItem.related_request_id, func.count()).where(
            WorkItem.related_request_id.in_(ids), WorkItem.status.in_(WQ.NONTERMINAL))
            .group_by(WorkItem.related_request_id)).all()) if ids else {})
        unread_ids = set()
        if ids:
            by_id = {r.id: r for r in reqs}
            last_req = dict(session.exec(select(RequestMessage.request_id, func.max(RequestMessage.created_at))
                            .where(RequestMessage.request_id.in_(ids), RequestMessage.sender_role != "admin")
                            .group_by(RequestMessage.request_id)).all())
            for rid, ts in last_req.items():
                r = by_id.get(rid)
                if r and ts and (r.admin_last_read_at is None or ts > r.admin_last_read_at):
                    unread_ids.add(rid)
        eff_of = {r.id: RS.effective_workflow(r) for r in reqs}
        f = {"view": view, "q": q, "direction": direction, "rtype": rtype, "status": status,
             "priority": priority, "assignee": assignee, "action": action, "overdue": overdue}
        hdr = {"section": "Concierge", "title": "Requests",
               "desc": "Buyer-search, buy-side and service requests — the operational source of truth.",
               "count": total}
        return templates.TemplateResponse("admin_requests.html", {
            "request": request, "user": user, "active": "requests", "reqs": reqs, "umap": umap,
            "deliv_map": deliv_map, "msg_map": msg_map, "me": user, "wq_counts": wq_counts,
            "unread_ids": unread_ids, "now": now, "f": f, "total": total, "page": page, "pages": pages,
            "hdr": hdr, "REQUEST_VIEWS": REQUEST_VIEWS, "REQUEST_TYPES": REQUEST_TYPES,
            "staff": _staff(session), "wf_label": RS.WORKFLOW_LABELS, "wf_badge": RS.WORKFLOW_BADGE,
            "eff_of": eff_of, "PRIORITY_BADGE": WQ.PRIORITY_BADGE})


@app.get("/admin/requests/count", response_class=HTMLResponse)
def admin_requests_count(request: Request):
    """Tiny HTMX fragment: the count of pending (submitted) requests, for the admin nav badge."""
    with Session(engine) as session:
        if not is_admin(current_user(request, session)):
            return HTMLResponse("")
        n = session.exec(select(func.count(ServiceRequest.id))
                         .where(ServiceRequest.status == "submitted")).one()
    return HTMLResponse(f'<span class="badge badge-amber ml-1">{n}</span>' if n else "")


@app.get("/admin/requests/{req_id}", response_class=HTMLResponse)
def admin_request_detail(request: Request, req_id: int, tab: str = "summary"):
    """The tabbed admin request workspace (Summary · Pipeline & research · Communication · Work items ·
    Files · Related). Admin-only. Presents existing results — it never rewrites Research/pipeline logic."""
    with Session(engine) as session:
        user = current_user(request, session)
        if not is_admin(user):
            return _forbidden()
        sr = session.get(ServiceRequest, req_id)
        if not sr:
            return _not_found()
        requester = session.get(User, sr.requester_id) if sr.requester_id else None
        assignee = session.get(User, sr.assigned_admin_id) if sr.assigned_admin_id else None
        company = session.get(Company, sr.on_behalf_company_id) if sr.on_behalf_company_id else None
        history = RS.status_history(session, req_id)
        actor_map = {u.id: u for u in session.exec(select(User)).all()}
        deliverables = session.exec(select(RequestDeliverable).where(RequestDeliverable.request_id == req_id)
                                    .order_by(RequestDeliverable.id)).all()
        messages = session.exec(select(RequestMessage).where(RequestMessage.request_id == req_id)
                                .order_by(RequestMessage.id)).all()
        updates = session.exec(select(SellerUpdate).where(SellerUpdate.request_id == req_id)
                               .order_by(SellerUpdate.id)).all()
        witems = session.exec(select(WorkItem).where(WorkItem.related_request_id == req_id)
                              .order_by(WorkItem.id.desc())).all()
        funnel = pipeline.request_funnel(session, sr)
        hdr = {"section": "Request", "title": sr.tracking_code or f"Request {sr.id}",
               "desc": (f"{sr.product} → {sr.market}" if sr.market else sr.product),
               "breadcrumb": [{"label": "Requests", "href": "/admin/requests"}]}
        return templates.TemplateResponse("admin_request_detail.html", {
            "request": request, "user": user, "active": "requests", "sr": sr,
            "tab": tab if tab in ("summary", "pipeline", "comms", "work", "files", "related") else "summary",
            "requester": requester, "assignee": assignee, "company": company, "history": history,
            "actor_map": actor_map, "deliverables": deliverables, "messages": messages, "updates": updates,
            "witems": witems, "funnel": funnel, "now": datetime.utcnow(), "hdr": hdr, "staff": _staff(session),
            "wf_states": RS.WORKFLOW_STATES, "wf_label": RS.WORKFLOW_LABELS, "wf_badge": RS.WORKFLOW_BADGE,
            "eff_wf": RS.effective_workflow(sr), "PRIORITY_BADGE": WQ.PRIORITY_BADGE,
            "STATUS_BADGE": WQ.STATUS_BADGE, "TYPE_LABELS": WQ.TYPE_LABELS, "REQUEST_TYPES": REQUEST_TYPES})


@app.post("/admin/requests/{req_id}/assign")
def admin_request_assign(request: Request, req_id: int, assignee: str = Form(""), priority: str = Form(""),
                         due: str = Form(""), next_action: str = Form("")):
    """Set assignee / priority / due / next-action on a request (all additive; legacy status untouched)."""
    with Session(engine) as session:
        user = current_user(request, session)
        if not is_admin(user):
            return _forbidden()
        sr = session.get(ServiceRequest, req_id)
        if not sr:
            return _not_found()
        if assignee == "me":
            sr.assigned_admin_id = user.id
        elif assignee == "none":
            sr.assigned_admin_id = None
        elif assignee.isdigit():
            sr.assigned_admin_id = int(assignee)
        if priority in ("low", "normal", "high", "urgent"):
            sr.priority = priority
        if due:
            try:
                sr.due_at = datetime.fromisoformat(due)
            except ValueError:
                pass
        sr.next_action_note = (next_action or "").strip()[:500]
        RS.touch_activity(sr)
        session.add(sr)
        pipeline.audit(session, user, "request", sr.id, "request_assigned",
                       {"assignee": sr.assigned_admin_id, "priority": sr.priority}, tenant_id=sr.owner_id)
        session.commit()
        _flash(request, "Request updated ✓")
    return RedirectResponse(f"/admin/requests/{req_id}", status_code=303)


@app.post("/admin/requests/{req_id}/workflow")
def admin_request_workflow(request: Request, req_id: int, to: str = Form(""), reason: str = Form("")):
    """Advance the additive workflow_status (under_review / waiting_* / ready_for_delivery / completed /
    cancelled). Records history + audits; manages the deliver_result task. Legacy status is left alone."""
    with Session(engine) as session:
        user = current_user(request, session)
        if not is_admin(user):
            return _forbidden()
        sr = session.get(ServiceRequest, req_id)
        if not sr:
            return _not_found()
        ok, err = RS.advance_workflow(session, sr, to, user, reason)
        if ok:
            RS.reconcile_legacy(sr, to)   # keep the seller-visible legacy status consistent (never contradict)
            session.add(sr)
            if to == "ready_for_delivery":
                WQ.create_work_item_safe(session, actor=user, type="deliver_result",
                                         title=f"Deliver result for {sr.tracking_code or sr.id}",
                                         description="This request is ready for delivery.",
                                         tenant_id=sr.owner_id, related_request_id=sr.id,
                                         idempotency_key=f"deliver_result:req:{sr.id}")
            elif to in ("delivered", "completed", "rejected", "cancelled"):
                for k in (f"deliver_result:req:{sr.id}", f"overdue:req:{sr.id}",
                          f"review_new_request:req:{sr.id}"):
                    WQ.resolve_by_key(session, k, user, f"request {to}")
            session.commit()
            _flash(request, f"Status → {RS.WORKFLOW_LABELS.get(to, to)} ✓")
        else:
            _flash(request, err, "rose")
    return RedirectResponse(f"/admin/requests/{req_id}", status_code=303)


@app.post("/admin/requests/{req_id}/mark-read")
def admin_request_mark_read(request: Request, req_id: int):
    """Mark a request's chat as read by the admin (POST only — never mutate on the GET thread poll)."""
    with Session(engine) as session:
        user = current_user(request, session)
        if not is_admin(user):
            return _forbidden()
        sr = session.get(ServiceRequest, req_id)
        if not sr:
            return _not_found()
        sr.admin_last_read_at = datetime.utcnow()
        sr.action_required_admin = False
        session.add(sr)
        session.commit()
    return RedirectResponse(f"/admin/requests/{req_id}", status_code=303)


def _notify_requester(session, sr):
    try:
        notify_request_update(sr, session.get(User, sr.requester_id))
    except Exception:  # noqa: BLE001
        pass


@app.post("/admin/requests/{req_id}/approve")
def admin_request_approve(request: Request, req_id: int):
    with Session(engine) as session:
        user = current_user(request, session)
        if not is_admin(user):
            return _forbidden()
        sr = session.get(ServiceRequest, req_id)
        if sr and sr.status == "submitted":
            sr.status, sr.approved_by, sr.approved_at = "approved", user.email, datetime.utcnow()
            RS.advance_workflow(session, sr, "approved", user)           # keep additive workflow in sync
            WQ.resolve_by_key(session, f"review_new_request:req:{sr.id}", user, "request reviewed")
            session.add(sr); session.commit(); session.refresh(sr)
            _notify_requester(session, sr)
    return RedirectResponse("/admin/requests", status_code=303)


@app.post("/admin/requests/{req_id}/reject")
def admin_request_reject(request: Request, req_id: int, reason: str = Form("")):
    with Session(engine) as session:
        user = current_user(request, session)
        if not is_admin(user):
            return _forbidden()
        sr = session.get(ServiceRequest, req_id)
        if sr and sr.status in ("submitted", "approved"):
            sr.status, sr.admin_note, sr.done_at = "rejected", (reason or "").strip()[:500], datetime.utcnow()
            RS.advance_workflow(session, sr, "rejected", user, reason=(reason or "").strip())
            for k in (f"review_new_request:req:{sr.id}", f"deliver_result:req:{sr.id}", f"overdue:req:{sr.id}"):
                WQ.resolve_by_key(session, k, user, "request rejected")
            session.add(sr); session.commit(); session.refresh(sr)
            _notify_requester(session, sr)
    return RedirectResponse("/admin/requests", status_code=303)


@app.post("/admin/requests/{req_id}/start")
def admin_request_start(request: Request, req_id: int):
    with Session(engine) as session:
        user = current_user(request, session)
        if not is_admin(user):
            return _forbidden()
        sr = session.get(ServiceRequest, req_id)
        if sr and sr.status == "approved":
            sr.status, sr.started_at = "running", datetime.utcnow()
            RS.advance_workflow(session, sr, "in_progress", user)
            session.add(sr); session.commit()
    return RedirectResponse("/admin/requests", status_code=303)


@app.post("/admin/requests/{req_id}/done")
def admin_request_done(request: Request, req_id: int, result: str = Form(""),
                       url: str = Form(""), seller_safe: str = Form(""), file: UploadFile = File(None)):
    """Deliver a request, with a result note + optional file/link. Can be called REPEATEDLY (even after
    'done') — each delivery APPENDS a RequestDeliverable, so re-sends stack instead of overwriting. Tick
    seller_safe ONLY when the file carries no buyer PII (else sellers can't download it)."""
    with Session(engine) as session:
        user = current_user(request, session)
        if not is_admin(user):
            return _forbidden()
        sr = session.get(ServiceRequest, req_id)
        if sr and sr.status in ("approved", "running", "done"):
            note = (result or "").strip()[:1000]
            link = (url or "").strip()[:500]
            has_file = file is not None and (file.filename or "").strip()
            if has_file or link or note:
                dv = RequestDeliverable(request_id=sr.id, note=note, url=link, delivered_by=user.email,
                                        seller_safe=(seller_safe == "1"))
                session.add(dv); session.commit(); session.refresh(dv)
                if has_file:
                    rel = _save_deliverable_file(sr.id, dv.id, file)
                    if rel:
                        dv.file_path, sr.result_file_path = rel, rel
                if link:
                    sr.result_url = link
                session.add(dv)
            sr.status, sr.done_at = "done", datetime.utcnow()
            if note:
                sr.result = note
            RS.advance_workflow(session, sr, "delivered", user)
            for k in (f"deliver_result:req:{sr.id}", f"overdue:req:{sr.id}"):
                WQ.resolve_by_key(session, k, user, "request delivered")
            session.add(sr); session.commit(); session.refresh(sr)
            _notify_requester(session, sr)
    return RedirectResponse("/admin/requests", status_code=303)


# ---------------------------------------------------------------- admin: confidential pipeline + updates

def _managed_leads(session, req_id):
    return session.exec(select(Lead).where(Lead.request_id == req_id, Lead.managed == True)  # noqa: E712
                        .order_by(Lead.pipeline_stage, Lead.id)).all()


@app.get("/admin/requests/{req_id}/pipeline", response_class=HTMLResponse)
def admin_pipeline(request: Request, req_id: int):
    """Admin-only board of the managed (confidential) buyers for a request — FULL PII + stage controls."""
    with Session(engine) as session:
        user = current_user(request, session)
        if not is_admin(user):
            return _forbidden()
        sr = session.get(ServiceRequest, req_id)
        if not sr:
            return _not_found()
        leads = _managed_leads(session, req_id)
        pipeline.audit(session, user, "request", req_id, "pii_view", {"n": len(leads)},
                       tenant_id=sr.owner_id); session.commit()
        admins = session.exec(select(User).where(User.role == "admin")).all()
        funnel = pipeline.request_funnel(session, sr)
        updates = session.exec(select(SellerUpdate).where(SellerUpdate.request_id == req_id)
                               .order_by(SellerUpdate.id.desc())).all()
        umap = {u.id: u for u in session.exec(select(User)).all()}
    flashes = request.session.pop("_flash", [])
    return templates.TemplateResponse("admin_pipeline.html", {
        "request": request, "user": user, "active": "admin_requests", "sr": sr, "leads": leads,
        "stages": pipeline.PIPELINE_STAGES, "labels": pipeline.PUBLIC_STAGE_LABEL,
        "loss_reasons": pipeline.STANDARD_LOSS_REASONS, "size_bands": pipeline.SIZE_BANDS,
        "tmpls": pipeline.UPDATE_TEMPLATES, "admins": admins, "funnel": funnel,
        "updates": updates, "umap": umap, "flashes": flashes})


@app.post("/admin/requests/{req_id}/pipeline/{lead_id}/stage")
def admin_pipeline_stage(request: Request, req_id: int, lead_id: int,
                         to_stage: str = Form(...), note: str = Form("")):
    with Session(engine) as session:
        user = current_user(request, session)
        if not is_admin(user):
            return _forbidden()
        lead = session.get(Lead, lead_id)
        if not lead or not lead.managed or lead.request_id != req_id:      # never trust the posted ids
            return _not_found()
        ok, err = pipeline.set_pipeline_stage(session, lead, to_stage, user, note)
        if ok:
            session.commit(); session.refresh(lead)
            if to_stage == "won":
                _open_deal_if_won(session, lead, user); session.commit()
        else:
            _flash(request, err, "rose")
    return RedirectResponse(f"/admin/requests/{req_id}/pipeline", status_code=303)


@app.post("/admin/requests/{req_id}/pipeline/{lead_id}/fields")
def admin_pipeline_fields(request: Request, req_id: int, lead_id: int,
                          buyer_category: str = Form(""), company_size_band: str = Form(""),
                          fit_score: str = Form(""), assigned_admin_id: str = Form(""),
                          next_action_note: str = Form(""), next_action_at: str = Form(""),
                          seller_action_required: str = Form("")):
    with Session(engine) as session:
        user = current_user(request, session)
        if not is_admin(user):
            return _forbidden()
        lead = session.get(Lead, lead_id)
        if not lead or not lead.managed or lead.request_id != req_id:
            return _not_found()
        lead.buyer_category = buyer_category.strip()[:80]
        lead.company_size_band = company_size_band.strip()[:20]
        if fit_score:
            try:
                lead.fit_score = max(0.0, min(100.0, float(fit_score)))
            except ValueError:
                pass
        if assigned_admin_id.isdigit():
            lead.assigned_admin_id = int(assigned_admin_id)
        lead.next_action_note = next_action_note.strip()[:200]
        if next_action_at:
            try:
                lead.next_action_at = datetime.fromisoformat(next_action_at)
            except ValueError:
                pass
        lead.seller_action_required = (seller_action_required == "1")
        session.add(lead)
        pipeline.audit(session, user, "lead", lead.id, "fields_update", {}, tenant_id=lead.seller_id)
        session.commit()
    return RedirectResponse(f"/admin/requests/{req_id}/pipeline", status_code=303)


@app.post("/admin/requests/{req_id}/publish/preview", response_class=HTMLResponse)
def admin_publish_preview(request: Request, req_id: int, anon_ref: str = Form(""),
                          public_status: str = Form(""), summary: str = Form(""),
                          next_action: str = Form(""), seller_question: str = Form(""),
                          deadline: str = Form("")):
    """Show EXACTLY what the seller will see + any contact/PII the admin must remove before publishing."""
    with Session(engine) as session:
        user = current_user(request, session)
        if not is_admin(user):
            return _forbidden()
        sr = session.get(ServiceRequest, req_id)
        if not sr:
            return _not_found()
    hits = pipeline.sanitize_scan(" ".join([summary, next_action, seller_question, public_status]))
    u = {"anon_ref": anon_ref.strip(), "public_status": public_status.strip(), "summary": summary.strip(),
         "next_action": next_action.strip(), "seller_question": seller_question.strip(),
         "deadline": deadline.strip()}
    return templates.TemplateResponse("publish_update_preview.html", {
        "request": request, "user": user, "sr": sr, "u": u, "hits": hits})


@app.post("/admin/requests/{req_id}/publish")
def admin_publish(request: Request, req_id: int, anon_ref: str = Form(""), public_status: str = Form(""),
                  summary: str = Form(""), next_action: str = Form(""), seller_question: str = Form(""),
                  deadline: str = Form("")):
    """Publish a SANITIZED seller-visible update. Refuses if any contact/PII slips through."""
    with Session(engine) as session:
        user = current_user(request, session)
        if not is_admin(user):
            return _forbidden()
        sr = session.get(ServiceRequest, req_id)
        if not sr:
            return _not_found()
        if pipeline.sanitize_scan(" ".join([summary, next_action, seller_question, public_status])):
            _flash(request, "Blocked — the update still contains contact details / PII. Remove them and preview again.", "rose")
            return RedirectResponse(f"/admin/requests/{req_id}/pipeline", status_code=303)
        dl = None
        if deadline:
            try:
                dl = datetime.fromisoformat(deadline)
            except ValueError:
                dl = None
        q = seller_question.strip()[:300]
        su = SellerUpdate(request_id=req_id, seller_id=sr.owner_id, anon_ref=anon_ref.strip()[:40],
                          public_status=public_status.strip()[:80], summary=summary.strip()[:2000],
                          next_action=next_action.strip()[:300], seller_question=q,
                          status="open" if q else "resolved",   # only a question is an outstanding action
                          deadline=dl, published=True, published_by=user.email)
        session.add(su); session.commit(); session.refresh(su)
        pipeline.audit(session, user, "seller_update", su.id, "publish_update", {"req": req_id},
                       tenant_id=sr.owner_id)
        session.commit()
        if q:   # a published QUESTION is an outstanding requester action — track it as an INTERNAL task
                # linked to (never merged with) the sanitized SellerUpdate the seller actually sees.
            sr.action_required_requester = True
            RS.touch_activity(sr); session.add(sr)
            WQ.create_work_item_safe(session, actor=user, type="requester_action_required",
                                     title="Requester action required",
                                     description="A published question is awaiting the requester's reply.",
                                     tenant_id=sr.owner_id, related_request_id=req_id,
                                     related_seller_update_id=su.id, visibility="requester_visible",
                                     waiting_on="requester", idempotency_key=f"requester_action:su:{su.id}",
                                     condition_version="open")
            session.commit()
        try:
            notify_request_message(sr, type("_M", (), {"body": (su.summary or su.public_status)})(),
                                   session.get(User, sr.requester_id), from_name="Go4it admin")
        except Exception:  # noqa: BLE001
            pass
        _flash(request, "Update published to the seller ✓")
    return RedirectResponse(f"/admin/requests/{req_id}/pipeline", status_code=303)


@app.get("/requests/{req_id}/deliverable/{dv_id}")
def request_deliverable_file(request: Request, req_id: int, dv_id: int):
    """Download one delivered file from a request's deliverables history — owning trader or admin only."""
    with Session(engine) as session:
        user = current_user(request, session)
        sr = session.get(ServiceRequest, req_id)
        dv = session.get(RequestDeliverable, dv_id)
        if not sr or not dv or dv.request_id != sr.id or not owns(sr.owner_id, user) or not dv.file_path:
            return _not_found()
        if not is_admin(user) and not dv.seller_safe:   # sellers get ONLY files the admin marked PII-free
            return _not_found()
        path = (REQUEST_FILES_DIR / dv.file_path).resolve()
        if not str(path).startswith(str(REQUEST_FILES_DIR.resolve()) + os.sep) or not path.exists():
            return _not_found()
        fname = os.path.basename(dv.file_path)
    return FileResponse(str(path), filename=fname)


@app.post("/requests/{req_id}/messages")
def request_message(request: Request, req_id: int, body: str = Form("")):
    """Post a message to a request's chat. Works for BOTH the owning trader and the admin (owns() admits
    both), and pings the OTHER side on Telegram."""
    with Session(engine) as session:
        user = current_user(request, session)
        sr = session.get(ServiceRequest, req_id)
        if not sr or not role_at_least(user, "agent") or not owns(sr.owner_id, user):
            return _not_found()
        text = (body or "").strip()[:4000]
        if text and is_admin(user):        # admin -> seller: block buyer contact details/PII before it's saved
            hits = pipeline.sanitize_scan(text)
            if hits:
                kinds = ", ".join(sorted({h["kind"] for h in hits}))
                _flash(request, f"Message blocked - it contains buyer contact details / PII ({kinds}). "
                                "Remove them before sending.", "rose")
                return RedirectResponse(request.headers.get("referer") or "/requests", status_code=303)
        if text:
            m = RequestMessage(request_id=sr.id, sender_id=user.id, sender_role=user.role, body=text)
            session.add(m); session.commit(); session.refresh(m)
            # Phase 3: a REQUESTER message needs admin review (one open task per request, idempotent); an
            # ADMIN message clears the request's pending-review flag + closes that task. Non-blocking.
            if not is_admin(user):
                sr.action_required_admin = True; RS.touch_activity(sr); session.add(sr)
                WQ.create_work_item_safe(session, actor=user, type="review_reply",
                                         title=f"Review reply on {sr.tracking_code or sr.id}",
                                         description="The requester sent a new message — review and respond.",
                                         tenant_id=sr.owner_id, related_request_id=sr.id,
                                         idempotency_key=f"review_reply:req:{sr.id}")
            else:
                sr.action_required_admin = False; RS.touch_activity(sr); session.add(sr)
                WQ.resolve_by_key(session, f"review_reply:req:{sr.id}", user, "admin replied")
            session.commit()
            try:
                if is_admin(user):
                    notify_request_message(sr, m, session.get(User, sr.requester_id), from_name="Go4it admin")
                else:
                    admin = session.exec(select(User).where(User.role == "admin")).first()
                    notify_request_message(sr, m, admin, from_name=(user.name or user.email))
            except Exception:  # noqa: BLE001
                pass
    return RedirectResponse(request.headers.get("referer") or "/requests", status_code=303)


@app.post("/requests/{req_id}/updates/{update_id}/resolve")
def resolve_seller_update(request: Request, req_id: int, update_id: int, answer: str = Form("")):
    """Resolve an open seller-question so 'Action required' can never go stale. The owning seller (or admin)
    marks it answered; an optional answer is posted into the request chat so the admin sees it. Every id is
    re-validated from the DB — a posted update_id from another request/seller is rejected (404)."""
    with Session(engine) as session:
        user = current_user(request, session)
        sr = session.get(ServiceRequest, req_id)
        if not sr or not role_at_least(user, "agent") or not owns(sr.owner_id, user):
            return _not_found()
        su = session.get(SellerUpdate, update_id)
        if not su or su.request_id != req_id or su.seller_id != sr.owner_id:   # never trust the posted id
            return _not_found()
        answer_text = (answer or "").strip()[:4000]
        if answer_text and is_admin(user):   # if an admin authors the reply, scan it (admin -> seller)
            if pipeline.sanitize_scan(answer_text):
                _flash(request, "Answer blocked - it contains buyer contact details / PII. Remove them first.", "rose")
                return RedirectResponse(request.headers.get("referer") or "/requests", status_code=303)
        if su.status != "resolved":
            su.status = "resolved"
            su.resolved_at = datetime.utcnow()
            su.resolved_by = user.email
            session.add(su)
            if su.lead_id:            # clear the buyer's action flag so it can't linger
                lead = session.get(Lead, su.lead_id)
                if lead and lead.request_id == req_id:
                    lead.seller_action_required = False
                    session.add(lead)
            pipeline.audit(session, user, "seller_update", su.id, "resolve_update", {"req": req_id},
                           tenant_id=sr.owner_id)
            # Phase 3: the requester answered → close the linked requester-visible task; if the SELLER
            # answered (not the admin), queue an admin reply-review. Non-blocking, idempotent.
            WQ.resolve_by_key(session, f"requester_action:su:{su.id}", user, "requester responded")
            sr.action_required_requester = False
            RS.touch_activity(sr); session.add(sr)
            if not is_admin(user):
                sr.action_required_admin = True; session.add(sr)
                WQ.create_work_item_safe(session, actor=user, type="review_reply",
                                         title="Review requester reply",
                                         description="The requester answered a published question — review the reply.",
                                         tenant_id=sr.owner_id, related_request_id=req_id,
                                         idempotency_key=f"review_reply:su:{su.id}")
            text = answer_text
            if text:                  # the seller's answer goes into the mediated chat, not to the buyer
                m = RequestMessage(request_id=sr.id, sender_id=user.id, sender_role=user.role,
                                   body=f"[Answer to: {su.seller_question[:120]}] {text}")
                session.add(m); session.commit(); session.refresh(m)
                try:
                    if is_admin(user):
                        notify_request_message(sr, m, session.get(User, sr.requester_id), from_name="Go4it admin")
                    else:
                        admin = session.exec(select(User).where(User.role == "admin")).first()
                        notify_request_message(sr, m, admin, from_name=(user.name or user.email))
                except Exception:  # noqa: BLE001
                    pass
            else:
                session.commit()
        _flash(request, "Answer sent — Go4it will take it from here ✓")
    return RedirectResponse(request.headers.get("referer") or "/requests", status_code=303)


# ----------------------------------------------------------------------------- Work Queue (Phase 3)
# The central admin queue: every open task — reviews, follow-ups, replies, enrichment, failed jobs, bounces,
# quotes-to-approve, deliveries, overdue work. Admin-only. Internal tasks NEVER reach a seller; a
# requester-visible action is published only through the sanitized SellerUpdate path (linked, not merged).

def _staff(session):
    """Assignable staff (admins/managers) for the Work Queue assignee pickers."""
    return session.exec(select(User).where(User.role.in_(("admin", "manager"))).order_by(User.email)).all()


@app.get("/admin/work-queue/count", response_class=HTMLResponse)
def work_queue_count(request: Request):
    with Session(engine) as session:
        if not is_admin(current_user(request, session)):
            return _forbidden()
        n = WQ.nav_open_count(session)
    return HTMLResponse(str(n) if n else "")


@app.get("/admin/work-queue", response_class=HTMLResponse)
def work_queue(request: Request, view: str = "all_open", q: str = "", assignee: str = "",
               status: str = "", priority: str = "", type: str = "", party: str = "",
               source: str = "", overdue: str = "", page: int = 1):
    with Session(engine) as session:
        user = current_user(request, session)
        if not is_admin(user):
            return _forbidden()
        view = view if view in WQ.VIEWS else "all_open"
        stmt = WQ.apply_view(select(WorkItem), view, user)
        q = (q or "").strip()
        if q:
            like = f"%{q}%"
            stmt = stmt.where(or_(WorkItem.title.ilike(like), WorkItem.description.ilike(like)))
        if assignee == "me":
            stmt = stmt.where(WorkItem.assigned_admin_id == user.id)
        elif assignee == "none":
            stmt = stmt.where(WorkItem.assigned_admin_id.is_(None))
        elif assignee.isdigit():
            stmt = stmt.where(WorkItem.assigned_admin_id == int(assignee))
        if status in WQ.STATUSES:
            stmt = stmt.where(WorkItem.status == status)
        if priority in WQ.PRIORITIES:
            stmt = stmt.where(WorkItem.priority == priority)
        if type in WQ.TYPES:
            stmt = stmt.where(WorkItem.type == type)
        if party in WQ.WAITING_PARTIES:
            stmt = stmt.where(WorkItem.waiting_on == party)
        if source in ("manual", "automatic"):
            stmt = stmt.where(WorkItem.source == source)
        if overdue == "1":
            stmt = stmt.where(WorkItem.due_at.is_not(None), WorkItem.due_at < datetime.utcnow(),
                              WorkItem.status.in_(WQ.NONTERMINAL))
        per = 50
        total = session.exec(select(func.count()).select_from(stmt.subquery())).one()
        pages = max(1, (total + per - 1) // per)
        page = max(1, min(page, pages))
        items = session.exec(stmt.order_by(WorkItem.due_at.is_(None), WorkItem.due_at, WorkItem.id.desc())
                             .offset((page - 1) * per).limit(per)).all()
        req_ids = {i.related_request_id for i in items if i.related_request_id}
        req_map = ({r.id: r for r in session.exec(select(ServiceRequest)
                    .where(ServiceRequest.id.in_(req_ids))).all()} if req_ids else {})
        co_ids = {i.related_company_id for i in items if i.related_company_id}
        co_map = ({c.id: c for c in session.exec(select(Company).where(Company.id.in_(co_ids))).all()}
                  if co_ids else {})
        user_map = {u.id: u for u in session.exec(select(User)).all()}
        counts = WQ.queue_counts(session, user)
        f = {"view": view, "q": q, "assignee": assignee, "status": status, "priority": priority,
             "type": type, "party": party, "source": source, "overdue": overdue}
        hdr = {"section": "Work Queue", "title": "Work Queue",
               "desc": "Everything that needs admin attention, in one place.", "count": counts["actionable"]}
        return templates.TemplateResponse("work_queue.html", {
            "request": request, "user": user, "is_admin": True, "items": items, "counts": counts,
            "req_map": req_map, "co_map": co_map, "user_map": user_map, "staff": _staff(session),
            "f": f, "total": total, "page": page, "pages": pages, "hdr": hdr, "now": datetime.utcnow(),
            "VIEWS": WQ.VIEWS, "VIEW_LABELS": WQ.VIEW_LABELS, "TYPE_LABELS": WQ.TYPE_LABELS,
            "TYPES": WQ.TYPES, "STATUSES": WQ.STATUSES, "PRIORITIES": WQ.PRIORITIES,
            "WAITING_PARTIES": WQ.WAITING_PARTIES, "STATUS_BADGE": WQ.STATUS_BADGE,
            "PRIORITY_BADGE": WQ.PRIORITY_BADGE, "party_category": WQ.party_category})


@app.post("/admin/work-queue/create")
def work_item_create(request: Request, title: str = Form(""), type: str = Form("other"),
                     priority: str = Form("normal"), assignee: str = Form(""), due: str = Form(""),
                     description: str = Form(""), related_request_id: str = Form(""),
                     related_lead_id: str = Form(""), related_company_id: str = Form(""),
                     related_quote_id: str = Form("")):
    """Create a manual work item. The tenant is derived from the LINKED record (never user-supplied) so a
    work item can never cross a tenant boundary. Requires a related record unless it's a general 'other' task."""
    with Session(engine) as session:
        user = current_user(request, session)
        if not is_admin(user):
            return _forbidden()
        title = (title or "").strip()
        if not title:
            _flash(request, "A task needs a title.", "rose")
            return RedirectResponse("/admin/work-queue", status_code=303)
        rr = int(related_request_id) if related_request_id.isdigit() else None
        rl = int(related_lead_id) if related_lead_id.isdigit() else None
        rc = int(related_company_id) if related_company_id.isdigit() else None
        rq = int(related_quote_id) if related_quote_id.isdigit() else None
        wtype = type if type in WQ.TYPES else "other"
        if not any([rr, rl, rc, rq]) and wtype != "other":
            _flash(request, "Link the task to a request, company, lead or quote.", "rose")
            return RedirectResponse("/admin/work-queue", status_code=303)
        tenant, err = WQ.related_tenant(session, related_request_id=rr, related_lead_id=rl,
                                        related_quote_id=rq, related_company_id=rc)
        if err:
            _flash(request, f"Cannot create task — {err}.", "rose")
            return RedirectResponse("/admin/work-queue", status_code=303)
        due_at = None
        if due:
            try:
                due_at = datetime.fromisoformat(due)
            except ValueError:
                due_at = None
        assigned = int(assignee) if assignee.isdigit() else (user.id if assignee == "me" else None)
        try:
            wi = WQ.create_work_item(session, type=wtype, title=title, description=(description or "").strip(),
                                     tenant_id=tenant, priority=priority, assigned_admin_id=assigned,
                                     created_by=user.id, source="manual", related_request_id=rr,
                                     related_lead_id=rl, related_company_id=rc, related_quote_id=rq, due_at=due_at)
            pipeline.audit(session, user, "work_item", wi.id, "work_item_created",
                           {"type": wi.type, "manual": True}, tenant_id=tenant)
            session.commit()
            _flash(request, "Task created ✓")
        except Exception:  # noqa: BLE001
            session.rollback()
            _flash(request, "Could not create the task.", "rose")
    return RedirectResponse("/admin/work-queue", status_code=303)


@app.post("/admin/work-queue/{item_id}/action")
def work_item_action(request: Request, item_id: int, action: str = Form(""), assignee: str = Form(""),
                     priority: str = Form(""), party: str = Form(""), due: str = Form(""),
                     reason: str = Form("")):
    """Quick actions on one work item. Cross-tenant is a non-issue (admin-only); a missing id fails closed."""
    back = request.headers.get("referer") or "/admin/work-queue"
    with Session(engine) as session:
        user = current_user(request, session)
        if not is_admin(user):
            return _forbidden()
        wi = session.get(WorkItem, item_id)
        if not wi:
            return _not_found()
        if action == "assign_me":
            WQ.assign_item(session, wi, user.id, user)
        elif action == "reassign":
            WQ.assign_item(session, wi, int(assignee) if assignee.isdigit() else None, user)
        elif action == "start":
            WQ.start_item(session, wi, user)
        elif action == "complete":
            WQ.complete_item(session, wi, user, reason)
        elif action == "waiting":
            WQ.mark_waiting(session, wi, party, user)
        elif action == "priority":
            WQ.set_priority(session, wi, priority, user)
        elif action == "due":
            d = None
            if due:
                try:
                    d = datetime.fromisoformat(due)
                except ValueError:
                    d = None
            WQ.set_due(session, wi, d, user)
        elif action == "dismiss":
            if not (reason or "").strip():
                _flash(request, "Dismissing a task needs a reason.", "rose")
                return RedirectResponse(back, status_code=303)
            WQ.dismiss_item(session, wi, reason, user)
        session.commit()
    return RedirectResponse(back, status_code=303)


@app.post("/admin/work-queue/bulk")
def work_item_bulk(request: Request, action: str = Form(""), ids: list = Form([]), assignee: str = Form(""),
                   priority: str = Form(""), due: str = Form(""), reason: str = Form("")):
    """Bulk actions on selected work items (assign / priority / due / complete / dismiss). No destructive
    bulk delete — completed/dismissed items are retained for history."""
    with Session(engine) as session:
        user = current_user(request, session)
        if not is_admin(user):
            return _forbidden()
        if action == "dismiss" and not (reason or "").strip():
            _flash(request, "Dismissing tasks needs a reason.", "rose")
            return RedirectResponse("/admin/work-queue", status_code=303)
        d = None
        if due:
            try:
                d = datetime.fromisoformat(due)
            except ValueError:
                d = None
        n = 0
        for raw in ids:
            wid = int(raw) if str(raw).isdigit() else None
            wi = session.get(WorkItem, wid) if wid else None
            if not wi:
                continue
            if action == "assign":
                who = int(assignee) if assignee.isdigit() else (user.id if assignee == "me" else None)
                WQ.assign_item(session, wi, who, user)
            elif action == "priority":
                WQ.set_priority(session, wi, priority, user)
            elif action == "due":
                WQ.set_due(session, wi, d, user)
            elif action == "complete":
                WQ.complete_item(session, wi, user)
            elif action == "dismiss":
                WQ.dismiss_item(session, wi, reason, user)
            n += 1
        session.commit()
        _flash(request, f"Updated {n} task(s) ✓")
    return RedirectResponse("/admin/work-queue", status_code=303)


# ----------------------------------------------------------------------------- Outreach (Phase 4)
# Admin-only sales-communication workspace: Campaigns, Inbox, Follow-ups, Templates, Bounces & Suppression,
# Email Accounts, Analytics. Two-way confidentiality is enforced by SG/SUP on every send; sellers never
# reach any of these pages or any buyer contact/message. Recipients ALWAYS come from the Trade Network.

def _out_hdr(section, title, desc, count=None):
    return {"section": section, "title": title, "desc": desc, "count": count}


def _admin_mailbox(session, user, mailbox_id=None):
    """The admin's chosen (or default) Go4it-controlled sending mailbox, or None."""
    q = select(MailAccount).where(MailAccount.user_id == user.id, MailAccount.active == True)  # noqa: E712
    if mailbox_id:
        m = session.get(MailAccount, mailbox_id)
        return m if (m and m.user_id == user.id) else None
    accts = session.exec(q.order_by(MailAccount.is_default.desc(), MailAccount.id)).all()
    return accts[0] if accts else None


@app.get("/campaigns", response_class=HTMLResponse)
def campaigns_list(request: Request, status: str = ""):
    with Session(engine) as session:
        user = current_user(request, session)
        if not is_admin(user):
            return _forbidden()
        stmt = select(Campaign)
        if status in CAMP.CAMPAIGN_STATUSES:
            stmt = stmt.where(Campaign.status == status)
        camps = session.exec(stmt.order_by(Campaign.id.desc())).all()
        counts = {}
        for c in camps:
            counts[c.id] = session.exec(select(func.count()).where(
                CampaignRecipient.campaign_id == c.id)).one()
        umap = {u.id: u for u in session.exec(select(User)).all()}
        mailboxes = session.exec(select(MailAccount).where(MailAccount.admin_owned == True)).all()  # noqa: E712
        reqs = session.exec(select(ServiceRequest).order_by(ServiceRequest.id.desc())).all()
        hdr = _out_hdr("Outreach", "Campaigns", "Organized buyer outreach — recipients come from the Trade "
                       "Network, sent only from Go4it mailboxes.", len(camps))
        return templates.TemplateResponse("campaigns.html", {
            "request": request, "user": user, "active": "campaigns", "campaigns": camps, "counts": counts,
            "umap": umap, "hdr": hdr, "f": {"status": status}, "STATUSES": CAMP.CAMPAIGN_STATUSES,
            "mailboxes": mailboxes, "reqs": reqs, "paused_all": SG.outreach_paused(session)})


@app.post("/campaigns")
def campaign_new(request: Request, name: str = Form(""), context_kind: str = Form("request"),
                 request_id: str = Form(""), tenant_id: str = Form(""), mailbox_id: str = Form(""),
                 category: str = Form(""), target_countries: str = Form("")):
    with Session(engine) as session:
        user = current_user(request, session)
        if not is_admin(user):
            return _forbidden()
        name = (name or "").strip()[:120]
        if not name:
            _flash(request, "Give the campaign a name.", "rose")
            return RedirectResponse("/campaigns", status_code=303)
        rid = int(request_id) if request_id.isdigit() else None
        tid = int(tenant_id) if tenant_id.isdigit() else (
            session.get(ServiceRequest, rid).owner_id if rid and session.get(ServiceRequest, rid) else None)
        mb = session.get(MailAccount, int(mailbox_id)) if mailbox_id.isdigit() else None
        c = Campaign(name=name, context_kind=context_kind, request_id=rid, tenant_id=tid,
                     category=category.strip()[:80], target_countries=target_countries.strip()[:200],
                     owner_id=user.id, mailbox_id=(mb.id if mb else None), status="draft")
        session.add(c); session.commit(); session.refresh(c)
        pipeline.audit(session, user, "campaign", c.id, "create", {"name": name}, tenant_id=tid)
        session.commit()
    return RedirectResponse(f"/campaigns/{c.id}", status_code=303)


@app.get("/campaigns/{cid}", response_class=HTMLResponse)
def campaign_detail(request: Request, cid: int, product: str = "", country: str = "", engagement: str = ""):
    with Session(engine) as session:
        user = current_user(request, session)
        if not is_admin(user):
            return _forbidden()
        c = session.get(Campaign, cid)
        if not c:
            return _not_found()
        steps = CAMP.steps_for(session, c)
        rcpts = session.exec(select(CampaignRecipient).where(CampaignRecipient.campaign_id == cid)
                             .order_by(CampaignRecipient.id).limit(200)).all()
        from collections import Counter
        rc_status = Counter(r.status for r in rcpts)
        f = {"product": product, "country": country, "engagement": engagement}
        preview = CAMP.audience_preview(session, c, f) if (product or country or engagement) else None
        mailbox = session.get(MailAccount, c.mailbox_id) if c.mailbox_id else None
        mailboxes = session.exec(select(MailAccount).where(MailAccount.admin_owned == True)).all()  # noqa: E712
        hdr = _out_hdr("Campaign", c.name, f"{c.status} · {len(rcpts)} recipients · context {c.context_kind}")
        return templates.TemplateResponse("campaign_detail.html", {
            "request": request, "user": user, "active": "campaigns", "c": c, "steps": steps,
            "rcpts": rcpts, "rc_status": dict(rc_status), "preview": preview, "f": f, "hdr": hdr,
            "mailbox": mailbox, "mailboxes": mailboxes, "STATUSES": CAMP.CAMPAIGN_STATUSES,
            "paused_all": SG.outreach_paused(session)})


@app.post("/campaigns/{cid}/audience")
def campaign_audience(request: Request, cid: int, do: str = Form("preview"), product: str = Form(""),
                      country: str = Form(""), engagement: str = Form(""), exclude_customers: str = Form(""),
                      exclude_negative: str = Form(""), exclude_negotiating: str = Form(""),
                      exclude_recent: str = Form("")):
    with Session(engine) as session:
        user = current_user(request, session)
        if not is_admin(user):
            return _forbidden()
        c = session.get(Campaign, cid)
        if not c:
            return _not_found()
        f = {"product": product.strip(), "country": country.strip(), "engagement": engagement.strip(),
             "exclude_customers": exclude_customers == "1", "exclude_negative": exclude_negative == "1",
             "exclude_negotiating": exclude_negotiating == "1", "exclude_recent": exclude_recent == "1"}
        if do == "enroll":
            res = CAMP.enroll(session, c, user, f)     # requires the admin to have seen the preview
            _flash(request, f"Enrolled {res['created']} recipient(s); {res['skipped_suppressed']} suppressed, "
                            f"{res['skipped_existing']} already in.")
            return RedirectResponse(f"/campaigns/{cid}", status_code=303)
        qs = "&".join(f"{k}={v}" for k, v in [("product", product), ("country", country),
                                              ("engagement", engagement)] if v)
        return RedirectResponse(f"/campaigns/{cid}?{qs}", status_code=303)


@app.post("/campaigns/{cid}/sequence")
def campaign_sequence(request: Request, cid: int, subjects: List[str] = Form(default=[]),
                      bodies: List[str] = Form(default=[]), delays: List[str] = Form(default=[]),
                      confirm: str = Form("")):
    with Session(engine) as session:
        user = current_user(request, session)
        if not is_admin(user):
            return _forbidden()
        c = session.get(Campaign, cid)
        if not c:
            return _not_found()
        if c.status == "running" and confirm != "1":
            _flash(request, "Editing a running campaign creates a new sequence version — confirm to proceed.",
                   "rose")
            return RedirectResponse(f"/campaigns/{cid}", status_code=303)
        steps = []
        for i, subj in enumerate(subjects):
            if not (subj or "").strip() and not (bodies[i] if i < len(bodies) else "").strip():
                continue
            steps.append({"subject": subj, "body": bodies[i] if i < len(bodies) else "",
                          "delay_days": (delays[i] if i < len(delays) else "0")})
        if not steps:
            _flash(request, "Add at least an initial email.", "rose")
            return RedirectResponse(f"/campaigns/{cid}", status_code=303)
        v = CAMP.set_sequence(session, c, steps, user)
        _flash(request, f"Saved sequence v{v} ({len(steps)} step(s)).")
    return RedirectResponse(f"/campaigns/{cid}", status_code=303)


@app.post("/campaigns/{cid}/status")
def campaign_status(request: Request, cid: int, to: str = Form(""), reason: str = Form("")):
    with Session(engine) as session:
        user = current_user(request, session)
        if not is_admin(user):
            return _forbidden()
        c = session.get(Campaign, cid)
        if not c:
            return _not_found()
        if to == "running":
            if not c.mailbox_id or not (session.get(MailAccount, c.mailbox_id) or MailAccount()).admin_owned:
                _flash(request, "Assign a Go4it admin mailbox before running.", "rose")
                return RedirectResponse(f"/campaigns/{cid}", status_code=303)
            if not CAMP.steps_for(session, c):
                _flash(request, "Add a sequence before running.", "rose")
                return RedirectResponse(f"/campaigns/{cid}", status_code=303)
        ok, err = CAMP.transition(session, c, to, user, reason)
        session.commit()
        _flash(request, f"Campaign → {to}." if ok else err, "emerald" if ok else "rose")
    return RedirectResponse(f"/campaigns/{cid}", status_code=303)


@app.post("/outreach/pause-all")
def outreach_pause_all(request: Request, on: str = Form("1")):
    with Session(engine) as session:
        user = current_user(request, session)
        if not is_admin(user):
            return _forbidden()
        SG.set_pause_all(session, on == "1", user)
        session.commit()
        _flash(request, "All outreach paused." if on == "1" else "Outreach resumed.",
               "amber" if on == "1" else "emerald")
    return RedirectResponse(request.headers.get("referer") or "/campaigns", status_code=303)


@app.get("/inbox", response_class=HTMLResponse)
def outreach_inbox(request: Request, view: str = "all", q: str = ""):
    with Session(engine) as session:
        user = current_user(request, session)
        if not is_admin(user):
            return _forbidden()
        # threads = leads that have an inbound message; admin-only, full buyer visibility
        inbound_lead_ids = {o.lead_id for o in session.exec(
            select(Outreach).where(Outreach.direction == "in")).all()}
        leads = session.exec(select(Lead).where(Lead.id.in_(inbound_lead_ids or {0}))).all() if inbound_lead_ids else []
        if view == "positive":
            leads = [ld for ld in leads if ld.reply_outcome == "positive"]
        elif view == "negative":
            leads = [ld for ld in leads if ld.reply_outcome == "negative"]
        elif view == "auto":
            leads = [ld for ld in leads if ld.reply_outcome == "auto_reply"]
        elif view == "needs_reply":
            leads = [ld for ld in leads if ld.engagement_class == "engaged" and not ld.reply_outcome]
        if q:
            ql = q.lower()
            leads = [ld for ld in leads if ql in (ld.buyer_company or "").lower()
                     or ql in (ld.email or "").lower()]
        umap = {u.id: u for u in session.exec(select(User)).all()}
        last_in = {}
        for o in session.exec(select(Outreach).where(Outreach.direction == "in").order_by(Outreach.id)).all():
            last_in[o.lead_id] = o
        hdr = _out_hdr("Outreach", "Shared Inbox", "Every buyer conversation — admin-only.", len(leads))
        return templates.TemplateResponse("inbox.html", {
            "request": request, "user": user, "active": "inbox", "leads": leads, "umap": umap,
            "last_in": last_in, "hdr": hdr, "f": {"view": view, "q": q},
            "VIEWS": ["all", "needs_reply", "positive", "negative", "auto"]})


@app.get("/inbox/{lead_id}", response_class=HTMLResponse)
def inbox_thread(request: Request, lead_id: int):
    with Session(engine) as session:
        user = current_user(request, session)
        if not is_admin(user):
            return _forbidden()
        lead = session.get(Lead, lead_id)
        if not lead:
            return _not_found()
        msgs = session.exec(select(Outreach).where(Outreach.lead_id == lead_id,
                            Outreach.channel == "email").order_by(Outreach.id)).all()
        mailbox = _admin_mailbox(session, user)
        hdr = _out_hdr("Inbox", lead.buyer_company or f"Buyer #{lead.id}", lead.email or "")
        return templates.TemplateResponse("inbox_thread.html", {
            "request": request, "user": user, "active": "inbox", "lead": lead, "msgs": msgs,
            "mailbox": mailbox, "hdr": hdr, "OUTCOMES": ["positive", "negative", "follow_up_later",
            "wrong_contact", "unsubscribed", "auto_reply"]})


@app.post("/inbox/{lead_id}/reply")
def inbox_reply(request: Request, lead_id: int, subject: str = Form(""), body: str = Form(""),
                mailbox_id: str = Form("")):
    with Session(engine) as session:
        user = current_user(request, session)
        if not is_admin(user):
            return _forbidden()
        lead = session.get(Lead, lead_id)
        if not lead:
            return _not_found()
        to = (lead.email or "").strip()
        if not to:
            _flash(request, "No active buyer address — replace the contact first.", "rose")
            return RedirectResponse(f"/inbox/{lead_id}", status_code=303)
        if SG.outreach_paused(session):
            _flash(request, "All outreach is paused.", "rose")
            return RedirectResponse(f"/inbox/{lead_id}", status_code=303)
        if SUP.is_suppressed(session, to, tenant_id=lead.seller_id):
            _flash(request, "That address is suppressed — not sent.", "rose")
            return RedirectResponse(f"/inbox/{lead_id}", status_code=303)
        mb = _admin_mailbox(session, user, int(mailbox_id) if mailbox_id.isdigit() else None)
        ok_mb, why = SG.mailbox_ok(mb) if mb else (False, "no mailbox")
        if not ok_mb:
            _flash(request, f"Connect a Go4it mailbox first ({why}).", "rose")
            return RedirectResponse("/mail", status_code=303)
        subject = SG.sanitize_header((subject or "").strip()[:200])
        guarded = SG.guard_buyer_text(session, body, lead.seller_id)   # buyers never learn the seller
        text, html = plain_parts(guarded)
        last_out = session.exec(select(Outreach).where(Outreach.lead_id == lead_id,
                                Outreach.direction == "out", Outreach.message_id != "")
                                .order_by(Outreach.id.desc())).first()
        from .outreach import send_via_account
        okk, err, mid = send_via_account(mb, to, subject, text, html=html, reply_to=mb.email,
                                         in_reply_to=(last_out.message_id if last_out else ""))
        session.add(Outreach(lead_id=lead_id, direction="out", channel="email", recipient=to[:200],
                             from_addr=mb.email[:200], subject=subject[:200], body=guarded[:4000],
                             status="sent" if okk else "failed", error=(err or "")[:400], message_id=mid or "",
                             user_id=user.id))
        session.commit()
        _flash(request, "Reply sent." if okk else f"Send failed: {err}", "emerald" if okk else "rose")
    return RedirectResponse(f"/inbox/{lead_id}", status_code=303)


@app.post("/inbox/{lead_id}/outcome")
def inbox_outcome(request: Request, lead_id: int, outcome: str = Form("")):
    """Admin confirms the reply outcome (deterministic suggestions, admin decides). Positive → the buyer may
    become a qualified opportunity (via the pipeline). Negative stays a valuable Trade Network record."""
    with Session(engine) as session:
        user = current_user(request, session)
        if not is_admin(user):
            return _forbidden()
        lead = session.get(Lead, lead_id)
        if not lead:
            return _not_found()
        lead.reply_outcome = outcome if outcome in ("positive", "negative", "neutral", "auto_reply",
                                                    "follow_up_later") else lead.reply_outcome
        if outcome == "positive":
            lead.engagement_class = "engaged"
        elif outcome == "negative":
            lead.engagement_class = "engaged"      # a negative human reply is still an engaged buyer
        elif outcome == "auto_reply":
            pass
        session.add(lead)
        CAMP.stop_recipient(session, lead_id, {"positive": "positive_reply", "negative": "negative_reply",
                          "follow_up_later": "follow_up_later"}.get(outcome, "replied"), user)
        pipeline.audit(session, user, "lead", lead_id, "reply_outcome", {"outcome": outcome},
                       tenant_id=lead.seller_id)
        session.commit()
        _flash(request, "Outcome recorded.")
    return RedirectResponse(f"/inbox/{lead_id}", status_code=303)


@app.get("/followups", response_class=HTMLResponse)
def outreach_followups(request: Request, view: str = "due"):
    with Session(engine) as session:
        user = current_user(request, session)
        if not is_admin(user):
            return _forbidden()
        now = datetime.utcnow()
        rc = session.exec(select(CampaignRecipient).where(
            CampaignRecipient.status.not_in(CAMP.TERMINAL_RECIPIENT))).all()
        if view == "overdue":
            rows = [r for r in rc if r.next_action_at and r.next_action_at < now]
        elif view == "upcoming":
            rows = [r for r in rc if r.next_action_at and r.next_action_at >= now]
        else:
            rows = [r for r in rc if r.next_action_at is None or r.next_action_at <= now]
        lmap = {ld.id: ld for ld in session.exec(select(Lead)).all()}
        cmap = {c.id: c for c in session.exec(select(Campaign)).all()}
        hdr = _out_hdr("Outreach", "Follow-ups", "Automated sequence steps (campaigns) + manual actions "
                       "(Work Queue).", len(rows))
        return templates.TemplateResponse("followups_outreach.html", {
            "request": request, "user": user, "active": "followups", "rows": rows, "lmap": lmap,
            "cmap": cmap, "hdr": hdr, "f": {"view": view}, "now": now})


@app.get("/templates", response_class=HTMLResponse)
def templates_list(request: Request):
    with Session(engine) as session:
        user = current_user(request, session)
        if not is_admin(user):
            return _forbidden()
        tpls = session.exec(select(EmailTemplate).where(EmailTemplate.status == "active")
                            .order_by(EmailTemplate.id.desc())).all()
        hdr = _out_hdr("Outreach", "Templates", "Reusable outreach templates — approved variables only, no "
                       "seller PII or raw HTML.", len(tpls))
        return templates.TemplateResponse("email_templates.html", {
            "request": request, "user": user, "active": "templates", "tpls": tpls, "hdr": hdr,
            "ALLOWED_VARS": ["product", "category", "quantity", "origin", "destination", "incoterm",
                             "go4it_rep", "go4it_contact"]})


@app.post("/templates")
def template_save(request: Request, tid: str = Form(""), name: str = Form(""), subject: str = Form(""),
                  body: str = Form(""), product: str = Form(""), category: str = Form(""),
                  country: str = Form(""), language: str = Form("en")):
    with Session(engine) as session:
        user = current_user(request, session)
        if not is_admin(user):
            return _forbidden()
        name = (name or "").strip()[:120]
        # reject unsafe content: raw HTML tags, seller-PII markers, header chars
        if "<" in (body or "") or "<" in (subject or ""):
            _flash(request, "Templates can't contain raw HTML.", "rose")
            return RedirectResponse("/templates", status_code=303)
        subject = SG.sanitize_header(subject)
        existing = session.get(EmailTemplate, int(tid)) if tid.isdigit() else None
        if existing:                      # editing a USED template → new version
            existing.status = "archived"
            session.add(existing)
            t = EmailTemplate(name=name or existing.name, subject=subject, body=(body or "")[:8000],
                              product=product, category=category, country=country, language=language,
                              version=existing.version + 1, created_by=existing.created_by,
                              updated_by=user.id, tenant_id=existing.tenant_id)
        else:
            t = EmailTemplate(name=name, subject=subject, body=(body or "")[:8000], product=product,
                              category=category, country=country, language=language, created_by=user.id)
        session.add(t); session.commit()
        _flash(request, "Template saved.")
    return RedirectResponse("/templates", status_code=303)


@app.get("/suppression", response_class=HTMLResponse)
def suppression_page(request: Request, tab: str = "suppression"):
    with Session(engine) as session:
        user = current_user(request, session)
        if not is_admin(user):
            return _forbidden()
        sups = session.exec(select(Suppression).where(Suppression.active == True)   # noqa: E712
                            .order_by(Suppression.id.desc()).limit(500)).all()
        bounces = session.exec(select(BounceRecord).order_by(BounceRecord.last_bounce_at.desc())
                               .limit(500)).all()
        from collections import Counter
        by_reason = dict(Counter(s.reason for s in sups))
        hdr = _out_hdr("Outreach", "Bounces & Suppression", "Do-not-contact list + durable bounce history.",
                       len(sups))
        return templates.TemplateResponse("suppression.html", {
            "request": request, "user": user, "active": "suppression", "sups": sups, "bounces": bounces,
            "by_reason": by_reason, "hdr": hdr, "tab": tab, "REASONS": SUP.REASONS})


@app.post("/suppression")
def suppression_add(request: Request, email: str = Form(""), reason: str = Form("manual"),
                    note: str = Form("")):
    with Session(engine) as session:
        user = current_user(request, session)
        if not is_admin(user):
            return _forbidden()
        SUP.suppress(session, email, reason, user, scope="platform", note=note, source_event="manual")
        session.commit()
        _flash(request, f"Suppressed {SUP.normalize_email(email)}.")
    return RedirectResponse("/suppression", status_code=303)


@app.post("/suppression/{sid}/remove")
def suppression_remove(request: Request, sid: int):
    with Session(engine) as session:
        user = current_user(request, session)
        if not is_admin(user):
            return _forbidden()
        s = session.get(Suppression, sid)
        if s:
            SUP.unsuppress(session, s, user)
            session.commit()
            _flash(request, "Removed from suppression.")
    return RedirectResponse("/suppression", status_code=303)


@app.get("/outreach/analytics", response_class=HTMLResponse)
def outreach_analytics(request: Request):
    with Session(engine) as session:
        user = current_user(request, session)
        if not is_admin(user):
            return _forbidden()
        c = lambda stmt: session.exec(stmt).one()   # noqa: E731
        attempted = c(select(func.count()).where(Outreach.direction == "out", Outreach.channel == "email"))
        sent = c(select(func.count()).where(Outreach.direction == "out", Outreach.channel == "email",
                                            Outreach.status == "sent"))
        human_replies = c(select(func.count()).where(Lead.buyer_replied_at.is_not(None),
                          Lead.reply_outcome != "auto_reply"))
        positive = c(select(func.count()).where(Lead.reply_outcome == "positive"))
        negative = c(select(func.count()).where(Lead.reply_outcome == "negative"))
        qualified = c(select(func.count()).where(Lead.engagement_class == "qualified"))
        hard_b = c(select(func.count()).where(BounceRecord.bounce_type.in_(("hard", "domain_failure"))))
        soft_b = c(select(func.count()).where(BounceRecord.bounce_type.in_(("soft", "mailbox_full"))))
        unsub = c(select(func.count()).where(Suppression.reason == "unsubscribe", Suppression.active == True))  # noqa: E712
        deals = c(select(func.count()).select_from(Deal))

        def rate(a, b):
            return round(100 * a / b, 1) if b else 0.0
        m = {"attempted": attempted, "sent": sent, "delivered": "n/a", "human_replies": human_replies,
             "positive": positive, "negative": negative, "follow_up_later": 0, "hard_bounces": hard_b,
             "soft_bounces": soft_b, "unsubscribes": unsub, "qualified": qualified, "deals": deals,
             "human_response_rate": rate(human_replies, sent), "response_denominator": "sent",
             "positive_rate": rate(positive, human_replies), "qualification_rate": rate(qualified, human_replies),
             "hard_bounce_rate": rate(hard_b, sent)}
        hdr = _out_hdr("Outreach", "Analytics", "Outreach performance with explicit formulas.")
        return templates.TemplateResponse("outreach_analytics.html", {
            "request": request, "user": user, "active": "analytics", "m": m, "hdr": hdr})


# ----------------------------------------------------------------------------- admin: users + oversight
# Founder-only. Accounts are created HERE (no public sign-up); every trader is isolated to their own
# data, and the oversight page shows what each is doing, broken down per user.

ASSIGNABLE_ROLES = ("agent", "admin")   # this model has just two: trader (agent) + founder (admin)


@app.get("/admin/users", response_class=HTMLResponse)
def admin_users(request: Request, error: str = "", ok: str = ""):
    with Session(engine) as session:
        user = current_user(request, session)
        if not is_admin(user):
            return _forbidden()
        users = session.exec(select(User).order_by(User.id.asc())).all()
        lead_counts = dict(session.exec(
            select(Lead.owner_id, func.count(Lead.id)).group_by(Lead.owner_id)).all())
        deal_counts = dict(session.exec(
            select(Deal.owner_id, func.count(Deal.id)).group_by(Deal.owner_id)).all())
        rows = [{"u": u, "leads": lead_counts.get(u.id, 0), "deals": deal_counts.get(u.id, 0)}
                for u in users]
    return templates.TemplateResponse("admin_users.html", {
        "request": request, "user": user, "active": "users", "rows": rows,
        "roles": ASSIGNABLE_ROLES, "error": error, "ok": ok})


@app.post("/admin/users")
def admin_user_create(request: Request, email: str = Form(...), name: str = Form(""),
                      role: str = Form("agent"), password: str = Form(...)):
    with Session(engine) as session:
        if not is_admin(current_user(request, session)):
            return _forbidden()
        email = (email or "").strip().lower()
        role = role if role in ASSIGNABLE_ROLES else "agent"
        if not email or len(password) < 6:
            return RedirectResponse("/admin/users?error=input", status_code=303)
        if session.exec(select(User).where(User.email == email)).first():
            return RedirectResponse("/admin/users?error=exists", status_code=303)
        session.add(User(email=email, name=name.strip(), role=role,
                         password_hash=hash_password(password)))
        session.commit()
    return RedirectResponse("/admin/users?ok=created", status_code=303)


@app.post("/admin/users/{user_id}/password")
def admin_user_password(request: Request, user_id: int, password: str = Form(...)):
    with Session(engine) as session:
        if not is_admin(current_user(request, session)):
            return _forbidden()
        u = session.get(User, user_id)
        if not u or len(password) < 6:
            return RedirectResponse("/admin/users?error=input", status_code=303)
        u.password_hash = hash_password(password)
        session.add(u)
        session.commit()
    return RedirectResponse("/admin/users?ok=password", status_code=303)


@app.post("/admin/users/{user_id}/toggle")
def admin_user_toggle(request: Request, user_id: int):
    with Session(engine) as session:
        admin = current_user(request, session)
        if not is_admin(admin):
            return _forbidden()
        u = session.get(User, user_id)
        if u and u.id != admin.id:          # never lock yourself out
            u.active = not u.active
            session.add(u)
            session.commit()
    return RedirectResponse("/admin/users?ok=toggled", status_code=303)


@app.get("/admin/activity", response_class=HTMLResponse)
def admin_activity(request: Request):
    """Per-user oversight — what each trader is doing, SEGREGATED by user, so the founder can watch for
    mistakes or improper activity. Admin only. This is the admin's own 'tafkik shode' breakdown."""
    with Session(engine) as session:
        user = current_user(request, session)
        if not is_admin(user):
            return _forbidden()
        users = session.exec(select(User).order_by(User.id.asc())).all()

        def by_owner(model, owner_col, extra=None):
            stmt = select(owner_col, func.count(model.id))
            if extra is not None:
                stmt = stmt.where(extra)
            return dict(session.exec(stmt.group_by(owner_col)).all())

        leads_by = by_owner(Lead, Lead.owner_id)
        won_by = by_owner(Lead, Lead.owner_id, Lead.status == "won")
        quotes_by = by_owner(Quote, Quote.owner_id)
        deals_by = by_owner(Deal, Deal.owner_id)
        last_act = {}
        for a in session.exec(select(Activity).order_by(Activity.id.desc()).limit(1000)).all():
            if a.user_id and a.user_id not in last_act:
                last_act[a.user_id] = a
        rows = [{"u": u, "leads": leads_by.get(u.id, 0), "won": won_by.get(u.id, 0),
                 "quotes": quotes_by.get(u.id, 0), "deals": deals_by.get(u.id, 0),
                 "last": last_act.get(u.id)} for u in users]
        rows.sort(key=lambda r: -r["leads"])
        pool = leads_by.get(None, 0)        # unowned leads = the admin pool awaiting assignment
    return templates.TemplateResponse("admin_activity.html", {
        "request": request, "user": user, "active": "admin_activity", "rows": rows, "pool": pool})


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
