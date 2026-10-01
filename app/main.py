"""go4it web app — catalog + leads + matching + quotation + team CRM."""
import hmac
import html as html_lib
import json
import logging
import os
import secrets
from datetime import datetime, timedelta
from pathlib import Path
from typing import List, Optional
from urllib.parse import quote_plus

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
from .models import (CatalogGenerationJob, CostRate, ProductCategory, ProductCategoryAlias, ProductDocument,
                     ProductPriceVersion, ProductSupplier, ProductVariant)
from . import company_service as CS
from . import category_service as CATS
from . import pricing as PRICING
from . import product_import as PIMPORT
from . import catalog_studio as STUDIO
from . import attachments as ATT
from . import tradenet as TN
from . import work_queue as WQ
from . import request_service as RS
from . import suppression as SUP
from . import send_guard as SG
from . import campaign_service as CAMP
from . import campaign_render as CR
from .models import (BounceRecord, Campaign, CampaignRecipient, CampaignStep, EmailTemplate, OutreachControl,
                     Suppression)
from .outreach import (build_parts, default_message, honey_message, mail_decrypt, mail_encrypt,
                       plain_parts, quotation_data, send_bulk_via_account, send_email, send_via_account,
                       verify_smtp, zinc_message)
from .quote_service import create_quote, ensure_version, revise_quote
from . import quote_workflow as QWF
from . import quote_portal as QP
from . import quote_pdf as QPDF
from . import pdf_render as PDF
from . import ratelimit as RL
from .deal_service import ensure_deal_for_quote_version, deal_ready_for_ops
from . import contract_service as CONTRACT
from . import esign as ESIGN
from .models import (Contract, ContractDocument, ContractParty, ContractStatusEvent, ContractTemplate,
                     ContractVersion, QuoteDocument, QuoteStatusEvent, QuoteVersion, SignatureEvent)
from decimal import Decimal
from . import operations as OPS
from . import freight as FREIGHT
from . import shipments as SHIP
from . import customs as CUSTOMS
from . import tradedocs as TDOCS
from . import ops_exceptions as OPSX
from . import ops_providers as OPSPROV
from . import payments as PAY
from . import remittance as REMIT
from . import seller_progress as SP
from . import metrics as METRICS
from . import analytics as ANALYTICS
from . import charts as CHARTS
from . import data_sources as DATASRC
from . import provenance_view as PROV
from . import demand as INTEL_DEMAND
from . import opportunities as INTEL_OPP
from . import alerts as INTEL_ALERTS
from . import reports as INTEL_REPORTS
from . import ai_command as AICMD
from . import ai_provider as AIPROV
from . import ai_tools as AITOOLS
from .models import (AIActionProposal, AICitation, AIConversation, AIMessage, AutomationRule)
from .models import (AnalyticsReport, AnalyticsSnapshot, DemandSignal, IntelAlert, Opportunity,
                     OpportunityMatch, OpportunitySignal)
from .models import (CustomsCase, DocumentRequirement, FreightOffer, FreightRequest, OperationCase,
                     OperationalException, PaymentMilestone, RemittanceCase, Settlement, Shipment,
                     ShipmentEvent, ShipmentLeg, TradeDocument)
from .research_engine import (PARTNERS, country_options, market_report,
                              product_options, rank_opportunities, recommend_destinations,
                              resolve_query)
from .sources.go4world_csv import Go4WorldCsvSource
from .telegram import (notify_outreach_sent, notify_quote_ready, notify_request_message, notify_request_update,
                       notify_send_failed, notify_service_request, notify_status_change, send_message)
from .tenant import is_admin, owns, scoped
from . import authz
from . import access_service as ACCESS
from . import permissions as P
from .models import AccessAuditLog, PermissionOverride, RoleTemplate, UserProfile

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

PUBLIC_PREFIXES = ("/login", "/logout", "/static", "/api", "/go4it-capture.user.js", "/p/", "/q/", "/ops/")

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
    """Require a logged-in, still-valid session for everything except public paths. Phase 10: a disabled/archived
    account or a revoked session (critical security change) is logged out on its very next request, everywhere."""
    path = request.url.path
    if not any(path.startswith(p) for p in PUBLIC_PREFIXES):
        if not request.session.get("user_id"):
            return RedirectResponse("/login", status_code=303)
        with Session(engine) as _s:
            if current_user(request, _s) is None:      # disabled / archived / session revoked
                request.session.clear()
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
    from . import attachments as _att
    if _att.config_warning():
        logger.warning(_att.config_warning())      # prominent: flag set but attachments forcibly disabled


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
            from .models import UserProfile
            prof = session.exec(select(UserProfile).where(UserProfile.user_id == user.id)).first()
            if prof is not None and prof.account_status != "active":   # disabled/archived cannot sign in
                _login_fail(key)
                return RedirectResponse("/login?error=1", status_code=303)
            _LOGIN_FAILS.pop(key, None)
            # a fresh session per sign-in: nothing of the previous user's (a pending message naming a buyer, an import
            # preview) can carry into the next account opened in the same browser
            request.session.clear()
            request.session["user_id"] = user.id
            request.session["login_at"] = datetime.utcnow().timestamp()
            if prof is not None:
                prof.last_login_at = datetime.utcnow(); session.add(prof); session.commit()
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
        # Confidential delivery: the buyers Go4it works for a seller are admin-owned managed leads, so the owner-scoped
        # counts above stay 0 for them. Show the anonymized funnel totals instead (counts only — never a buyer row).
        seller_funnel, my_deliv = None, {}
        if not is_admin(user) and user is not None:
            all_reqs = session.exec(scoped(select(ServiceRequest), ServiceRequest.owner_id, user)).all()
            fs = [pipeline.request_funnel(session, r) for r in all_reqs]
            seller_funnel = {"total": sum(f["total_prospects"] for f in fs),
                             "contacted": sum(f["reached"]["contacted"] for f in fs),
                             "in_conversation": sum(f["reached"]["responded"] for f in fs),
                             "won": sum(f["reached"]["won"] for f in fs),
                             "countries": len({c for f in fs for c in f["countries"]})}
            my_deliv = {rid: [d for d in dvs if d.seller_safe]          # sellers only ever see PII-free files
                        for rid, dvs in _deliverables_for(session, [r.id for r in my_requests]).items()}

        ctx = {
            "request": request, "user": user, "active": "dashboard", "is_admin": is_admin(user),
            "my_requests": my_requests, "request_types": REQUEST_TYPES,
            "seller_funnel": seller_funnel, "deliv_map": my_deliv,
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
        # Phase 8 — the admin intelligence layer on the dashboard (metric-registry KPIs + the commercial funnel).
        # Computed live, read-only; the trader branch is untouched. GET never mutates business data.
        if is_admin(user):
            since = datetime.utcnow() - timedelta(days=30)
            ctx["intel_kpis"] = ANALYTICS.dashboard_kpis(session)
            ctx["funnel"] = ANALYTICS.funnel(session)
            ctx["funnel_rows"] = CHARTS.funnel_rows(ctx["funnel"]["stages"])
            ctx["replies_outcome"] = ANALYTICS.replies_by_outcome(session, since=since)
            ctx["deals_stage_bars"] = CHARTS.bar_rows(ANALYTICS.deals_by_stage(session))
            ctx["wq_priority_bars"] = CHARTS.bar_rows(ANALYTICS.work_queue_by(session, "priority"))
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
    """Buyer outreach send — requires outreach.email.send (separate from drafting/viewing). Confidential:
    only internal staff with the send permission may contact buyers; sellers never reach this."""
    with Session(engine) as session:
        user = current_user(request, session)
        if not is_admin(user) or not authz.has_permission(session, user, "outreach.email.send"):
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
        prepared, suppressed, managed, seen = [], 0, 0, set()
        for ld in leads:
            em = (ld.email or "").strip()
            if not em or em.lower() in seen:     # one email per address per batch
                continue
            if ld.managed:                       # confidential managed buyers are emailed through Campaigns
                managed += 1                     # (footer, unsubscribe header, daily limit, pipeline sync)
                continue
            if SUP.is_suppressed(session, em, tenant_id=ld.seller_id):
                suppressed += 1
                continue
            if len(prepared) >= 60:            # cap the synchronous batch
                break
            guarded = SG.guard_buyer_text(session, body, ld.seller_id)   # buyers never learn the seller
            text, html = plain_parts(guarded)
            prepared.append((ld, em, guarded, text, html))
            seen.add(em.lower())
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
        skipped = len(leads) - len(prepared) - suppressed - managed
        note = f"Sent {sent} email(s) from {acct.email}."
        if fail:
            note += f" {fail} failed."
        if suppressed:
            note += f" {suppressed} skipped (on the do-not-contact list)."
        if managed:
            note += f" {managed} managed buyer(s) skipped — email them through Campaigns."
        if skipped:
            note += f" {skipped} skipped (no email address, a duplicate, or over the 60-per-send cap)."
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
        can_manage = _outreach_admin(session, user)
        missing = {a.id: CR.validate_sender(a) for a in accounts}
    flashes = request.session.pop("_flash", [])
    return templates.TemplateResponse("mail_accounts.html", {
        "request": request, "user": user, "active": "mail", "accounts": accounts, "flashes": flashes,
        "can_manage": can_manage, "missing": missing, "today": datetime.utcnow().strftime("%Y-%m-%d")})


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
        in_use = session.exec(select(func.count()).where(
            Campaign.mailbox_id == acct.id, Campaign.status.not_in(("archived", "cancelled", "completed")))).one()
        if in_use:
            _flash(request, f"{acct.email} is used by {in_use} campaign(s) — pause it here or re-enter its "
                            "App Password instead of removing it.", "rose")
            return RedirectResponse("/mail", status_code=303)
        session.delete(acct); session.commit()
    return RedirectResponse("/mail", status_code=303)


def _outreach_admin(session, user) -> bool:
    """Founder / outreach-manager controls: internal staff holding outreach.campaign.manage."""
    return is_admin(user) and authz.has_permission(session, user, "outreach.campaign.manage")


@app.post("/mail/{acct_id}/controls")
def mail_controls(request: Request, acct_id: int, admin_owned: str = Form(""), paused: str = Form(""),
                  daily_limit: str = Form(""), from_name: str = Form(""), sender_company: str = Form(""),
                  postal_address: str = Form("")):
    """Go4it-owned flag, pause, daily limit and the buyer-facing footer (sender company + postal address)."""
    with Session(engine) as session:
        user = current_user(request, session)
        if not _outreach_admin(session, user):
            return _forbidden()
        acct = session.get(MailAccount, acct_id)
        if not acct or acct.user_id != user.id:
            return _not_found()
        before = {"admin_owned": acct.admin_owned, "paused": acct.paused, "daily_limit": acct.daily_limit}
        acct.admin_owned = admin_owned == "1"
        was_paused, acct.paused = acct.paused, paused == "1"
        try:
            acct.daily_limit = max(0, min(2000, int(daily_limit)))
        except (TypeError, ValueError):
            pass
        acct.from_name = SG.sanitize_header(from_name)[:120]
        acct.sender_company = SG.sanitize_header(sender_company)[:160]
        acct.postal_address = "\n".join(SG.sanitize_header(ln) for ln in (postal_address or "").splitlines()
                                        if ln.strip())[:400]
        if was_paused and not acct.paused:                  # resumed → the failure that paused it is handled
            acct.last_send_error = ""
            for key in (f"mailbox_auth_failure:{acct.id}", f"mailbox_paused:{acct.id}"):
                WQ.resolve_by_key(session, key, user, note="mailbox resumed")
        acct.updated_at = datetime.utcnow()
        session.add(acct)
        pipeline.audit(session, user, "mailbox", acct.id, "controls",
                       {"before": before, "after": {"admin_owned": acct.admin_owned, "paused": acct.paused,
                                                    "daily_limit": acct.daily_limit}})
        session.commit()
        problems = CR.validate_sender(acct)
        _flash(request, f"Saved {acct.email}." + (f" Still needed before sending: {'; '.join(problems)}."
                                                  if problems else ""), "amber" if problems else "emerald")
    return RedirectResponse("/mail", status_code=303)


@app.post("/mail/{acct_id}/credentials")
def mail_credentials(request: Request, acct_id: int, app_password: str = Form("")):
    """Re-enter the App Password (e.g. after an auth failure) — verified before saving; resumes the mailbox."""
    with Session(engine) as session:
        user = current_user(request, session)
        if not _outreach_admin(session, user):
            return _forbidden()
        acct = session.get(MailAccount, acct_id)
        if not acct or acct.user_id != user.id:
            return _not_found()
        pw = (app_password or "").strip()
        ok, err = verify_smtp(acct.smtp_host, acct.smtp_port, acct.email, pw) if pw else (False, "enter it first")
        if not ok:
            _flash(request, f"Couldn't connect {acct.email}: {err}", "rose")
            return RedirectResponse("/mail", status_code=303)
        acct.smtp_password_enc = mail_encrypt(pw)
        acct.last_verified_at = datetime.utcnow()
        resumed = acct.paused and (acct.last_send_error or "").startswith(("auth", "config"))
        if resumed:                   # only a credential failure is fixed by new credentials; a deliberate or
            acct.paused, acct.last_send_error = False, ""          # quota pause stays until lifted on purpose
            WQ.resolve_by_key(session, f"mailbox_auth_failure:{acct.id}", user, note="credentials re-entered")
        session.add(acct)
        pipeline.audit(session, user, "mailbox", acct.id, "credentials_updated", {"resumed": resumed})
        session.commit()
        _flash(request, f"{acct.email} reconnected ✓" + (" and resumed." if resumed else
                                                         (" It is still paused — resume it in its controls."
                                                          if acct.paused else "")))
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
        # product counts: legacy Product.supplier_id + canonical ProductSupplier links (by company)
        pcounts = {sid: c for sid, c in session.exec(
            select(Product.supplier_id, func.count(Product.id)).group_by(Product.supplier_id)).all()}
        ps_by_company = {}
        for cid, c in session.exec(select(ProductSupplier.company_id, func.count(ProductSupplier.id))
                                   .group_by(ProductSupplier.company_id)).all():
            ps_by_company[cid] = c
        companies = {c.id: c for c in session.exec(select(Company)).all()}
        # supplied categories per supplier (via its products)
        rows = []
        for s in suppliers:
            n_products = pcounts.get(s.id, 0) + (ps_by_company.get(s.company_id, 0) if s.company_id else 0)
            co = companies.get(s.company_id) if s.company_id else None
            missing = []
            if not (s.contact or s.email or s.phone):
                missing.append("contact")
            if not s.company_id:
                missing.append("company link")
            if n_products == 0:
                missing.append("products")
            rows.append({"s": s, "n_products": n_products, "company": co, "missing": missing,
                         "verification": (co.verification_status if co else "—"),
                         "reliability": (s.reliability if s.reliability_rated else None)})
        countries = sorted({s.country for s in session.exec(select(Supplier)).all() if s.country})
        product_counts = {r["s"].id: r["n_products"] for r in rows}
        verifications = {r["s"].id: r["verification"] for r in rows}
        missing_map = {r["s"].id: r["missing"] for r in rows}
        ctx = {"request": request, "user": user, "active": "suppliers",
               "suppliers": suppliers, "rows": rows, "pcounts": pcounts, "product_counts": product_counts,
               "verifications": verifications, "missing_map": missing_map, "can_edit": True,
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
        sup.reliability_rated = True   # an admin explicitly reviewed + set the rating
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
        sup.reliability_rated = True   # an admin explicitly reviewed + set the rating
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
    """Canonical company view — requires buyer.pii.view (contact PII lives here). Never shows anon refs as an id."""
    with Session(engine) as session:
        user = current_user(request, session)
        if not is_admin(user) or not authz.has_permission(session, user, "buyer.pii.view"):
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
    """Audited, CSV-injection-safe export of a Trade Network slice. Requires buyer.pii.export (a separate
    high-risk capability from merely viewing a workspace). NEVER seller-reachable."""
    with Session(engine) as session:
        user = current_user(request, session)
        if not is_admin(user) or not authz.has_permission(session, user, "buyer.pii.export"):
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
def command_page(request: Request, c: int = 0):
    """The AI Command copilot: an evidence-based admin assistant over Go4it data. Admin-only. The legacy buyer-
    harvest box remains available (a collapsible affordance) and its routes are unchanged."""
    with Session(engine) as session:
        user = current_user(request, session)
        if not is_admin(user):          # the copilot is the founder's moat — admin only
            return _forbidden()
        convos = session.exec(select(AIConversation).where(
            AIConversation.owner_id == user.id, AIConversation.status == "active")
            .order_by(AIConversation.updated_at.desc()).limit(30)).all()
        active = session.get(AIConversation, c) if c else (convos[0] if convos else None)
        if active and active.owner_id != user.id:        # cross-admin ownership guard
            return _not_found()
        messages, proposals = [], []
        if active:
            msgs = session.exec(select(AIMessage).where(AIMessage.conversation_id == active.id)
                                .order_by(AIMessage.id.asc())).all()
            for m in msgs:
                cites = session.exec(select(AICitation).where(AICitation.message_id == m.id)).all()
                messages.append({"m": m, "text": AICMD.message_text(m), "citations": cites})
            proposals = session.exec(select(AIActionProposal).where(
                AIActionProposal.conversation_id == active.id,
                AIActionProposal.status == "proposed").order_by(AIActionProposal.id.desc())).all()
        jobs = session.exec(select(CommandJob).order_by(CommandJob.id.desc()).limit(10)).all()
    ctx = {"request": request, "user": user, "active": "command", "convos": convos, "conv": active,
           "messages": messages, "proposals": proposals, "suggested": AICMD.SUGGESTED,
           "provider": AIPROV.provider_status(), "jobs": jobs}
    return templates.TemplateResponse("command.html", ctx)


@app.post("/command/ask", response_class=HTMLResponse)
def command_ask(request: Request, prompt: str = Form(""), conversation_id: str = Form("")):
    """Ask the copilot. Creates/continues a conversation, runs the deterministic (or provider) answer, and
    returns the rendered turn. Never mutates business data."""
    with Session(engine) as session:
        user = current_user(request, session)
        if not is_admin(user):
            return HTMLResponse("Forbidden", status_code=403)
        prompt = (prompt or "").strip()
        if not prompt:
            return HTMLResponse("", status_code=204)
        conv = session.get(AIConversation, int(conversation_id)) if conversation_id.strip() else None
        if conv and conv.owner_id != user.id:
            return _not_found()
        if not conv:
            conv = AICMD.new_conversation(session, user)
        res = AICMD.answer(session, conv, prompt, user)
        session.commit()
        cid = conv.id
    # re-render the full conversation area (HTMX swaps it in)
    return RedirectResponse(f"/command?c={cid}", status_code=303)


@app.post("/command/conversations/{conv_id}/rename")
def command_rename(request: Request, conv_id: int, title: str = Form("")):
    with Session(engine) as session:
        user = current_user(request, session)
        if not is_admin(user):
            return _forbidden()
        conv = session.get(AIConversation, conv_id)
        if not conv or conv.owner_id != user.id:
            return _not_found()
        conv.title = (title or conv.title).strip()[:80]
        conv.updated_at = datetime.utcnow()
        session.add(conv); session.commit()
    return RedirectResponse(f"/command?c={conv_id}", status_code=303)


@app.post("/command/conversations/{conv_id}/archive")
def command_archive(request: Request, conv_id: int):
    with Session(engine) as session:
        user = current_user(request, session)
        if not is_admin(user):
            return _forbidden()
        conv = session.get(AIConversation, conv_id)
        if not conv or conv.owner_id != user.id:
            return _not_found()
        conv.status = "archived"
        conv.archived_at = datetime.utcnow()
        session.add(conv); session.commit()
    return RedirectResponse("/command", status_code=303)


@app.post("/command/pause-all")
def command_pause_all(request: Request, paused: str = Form("1")):
    """Emergency Pause-All for AI provider work (deterministic answers still available)."""
    with Session(engine) as session:
        if not is_admin(current_user(request, session)):
            return _forbidden()
    AIPROV.pause_all(paused == "1")
    from . import automation as _AUTO
    _AUTO.pause_all(paused == "1")
    return RedirectResponse("/command", status_code=303)


@app.post("/command/proposals/{prop_id}/approve")
def command_approve_proposal(request: Request, prop_id: int, background: BackgroundTasks, nonce: str = Form("")):
    """Approve an AI action proposal. Safe execution: nonce + revalidation + payload-hash + idempotent execute
    via an existing domain service. Double-click is a no-op."""
    from . import ai_actions as AIACT
    with Session(engine) as session:
        user = current_user(request, session)
        if not is_admin(user):
            return _forbidden()
        p = session.get(AIActionProposal, prop_id)
        if not p or (p.conversation_id and session.get(AIConversation, p.conversation_id).owner_id != user.id):
            return _not_found()
        cid = p.conversation_id
        ok, result = AIACT.approve(session, p, nonce=nonce, actor=user)
        session.commit()
        # a start_research approval created a queued CommandJob via the existing pipeline; run it in the
        # background (the existing harvest path) — never before approval.
        job_id = result.get("command_job_id") if ok and isinstance(result, dict) else None
    if job_id:
        background.add_task(run_command_job, job_id)
    return RedirectResponse(f"/command?c={cid}", status_code=303)


@app.post("/command/proposals/{prop_id}/decline")
def command_decline_proposal(request: Request, prop_id: int):
    from . import ai_actions as AIACT
    with Session(engine) as session:
        user = current_user(request, session)
        if not is_admin(user):
            return _forbidden()
        p = session.get(AIActionProposal, prop_id)
        if not p:
            return _not_found()
        cid = p.conversation_id
        AIACT.decline(session, p, actor=user)
        session.commit()
    return RedirectResponse(f"/command?c={cid}", status_code=303)


@app.get("/command/brief", response_class=HTMLResponse)
def command_brief(request: Request, period: str = "daily"):
    from . import ai_brief
    with Session(engine) as session:
        user, deny = _ops_admin(request, session)
        if deny:
            return deny
        brief = ai_brief.generate_brief(session, period="weekly" if period == "weekly" else "daily")
    return templates.TemplateResponse("command_brief.html", {"request": request, "user": user, "brief": brief})


@app.get("/command/automation", response_class=HTMLResponse)
def command_automation(request: Request):
    from . import automation as AUTO
    with Session(engine) as session:
        user, deny = _ops_admin(request, session)
        if deny:
            return deny
        rules = session.exec(select(AutomationRule).order_by(AutomationRule.id.desc()).limit(50)).all()
        paused = AUTO.is_paused()
    return templates.TemplateResponse("command_automation.html", {
        "request": request, "user": user, "rules": rules, "triggers": AUTO.TRIGGERS, "actions": AUTO.ACTIONS,
        "paused": paused})


@app.post("/command/automation")
def command_automation_create(request: Request, name: str = Form(""), trigger_type: str = Form(""),
                              action_type: str = Form(""), min_score: str = Form("")):
    from . import automation as AUTO
    with Session(engine) as session:
        user, deny = _ops_admin(request, session)
        if deny:
            return deny
        conditions = {"min_score": int(min_score)} if min_score.strip().isdigit() else {}
        rule, err = AUTO.create_rule(session, name=name or "rule", trigger_type=trigger_type,
                                     action_type=action_type, conditions=conditions, actor=user)
        session.commit()
        if err:
            return HTMLResponse(f"Cannot create rule: {err}", 400)
    return RedirectResponse("/command/automation", status_code=303)


@app.post("/command/automation/{rule_id}/toggle")
def command_automation_toggle(request: Request, rule_id: int, enabled: str = Form("")):
    from . import automation as AUTO
    with Session(engine) as session:
        user, deny = _ops_admin(request, session)
        if deny:
            return deny
        rule = session.get(AutomationRule, rule_id)
        if not rule:
            return _not_found()
        AUTO.set_enabled(session, rule, enabled == "1", actor=user)
        session.commit()
    return RedirectResponse("/command/automation", status_code=303)


@app.post("/command/automation/{rule_id}/dry-run")
def command_automation_dryrun(request: Request, rule_id: int):
    from . import automation as AUTO
    with Session(engine) as session:
        user, deny = _ops_admin(request, session)
        if deny:
            return deny
        rule = session.get(AutomationRule, rule_id)
        if not rule:
            return _not_found()
        run, _did = AUTO.run_rule(session, rule, dry_run=True, actor=user)
        session.commit()
        preview = run.output_summary if run else "condition not currently met"
    return HTMLResponse(f"<div class='card'>Dry-run: {preview}</div>")


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
        can_send = SMTP_ENABLED and is_admin(user) and authz.has_permission(session, user, "outreach.email.send")
    return templates.TemplateResponse(
        "lead_detail.html",
        {"request": request, "user": user, "is_admin": is_admin(user), "lead": lead, "owner": owner,
         "users": users, "products": products, "quotable": quotable,
         "matches": [{"m": m, "product": products.get(m.product_id)} for m in matches],
         "quotes": quotes, "timeline": timeline, "outreach": outreach, "thread": thread,
         "default_subject": default_subject, "default_body": default_body,
         "latest_share_url": latest_share_url,
         "smtp_enabled": can_send, "today": datetime.utcnow().date(),
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

PRODUCT_SORTS = {"new": Product.id.desc(), "name": Product.name.asc(),
                 "updated": Product.updated_at.desc(), "price": Product.exw_price.desc()}
_COMPLETENESS_FIELDS = ("name", "sku", "category_id", "hs_code", "unit", "exw_price", "weight_kg_per_unit",
                        "origin_country", "spec", "min_order_qty")


def _product_completeness(p) -> int:
    """0-100 cached completeness from the presence of the key catalog fields (origin_country OR origin_region
    counts). Cheap + deterministic; the Work Queue uses the same notion of 'incomplete'."""
    present = 0
    for f in _COMPLETENESS_FIELDS:
        v = getattr(p, f, None)
        if f == "origin_country":
            v = v or getattr(p, "origin_region", "")
        if v not in (None, "", 0, 0.0):
            present += 1
    return round(present * 100 / len(_COMPLETENESS_FIELDS))


@app.get("/catalog", response_class=HTMLResponse)
def catalog(request: Request, q: str = "", category: str = "", origin: str = "", supplier: str = "",
            status: str = "", verification: str = "", completeness: str = "", price: str = "", hs: str = "",
            listed: str = "yes", sort: str = "new", page: int = 1,
            imported: int = 0, updated: int = 0, errors: int = 0):
    """The admin catalog workspace — server-side search / filter / sort / pagination (never loads the whole
    catalog into the browser). Admin only (exposes EXW buy-cost + margin)."""
    per = 50
    with Session(engine) as session:
        user = current_user(request, session)
        if not is_admin(user):
            return _forbidden()
        stmt = select(Product)
        if listed == "archived":
            stmt = stmt.where(Product.active == False)          # noqa: E712
        elif listed != "all":
            stmt = stmt.where(Product.active == True)           # noqa: E712
        if q:
            like = f"%{q.strip()}%"
            stmt = stmt.where(Product.name.ilike(like) | Product.sku.ilike(like)
                              | Product.hs_code.ilike(like) | Product.brand.ilike(like))
        if category == "none":
            stmt = stmt.where(Product.category_id == None)      # noqa: E711
        elif category.isdigit():
            stmt = stmt.where(Product.category_id == int(category))
        if origin:
            stmt = stmt.where((Product.origin_country == origin) | (Product.origin_region == origin))
        if supplier.isdigit():
            pids = [ps.product_id for ps in session.exec(
                select(ProductSupplier).where(ProductSupplier.company_id == int(supplier))).all()]
            stmt = stmt.where(Product.id.in_(pids or [-1]))
        if status:
            stmt = stmt.where(Product.status == status)
        if verification:
            stmt = stmt.where(Product.verification_status == verification)
        if hs == "no":
            stmt = stmt.where((Product.hs_code == None) | (Product.hs_code == ""))   # noqa: E711
        elif hs == "yes":
            stmt = stmt.where((Product.hs_code != None) & (Product.hs_code != ""))    # noqa: E711
        if price == "yes":
            stmt = stmt.where(Product.exw_price > 0)
        elif price == "no":
            stmt = stmt.where((Product.exw_price == None) | (Product.exw_price == 0))  # noqa: E711
        total = session.exec(select(func.count()).select_from(stmt.subquery())).one()
        pages = max(1, (total + per - 1) // per)
        page = min(max(1, page), pages)
        order = PRODUCT_SORTS.get(sort, PRODUCT_SORTS["new"])
        products = session.exec(stmt.order_by(order).offset((page - 1) * per).limit(per)).all()
        # completeness filter is post-hoc (cheap on a page of 50)
        rows = [{"p": p, "completeness": _product_completeness(p)} for p in products]
        if completeness == "low":
            rows = [r for r in rows if r["completeness"] < 60]
        elif completeness == "high":
            rows = [r for r in rows if r["completeness"] >= 60]
        pids = [p.id for p in products]
        supcounts = {}
        if pids:
            for ps in session.exec(select(ProductSupplier).where(ProductSupplier.product_id.in_(pids))).all():
                supcounts[ps.product_id] = supcounts.get(ps.product_id, 0) + 1
        for p in products:
            if not supcounts.get(p.id) and p.supplier_id:
                supcounts[p.id] = 1
        cats = {c.id: c for c in session.exec(select(ProductCategory).where(
            ProductCategory.status == "active")).all()}
        companies = {c.id: c for c in session.exec(select(Company).where(
            Company.primary_role == "supplier")).all()}
        origins = sorted({(p.origin_country or p.origin_region) for p in session.exec(
            select(Product)).all() if (p.origin_country or p.origin_region)})
    return templates.TemplateResponse("catalog.html", {
        "request": request, "user": user, "rows": rows, "supcounts": supcounts, "cats": cats,
        "companies": companies, "origins": origins, "total": total, "page": page, "pages": pages,
        "q": q, "f": {"category": category, "origin": origin, "supplier": supplier, "status": status,
                      "verification": verification, "completeness": completeness, "price": price, "hs": hs,
                      "listed": listed, "sort": sort},
        "imported": imported, "updated": updated, "errors": errors})


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


@app.post("/catalog/import/preview", response_class=HTMLResponse)
def import_catalog_preview(request: Request, file: UploadFile = File(...)):
    """Parse + classify each row (new/update/ambiguous/skip) WITHOUT writing — conservative identity, never
    name-only. The admin reviews the preview before applying."""
    with Session(engine) as session:
        user = current_user(request, session)
        if not is_admin(user):
            return _forbidden()
        text = file.file.read().decode("utf-8-sig", errors="replace")
        rows, errors, mapping = PIMPORT.parse(text)
        prev = PIMPORT.preview(session, rows)
        request.session["_import_csv"] = text        # stash for the apply step
    return templates.TemplateResponse("catalog_import.html", {
        "request": request, "user": user, "active": "catalog", "preview": prev, "errors": errors,
        "mapping": mapping, "row_count": len(rows)})


@app.post("/catalog/import")
def import_catalog(request: Request, file: UploadFile = File(None)):
    """Apply the import (idempotent, transaction-safe). Uses the stashed preview CSV, or a freshly uploaded
    file. Ambiguous rows become review tasks (never auto-merged)."""
    with Session(engine) as session:
        user = current_user(request, session)
        if not is_admin(user):
            return _forbidden()
        text = ""
        if file is not None:
            text = file.file.read().decode("utf-8-sig", errors="replace")
        else:
            text = request.session.pop("_import_csv", "")
        if not text:
            return RedirectResponse("/catalog?errors=1", status_code=303)
        rows, _errors, _map = PIMPORT.parse(text)
        counts, report = PIMPORT.apply(session, rows, actor=user)
        session.commit()
        pipeline.audit(session, user, "product", None, "catalog_import", counts)
        session.commit()
        request.session["_import_report"] = PIMPORT.error_report_csv(report)
    return RedirectResponse(
        f"/catalog?imported={counts['created']}&updated={counts['updated']}&errors={counts['errors']}",
        status_code=303)


@app.get("/catalog/import/report.csv", response_class=PlainTextResponse)
def import_report_csv(request: Request):
    with Session(engine) as session:
        if not is_admin(current_user(request, session)):
            return _forbidden()
    rep = request.session.get("_import_report", "row,result,detail\n")
    return PlainTextResponse(rep, headers={"Content-Disposition": "attachment; filename=import_report.csv"})


@app.get("/catalog/export.csv", response_class=PlainTextResponse)
def catalog_export(request: Request):
    """Admin-only, audited CSV export of the catalog."""
    import csv as _csv
    import io as _io
    with Session(engine) as session:
        user = current_user(request, session)
        if not is_admin(user):
            return _forbidden()
        products = session.exec(select(Product).order_by(Product.id)).all()
        out = _io.StringIO()
        w = _csv.writer(out)
        w.writerow(["id", "sku", "name", "category", "hs_code", "origin_country", "unit", "exw_price",
                    "currency", "min_order_qty", "verification_status", "status"])
        for p in products:
            w.writerow([p.id, p.sku, p.name, p.category, p.hs_code, p.origin_country or p.origin_region,
                        p.unit, p.exw_price, p.currency, p.min_order_qty, p.verification_status, p.status])
        pipeline.audit(session, user, "export", None, "catalog_export", {"rows": len(products)})
        session.commit()
    return PlainTextResponse(out.getvalue(),
                             headers={"Content-Disposition": "attachment; filename=go4it_catalog.csv"})


@app.get("/catalog/template.csv", response_class=PlainTextResponse)
def catalog_template_csv():
    return PlainTextResponse(PIMPORT.template_csv(),
                             headers={"Content-Disposition": "attachment; filename=go4it_products_template.csv"})


@app.get("/catalog/sample.csv", response_class=PlainTextResponse)
def sample_csv():
    return PlainTextResponse(
        SAMPLE_CSV,
        headers={"Content-Disposition": "attachment; filename=go4it_products_sample.csv"})


# ----------------------------------------------------------------------------- product detail (Phase 5)
_PRODUCT_EDIT_FIELDS = ("name", "sku", "short_description", "spec", "category", "subcategory", "brand", "grade",
                        "hs_code", "unit", "currency", "exw_price", "weight_kg_per_unit", "cbm_per_unit",
                        "packaging", "min_order_qty", "units_per_package", "origin_country", "origin_city",
                        "origin_region", "producer", "incoterms", "certifications", "lead_time_days",
                        "production_capacity", "shelf_life", "storage_requirements", "internal_notes")


@app.get("/catalog/products/{product_id}", response_class=HTMLResponse)
def product_detail(request: Request, product_id: int, tab: str = "overview"):
    """The tabbed admin product workspace (Overview/Specifications/Suppliers/Pricing/Documents/Catalogs/
    Activity). Admin only. No business mutation on this GET."""
    with Session(engine) as session:
        user = current_user(request, session)
        if not is_admin(user):
            return _forbidden()
        p = session.get(Product, product_id)
        if not p:
            return _not_found()
        cat = session.get(ProductCategory, p.category_id) if p.category_id else None
        sup_links = session.exec(select(ProductSupplier).where(
            ProductSupplier.product_id == p.id)).all()
        companies = {c.id: c for c in session.exec(select(Company)).all()}
        supplier_companies = session.exec(select(Company).where(Company.primary_role == "supplier")).all()
        prices = session.exec(select(ProductPriceVersion).where(
            ProductPriceVersion.product_id == p.id).order_by(ProductPriceVersion.version.desc())).all()
        docs = session.exec(select(ProductDocument).where(
            ProductDocument.product_id == p.id, ProductDocument.status == "active")).all()
        catalogs = session.exec(select(CatalogGenerationJob).where(
            CatalogGenerationJob.product_id == p.id).order_by(CatalogGenerationJob.version.desc())).all()
        acts = session.exec(select(AuditLog).where(
            AuditLog.entity_type.in_(("product", "price_version")),
            AuditLog.entity_id == p.id).order_by(AuditLog.id.desc()).limit(50)).all()
        cats = session.exec(select(ProductCategory).where(ProductCategory.status == "active")).all()
        completeness = _product_completeness(p)
        missing = WQ._missing_fields(session, p)
        # parse price breakdowns for display
        import json as _json
        price_rows = []
        for pv in prices:
            try:
                bd = _json.loads(pv.breakdown or "[]")
                ex = _json.loads(pv.excluded_costs or "[]")
            except Exception:  # noqa: BLE001
                bd, ex = [], []
            price_rows.append({"pv": pv, "breakdown": bd, "excluded": ex})
    return templates.TemplateResponse("product_detail.html", {
        "request": request, "user": user, "active": "catalog", "p": p, "cat": cat, "tab": tab,
        "sup_links": sup_links, "companies": companies, "supplier_companies": supplier_companies,
        "price_rows": price_rows, "docs": docs, "catalogs": catalogs, "acts": acts, "cats": cats,
        "completeness": completeness, "missing": missing, "incoterms": PRICING.INCOTERMS})


@app.post("/catalog/products/{product_id}/edit")
async def product_edit(request: Request, product_id: int):
    """Explicit Save. Every changed field is audited (previous → new). No-op fields are ignored."""
    form = await request.form()
    with Session(engine) as session:
        user = current_user(request, session)
        if not is_admin(user):
            return _forbidden()
        p = session.get(Product, product_id)
        if not p:
            return _not_found()
        changes = {}
        for f in _PRODUCT_EDIT_FIELDS:
            if f not in form:
                continue
            raw = form.get(f, "")
            new = _coerce_field(p, f, raw)
            old = getattr(p, f, None)
            if new != old:
                changes[f] = {"from": _safe(old), "to": _safe(new)}
                setattr(p, f, new)
        if changes:
            if "category" in changes and (p.category or "").strip():
                cat = CATS.get_or_create_category(session, p.category)
                if cat:
                    p.category_id = cat.id
            p.status = "active" if p.active else "archived"
            p.completeness_score = _product_completeness(p)
            p.updated_at = datetime.utcnow()
            p.updated_by = user.email
            session.add(p)
            pipeline.audit(session, user, "product", p.id, "product_edit", {"fields": list(changes.keys())})
            session.commit()
    return RedirectResponse(f"/catalog/products/{product_id}", status_code=303)


@app.post("/catalog/products/{product_id}/verify")
def product_verify(request: Request, product_id: int, verification_status: str = Form("verified")):
    with Session(engine) as session:
        user = current_user(request, session)
        if not is_admin(user):
            return _forbidden()
        p = session.get(Product, product_id)
        if not p:
            return _not_found()
        if verification_status not in ("unverified", "verified", "rejected"):
            verification_status = "unverified"
        prev, p.verification_status = p.verification_status, verification_status
        p.verified_at = datetime.utcnow() if verification_status == "verified" else None
        p.verified_by = user.email if verification_status == "verified" else ""
        session.add(p)
        pipeline.audit(session, user, "product", p.id, "product_verify",
                       {"from": prev, "to": verification_status})
        session.commit()
    return RedirectResponse(f"/catalog/products/{product_id}", status_code=303)


@app.post("/catalog/products/{product_id}/status")
def product_status(request: Request, product_id: int, status: str = Form("active")):
    with Session(engine) as session:
        user = current_user(request, session)
        if not is_admin(user):
            return _forbidden()
        p = session.get(Product, product_id)
        if not p:
            return _not_found()
        if status not in ("draft", "active", "archived"):
            status = "active"
        prev = p.status
        p.status = status
        p.active = (status != "archived")
        session.add(p)
        pipeline.audit(session, user, "product", p.id, "product_status", {"from": prev, "to": status})
        session.commit()
    return RedirectResponse(f"/catalog/products/{product_id}", status_code=303)


@app.post("/catalog/products/{product_id}/suppliers")
def product_supplier_add(request: Request, product_id: int, company_id: int = Form(...),
                         supplier_sku: str = Form(""), supplier_price: float = Form(0),
                         currency: str = Form("USD"), is_primary: str = Form("")):
    """Link a product to a canonical Trade Network supplier Company (idempotent on (product, company))."""
    with Session(engine) as session:
        user = current_user(request, session)
        if not is_admin(user):
            return _forbidden()
        p = session.get(Product, product_id)
        co = session.get(Company, company_id)
        if not p or not co:
            return _not_found()
        exists = session.exec(select(ProductSupplier).where(
            ProductSupplier.product_id == product_id, ProductSupplier.company_id == company_id)).first()
        if not exists:
            session.add(ProductSupplier(product_id=product_id, company_id=company_id,
                                        supplier_sku=supplier_sku, supplier_price=supplier_price,
                                        currency=currency, is_primary=(is_primary == "1")))
            CS._add_role(session, co, "supplier")
            pipeline.audit(session, user, "product", product_id, "product_supplier_link",
                           {"company_id": company_id})
            session.commit()
    return RedirectResponse(f"/catalog/products/{product_id}?tab=suppliers", status_code=303)


@app.post("/catalog/products/{product_id}/price")
def product_price_new(request: Request, product_id: int, incoterm: str = Form("EXW"),
                      quantity: float = Form(0), destination: str = Form(""),
                      transport_mode: str = Form(""), margin_pct: float = Form(0),
                      target_currency: str = Form("")):
    """Create an IMMUTABLE price version from the calculator. Never edits an existing version."""
    with Session(engine) as session:
        user = current_user(request, session)
        if not is_admin(user):
            return _forbidden()
        p = session.get(Product, product_id)
        if not p:
            return _not_found()
        PRICING.create_price_version(session, p, incoterm=incoterm,
                                     quantity=(quantity or None), destination=destination,
                                     transport_mode=transport_mode, margin_pct=margin_pct,
                                     target_currency=(target_currency or None), actor=user)
        session.commit()
    return RedirectResponse(f"/catalog/products/{product_id}?tab=pricing", status_code=303)


@app.post("/catalog/products/{product_id}/price/{pv_id}/action")
def product_price_action(request: Request, product_id: int, pv_id: int, action: str = Form(...)):
    """Approve / duplicate-to-revise / mark needs_review / expire / archive a price version. History is never
    rewritten — 'duplicate' clones to a fresh draft."""
    with Session(engine) as session:
        user = current_user(request, session)
        if not is_admin(user):
            return _forbidden()
        pv = session.get(ProductPriceVersion, pv_id)
        if not pv or pv.product_id != product_id:
            return _not_found()
        if action == "duplicate":
            PRICING.duplicate_version(session, pv, actor=user)
        elif action in ("approved", "needs_review", "expired", "archived", "draft"):
            PRICING.transition_version(session, pv, action, actor=user)
        session.commit()
    return RedirectResponse(f"/catalog/products/{product_id}?tab=pricing", status_code=303)


@app.post("/catalog/bulk")
async def catalog_bulk(request: Request):
    """Bulk category-assign or status-change over selected products. Admin only; audited."""
    form = await request.form()
    with Session(engine) as session:
        user = current_user(request, session)
        if not is_admin(user):
            return _forbidden()
        ids = [int(x) for x in form.getlist("product_ids") if str(x).isdigit()]
        action = form.get("action", "")
        if action == "category" and form.get("category_id", "").isdigit():
            cid = int(form["category_id"])
            n = CATS.move_products(session, ids, cid, actor=user)
            pipeline.audit(session, user, "product", None, "bulk_category", {"n": n, "category_id": cid})
        elif action in ("archive", "activate"):
            n = 0
            for pid in ids:
                p = session.get(Product, pid)
                if p:
                    p.active = (action == "activate")
                    p.status = "active" if p.active else "archived"
                    session.add(p); n += 1
            pipeline.audit(session, user, "product", None, "bulk_status", {"n": n, "action": action})
        session.commit()
    return RedirectResponse("/catalog", status_code=303)


# ----------------------------------------------------------------------------- categories (Phase 5)
@app.get("/categories", response_class=HTMLResponse)
def categories_page(request: Request):
    with Session(engine) as session:
        user = current_user(request, session)
        if not is_admin(user):
            return _forbidden()
        cats = session.exec(select(ProductCategory).order_by(ProductCategory.name)).all()
        counts = {c.id: CATS.product_count(session, c.id) for c in cats}
        uncategorized = session.exec(select(func.count()).select_from(Product).where(
            Product.category_id == None)).one()                 # noqa: E711
        aliases = {}
        for a in session.exec(select(ProductCategoryAlias)).all():
            aliases.setdefault(a.category_id, []).append(a.alias_normalized)
    return templates.TemplateResponse("categories.html", {
        "request": request, "user": user, "active": "categories", "cats": cats, "counts": counts,
        "uncategorized": uncategorized, "aliases": aliases})


@app.post("/categories")
def category_create(request: Request, name: str = Form(...), parent_id: str = Form("")):
    with Session(engine) as session:
        user = current_user(request, session)
        if not is_admin(user):
            return _forbidden()
        pid = int(parent_id) if parent_id.isdigit() else None
        CATS.get_or_create_category(session, name, parent_id=pid)
        session.commit()
    return RedirectResponse("/categories", status_code=303)


@app.post("/categories/merge")
def category_merge(request: Request, source_id: int = Form(...), dest_id: int = Form(...)):
    with Session(engine) as session:
        user = current_user(request, session)
        if not is_admin(user):
            return _forbidden()
        CATS.merge_categories(session, source_id, dest_id, actor=user)
        session.commit()
    return RedirectResponse("/categories", status_code=303)


@app.post("/categories/{category_id}/status")
def category_status(request: Request, category_id: int, status: str = Form("active")):
    with Session(engine) as session:
        user = current_user(request, session)
        if not is_admin(user):
            return _forbidden()
        CATS.set_status(session, category_id, status, actor=user)
        session.commit()
    return RedirectResponse("/categories", status_code=303)


@app.post("/categories/{category_id}/alias")
def category_alias_add(request: Request, category_id: int, alias: str = Form(...)):
    with Session(engine) as session:
        user = current_user(request, session)
        if not is_admin(user):
            return _forbidden()
        CATS.add_alias(session, alias, category_id)
        session.commit()
    return RedirectResponse("/categories", status_code=303)


# ----------------------------------------------------------------------------- pricing & rates (Phase 5)
@app.get("/pricing", response_class=HTMLResponse)
def pricing_page(request: Request):
    """Admin-only structured cost rates + FX (with honest staleness) + a standalone landed-price calculator.
    Separate page from Catalog (Catalog and Pricing are never combined into one crowded screen)."""
    with Session(engine) as session:
        user = current_user(request, session)
        if not is_admin(user):
            return _forbidden()
        rates = session.exec(select(CostRate).order_by(CostRate.rate_type, CostRate.id.desc())).all()
        fxs = session.exec(select(FxRate).order_by(FxRate.id.desc())).all()
        now = datetime.utcnow()
        rate_rows = [{"r": r, "active": PRICING.rate_active({
            "status": r.status, "valid_from": r.valid_from, "valid_until": r.valid_until}, now)} for r in rates]
        fx_rows = [{"fx": f, "state": PRICING.fx_state({
            "rate": f.rate, "kind": f.kind, "expires_at": f.expires_at}, now),
            "label": PRICING.fx_label(PRICING.fx_state({
                "rate": f.rate, "kind": f.kind, "expires_at": f.expires_at}, now))} for f in fxs]
    return templates.TemplateResponse("pricing.html", {
        "request": request, "user": user, "active": "pricing", "rate_rows": rate_rows, "fx_rows": fx_rows,
        "rate_types": PRICING._LABELS, "incoterms": PRICING.INCOTERMS})


@app.post("/pricing/rates")
def cost_rate_create(request: Request, name: str = Form(""), rate_type: str = Form(...),
                     origin: str = Form(""), destination: str = Form(""), transport_mode: str = Form(""),
                     currency: str = Form("USD"), unit_basis: str = Form("per_shipment"),
                     amount: float = Form(0), min_charge: float = Form(0), valid_until: str = Form(""),
                     source: str = Form(""), confidence: str = Form("unverified")):
    with Session(engine) as session:
        user = current_user(request, session)
        if not is_admin(user):
            return _forbidden()
        vu = None
        if valid_until:
            try:
                vu = datetime.fromisoformat(valid_until)
            except ValueError:
                vu = None
        session.add(CostRate(name=name, rate_type=rate_type, origin=origin, destination=destination,
                             transport_mode=transport_mode, currency=currency, unit_basis=unit_basis,
                             amount=amount, min_charge=min_charge, valid_until=vu, source=source,
                             confidence=confidence, created_by=user.email))
        pipeline.audit(session, user, "cost_rate", None, "cost_rate_create", {"type": rate_type})
        session.commit()
    return RedirectResponse("/pricing", status_code=303)


@app.post("/pricing/fx")
def fx_upsert(request: Request, base: str = Form(...), quote: str = Form("USD"), rate: float = Form(...),
              kind: str = Form("manual"), source: str = Form(""), expires_days: int = Form(0)):
    with Session(engine) as session:
        user = current_user(request, session)
        if not is_admin(user):
            return _forbidden()
        if rate <= 0:
            return RedirectResponse("/pricing", status_code=303)
        base, quote = base.upper().strip(), quote.upper().strip()
        row = session.exec(select(FxRate).where(FxRate.base == base, FxRate.quote == quote)).first()
        now = datetime.utcnow()
        exp = now + timedelta(days=expires_days) if expires_days > 0 else None
        if row:
            row.rate, row.kind, row.source = rate, kind, source
            row.retrieved_at, row.expires_at, row.verified_by, row.active = now, exp, user.email, True
        else:
            row = FxRate(base=base, quote=quote, rate=rate, kind=kind, source=source, retrieved_at=now,
                         expires_at=exp, verified_by=user.email, active=True)
        session.add(row)
        pipeline.audit(session, user, "fx_rate", None, "fx_upsert", {"base": base, "quote": quote})
        session.commit()
    return RedirectResponse("/pricing", status_code=303)


@app.post("/pricing/calc", response_class=HTMLResponse)
def pricing_calc(request: Request, product_id: int = Form(...), incoterm: str = Form("EXW"),
                 quantity: float = Form(0), margin_pct: float = Form(0), destination: str = Form(""),
                 target_currency: str = Form("")):
    """Standalone calculator preview (does NOT persist). To save, use the product Pricing tab."""
    with Session(engine) as session:
        user = current_user(request, session)
        if not is_admin(user):
            return _forbidden()
        p = session.get(Product, product_id)
        if not p:
            return _not_found()
        fx = PRICING._fx_for(session, p.currency or "USD", (target_currency or p.currency or "USD"))
        rates = PRICING._rates_for(session, p, None)
        calc = PRICING.compute_price(base_price=p.exw_price, base_currency=p.currency or "USD",
                                     quantity=(quantity or p.min_order_qty or 1),
                                     weight_kg_per_unit=p.weight_kg_per_unit, incoterm=incoterm, rates=rates,
                                     fx=fx, margin_pct=margin_pct, target_currency=(target_currency or None))
    return templates.TemplateResponse("pricing_calc_result.html", {
        "request": request, "user": user, "p": p, "calc": calc,
        "fx_label": PRICING.fx_label(calc["fx_state"])})


def _coerce_field(p, field, raw):
    """Coerce a form string to the product field's type (float/int/str)."""
    cur = getattr(p, field, None)
    if isinstance(cur, bool):
        return raw in ("1", "true", "on", "yes")
    if isinstance(cur, int) and not isinstance(cur, bool):
        try:
            return int(float(raw)) if raw != "" else 0
        except ValueError:
            return cur
    if isinstance(cur, float):
        try:
            return float(raw) if raw != "" else 0.0
        except ValueError:
            return cur
    return (raw or "").strip()


def _safe(v):
    return v.isoformat() if isinstance(v, datetime) else v


# ----------------------------------------------------------------------------- product documents (Phase 5, B)
@app.post("/catalog/products/{product_id}/documents")
def product_document_upload(request: Request, product_id: int, doc_type: str = Form("spec"),
                            title: str = Form(""), file: UploadFile = File(...)):
    """Upload a PRIVATE product document. Validated (extension/MIME/size/dangerous/traversal via the tested
    _secure_validate), stored under PRODUCT_FILES_DIR with a safe generated name, original filename kept as
    metadata, admin-only, audited. NOT the disabled email-attachment path."""
    with Session(engine) as session:
        user = current_user(request, session)
        if not is_admin(user):
            return _forbidden()
        p = session.get(Product, product_id)
        if not p:
            return _not_found()
        data = file.file.read(ATT.MAX_BYTES + 1)
        ok, reason = ATT._secure_validate(file.filename or "", file.content_type or "", len(data))
        if not ok or len(data) > ATT.MAX_BYTES:
            return RedirectResponse(f"/catalog/products/{product_id}?tab=documents&err=1", status_code=303)
        doc = ProductDocument(product_id=product_id, doc_type=doc_type, title=title,
                              original_filename=file.filename or "", content_type=file.content_type or "",
                              size_bytes=len(data), uploaded_by=user.email, status="active")
        session.add(doc); session.flush()
        doc.file_path = _save_product_file(product_id, doc.id, file, data)
        session.add(doc)
        pipeline.audit(session, user, "product", product_id, "document_upload",
                       {"doc_id": doc.id, "type": doc_type})
        session.commit()
    return RedirectResponse(f"/catalog/products/{product_id}?tab=documents", status_code=303)


@app.get("/catalog/products/{product_id}/documents/{doc_id}/download")
def product_document_download(request: Request, product_id: int, doc_id: int):
    """Download a product document — ADMIN ONLY. Sellers never reach product files directly; seller access is
    granted ONLY by publishing a file to a specific request as a seller-safe deliverable (owner-scoped, via
    request_deliverable_file). `seller_safe` alone never grants access. Path re-validated inside
    PRODUCT_FILES_DIR (no traversal)."""
    with Session(engine) as session:
        user = current_user(request, session)
        if not is_admin(user):
            return _not_found()                          # 404, not 403 — don't reveal it exists
        doc = session.get(ProductDocument, doc_id)
        if not doc or doc.product_id != product_id or doc.status != "active":
            return _not_found()
        rel = doc.file_path
    path = (PRODUCT_FILES_DIR / rel).resolve()
    if not str(path).startswith(str(PRODUCT_FILES_DIR.resolve()) + os.sep) or not path.exists():
        return _not_found()
    return FileResponse(str(path), filename=doc.original_filename or path.name,
                        media_type=doc.content_type or "application/octet-stream")


# --- seller-safe publication: link a file to a SPECIFIC request (owner-scoped) as a deliverable --------
_PUBLISHABLE_IMAGE_MIME = ("image/png", "image/jpeg")


def _doc_publishable(doc) -> bool:
    """Without malware scanning, a product document is publishable to a seller ONLY if it is a validated safe
    raster image (or an admin has explicitly marked it scanned). Other uploaded files stay quarantined."""
    if doc.quarantine == "scanned":
        return True
    return (doc.doc_type in ("product_image", "packaging_image")
            and (doc.content_type or "").lower() in _PUBLISHABLE_IMAGE_MIME)


def _publish_to_request(session, req_id, src_abs_path, orig_name, delivered_by):
    """Copy a private product/catalog file into the request's deliverable store and create a seller-safe
    RequestDeliverable. Access then flows through request_deliverable_file, which is OWNER-SCOPED (Seller A
    cannot read Seller B's requests). Returns the deliverable or None."""
    sr = session.get(ServiceRequest, req_id)
    if not sr or not src_abs_path.exists():
        return None
    data = src_abs_path.read_bytes()
    if len(data) > MAX_DOC_BYTES:
        return None
    dv = RequestDeliverable(request_id=req_id, note="Published from product catalog", seller_safe=True,
                            delivered_by=delivered_by)
    session.add(dv); session.flush()
    dest_dir = REQUEST_FILES_DIR / str(req_id)
    dest_dir.mkdir(parents=True, exist_ok=True)
    fname = f"{dv.id}_{_safe_name(orig_name)}"
    (dest_dir / fname).write_bytes(data)
    dv.file_path = f"{req_id}/{fname}"
    session.add(dv)
    return dv


@app.post("/catalog/products/{product_id}/documents/{doc_id}/publish")
def product_document_publish(request: Request, product_id: int, doc_id: int, req_id: int = Form(...)):
    """Publish a product IMAGE to a specific request as a seller-safe deliverable (owner-scoped access).
    Refuses anything but validated raster images / scanned docs (quarantine)."""
    with Session(engine) as session:
        user = current_user(request, session)
        if not is_admin(user):
            return _forbidden()
        doc = session.get(ProductDocument, doc_id)
        if not doc or doc.product_id != product_id or doc.status != "active":
            return _not_found()
        if not _doc_publishable(doc):
            return RedirectResponse(f"/catalog/products/{product_id}?tab=documents&err=quarantined",
                                    status_code=303)
        src = (PRODUCT_FILES_DIR / doc.file_path).resolve()
        if not str(src).startswith(str(PRODUCT_FILES_DIR.resolve()) + os.sep):
            return _not_found()
        dv = _publish_to_request(session, req_id, src, doc.original_filename or "document", user.email)
        if dv:
            doc.seller_safe = True; session.add(doc)
            pipeline.audit(session, user, "product", product_id, "document_publish",
                           {"doc_id": doc_id, "request_id": req_id, "deliverable_id": dv.id})
            session.commit()
    return RedirectResponse(f"/catalog/products/{product_id}?tab=documents", status_code=303)


@app.post("/catalog/products/{product_id}/documents/{doc_id}/scan-clear")
def product_document_scan_clear(request: Request, product_id: int, doc_id: int):
    """Admin marks a quarantined document as scanned (the malware-scan integration point). Until a real
    scanner is wired, this is an explicit admin attestation — audited."""
    with Session(engine) as session:
        user = current_user(request, session)
        if not is_admin(user):
            return _forbidden()
        doc = session.get(ProductDocument, doc_id)
        if not doc or doc.product_id != product_id:
            return _not_found()
        doc.quarantine = "scanned"; session.add(doc)
        pipeline.audit(session, user, "product", product_id, "document_scan_clear", {"doc_id": doc_id})
        session.commit()
    return RedirectResponse(f"/catalog/products/{product_id}?tab=documents", status_code=303)


@app.post("/catalog/products/{product_id}/documents/{doc_id}/archive")
def product_document_archive(request: Request, product_id: int, doc_id: int):
    """Archive (never destructively delete) a product document."""
    with Session(engine) as session:
        user = current_user(request, session)
        if not is_admin(user):
            return _forbidden()
        doc = session.get(ProductDocument, doc_id)
        if not doc or doc.product_id != product_id:
            return _not_found()
        doc.status = "archived"; session.add(doc)
        pipeline.audit(session, user, "product", product_id, "document_archive", {"doc_id": doc_id})
        session.commit()
    return RedirectResponse(f"/catalog/products/{product_id}?tab=documents", status_code=303)


# ----------------------------------------------------------------------------- Catalog Studio (Phase 5, B)
@app.get("/catalog/studio", response_class=HTMLResponse)
def catalog_studio(request: Request, product_id: int = 0):
    """Admin-only Catalog Studio — generate a branded one-pager PDF. Shows provider status honestly (Higgs is
    'Not configured' until real credentials/API docs exist)."""
    with Session(engine) as session:
        user = current_user(request, session)
        if not is_admin(user):
            return _forbidden()
        products = session.exec(select(Product).where(Product.active == True)                # noqa: E712
                                .order_by(Product.name).limit(500)).all()
        jobs = session.exec(select(CatalogGenerationJob).order_by(
            CatalogGenerationJob.id.desc()).limit(50)).all()
        prod_names = {p.id: p.name for p in session.exec(select(Product)).all()}
    return templates.TemplateResponse("catalog_studio.html", {
        "request": request, "user": user, "active": "studio", "products": products, "jobs": jobs,
        "prod_names": prod_names, "provider_status": STUDIO.provider_status(),
        "selected": product_id})


@app.post("/catalog/studio/generate")
def catalog_studio_generate(request: Request, product_id: int = Form(...), provider: str = Form("builtin")):
    """Create a generation job and run it inline (bounded). A failure marks the job failed + opens a work item
    — it never blocks. Only APPROVED product fields are sent; contact is Go4it."""
    with Session(engine) as session:
        user = current_user(request, session)
        if not is_admin(user):
            return _forbidden()
        p = session.get(Product, product_id)
        if not p:
            return _not_found()
        version = (session.exec(select(func.count()).select_from(CatalogGenerationJob)
                                .where(CatalogGenerationJob.product_id == product_id)).one() or 0) + 1
        job = CatalogGenerationJob(product_id=product_id, version=version, status="draft",
                                   provider=provider, generated_by=user.email)
        session.add(job); session.flush()
        pv = session.exec(select(ProductPriceVersion).where(
            ProductPriceVersion.product_id == product_id,
            ProductPriceVersion.status == "approved").order_by(ProductPriceVersion.version.desc())).first()
        STUDIO.generate_catalog(session, job, PRODUCT_FILES_DIR, price_version=pv, actor=user)
        session.commit()
        if job.status == "failed":
            WQ.create_work_item_safe(session, actor=user, type="catalog_generation_failed",
                                     title=f"Catalog generation failed (product {product_id})",
                                     description=(job.error or "generation failed")[:400],
                                     related_product_id=product_id,
                                     idempotency_key=f"catalog_generation_failed:job:{job.id}",
                                     condition_version=f"v{job.version}")
            session.commit()
    return RedirectResponse(f"/catalog/studio?product_id={product_id}", status_code=303)


@app.post("/catalog/studio/{job_id}/action")
def catalog_studio_action(request: Request, job_id: int, action: str = Form(...), req_id: str = Form("")):
    """Approve / archive a generated catalog, or PUBLISH an approved Go4it PDF to a SPECIFIC request as a
    seller-safe deliverable (owner-scoped access). Never auto-emails."""
    with Session(engine) as session:
        user = current_user(request, session)
        if not is_admin(user):
            return _forbidden()
        job = session.get(CatalogGenerationJob, job_id)
        if not job:
            return _not_found()
        if action == "approve" and job.status == "needs_review":
            job.status = "approved"
        elif action == "archive":
            job.status = "archived"
        elif action == "publish_seller_safe" and job.status == "approved" and req_id.isdigit():
            src = (PRODUCT_FILES_DIR / job.file_path).resolve()
            if str(src).startswith(str(PRODUCT_FILES_DIR.resolve()) + os.sep):
                dv = _publish_to_request(session, int(req_id), src, f"go4it_catalog_{job_id}.pdf", user.email)
                if dv:
                    job.seller_safe = True         # marker only; access is via the owner-scoped deliverable
                    pipeline.audit(session, user, "product", job.product_id, "catalog_publish",
                                   {"job": job_id, "request_id": int(req_id), "deliverable_id": dv.id})
        session.add(job)
        if action != "publish_seller_safe":
            pipeline.audit(session, user, "product", job.product_id, "catalog_" + action, {"job": job_id})
        session.commit()
        pid = job.product_id
    return RedirectResponse(f"/catalog/studio?product_id={pid}", status_code=303)


@app.get("/catalog/studio/{job_id}/download")
def catalog_studio_download(request: Request, job_id: int):
    """Download a generated catalog PDF — ADMIN ONLY. Seller access is only via a published owner-scoped
    deliverable (request_deliverable_file). Never auto-sent."""
    with Session(engine) as session:
        user = current_user(request, session)
        if not is_admin(user):
            return _not_found()
        job = session.get(CatalogGenerationJob, job_id)
        if not job or not job.file_path or job.status not in ("needs_review", "approved"):
            return _not_found()
        rel = job.file_path
    path = (PRODUCT_FILES_DIR / rel).resolve()
    if not str(path).startswith(str(PRODUCT_FILES_DIR.resolve()) + os.sep) or not path.exists():
        return _not_found()
    return FileResponse(str(path), filename=f"go4it_catalog_{job_id}.pdf", media_type="application/pdf")


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
            # logging a contact stays open to the lead's owner; actually EMAILING a buyer is internal-only
            if not is_admin(user) or not authz.has_permission(session, user, "outreach.email.send"):
                return _forbidden()
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
        # arm the auto follow-up on the FIRST successful email (worker sends FU#1 at +FOLLOWUP_DAYS_1) — never for a
        # managed buyer, whose follow-ups are campaign steps (app/followups.py skips managed leads too)
        if status == "sent" and prior_sent == 0 and FOLLOWUP_ENABLED and not lead.managed:
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


QUOTE_STATUSES = QWF.STATUSES


@app.get("/quotes", response_class=HTMLResponse)
def quotes_list(request: Request, q: str = "", status: str = "", currency: str = "", incoterm: str = "",
                sent: str = "", viewed: str = "", accepted: str = "", page: int = 1):
    """Searchable, filterable, paginated quotes workspace (never loads all quotes). Tenant-scoped."""
    per = 50
    with Session(engine) as session:
        user = current_user(request, session)
        stmt = scoped(select(Quote), Quote.owner_id, user)
        if q:
            stmt = stmt.where(Quote.tracking_code.ilike(f"%{q.strip()}%"))
        if status:
            stmt = stmt.where(Quote.status == status)
        if currency:
            stmt = stmt.where(Quote.quote_currency == currency.upper())
        if incoterm:
            stmt = stmt.where(Quote.incoterm == incoterm.upper())
        if sent == "yes":
            stmt = stmt.where(Quote.status.in_(("sent", "viewed", "accepted", "rejected", "change_requested")))
        if viewed == "yes":
            stmt = stmt.where(Quote.viewed_at != None)                      # noqa: E711
        elif viewed == "no":
            stmt = stmt.where(Quote.viewed_at == None)                      # noqa: E711
        if accepted == "yes":
            stmt = stmt.where(Quote.status == "accepted")
        elif accepted == "no":
            stmt = stmt.where(Quote.status == "rejected")
        total = session.exec(select(func.count()).select_from(stmt.subquery())).one()
        pages = max(1, (total + per - 1) // per)
        page = min(max(1, page), pages)
        quotes = session.exec(stmt.order_by(Quote.id.desc()).offset((page - 1) * per).limit(per)).all()
        lead_ids = {qt.lead_id for qt in quotes} or {0}
        leads = {l.id: l for l in session.exec(select(Lead).where(Lead.id.in_(lead_ids))).all()}
        products = {p.id: p for p in session.exec(select(Product)).all()}
        rows = [{"q": qt, "lead": leads.get(qt.lead_id), "product": products.get(qt.product_id)}
                for qt in quotes]
    return templates.TemplateResponse("quotes_list.html", {
        "request": request, "user": user, "rows": rows, "total": total, "page": page, "pages": pages,
        "statuses": QUOTE_STATUSES,
        "f": {"q": q, "status": status, "currency": currency, "incoterm": incoterm, "sent": sent,
              "viewed": viewed, "accepted": accepted}})


@app.get("/quotes/export.csv", response_class=PlainTextResponse)
def quotes_export(request: Request):
    """Admin-only, audited CSV export of quotes (tenant-scoped)."""
    import csv as _csv
    import io as _io
    with Session(engine) as session:
        user = current_user(request, session)
        if not is_admin(user):
            return _forbidden()
        quotes = session.exec(select(Quote).order_by(Quote.id)).all()
        out = _io.StringIO(); w = _csv.writer(out)
        w.writerow(["ref", "version", "status", "incoterm", "currency", "quantity", "delivered_total",
                    "validity_days", "created"])
        for qt in quotes:
            w.writerow([qt.tracking_code, qt.version, qt.status, qt.incoterm, qt.quote_currency, qt.quantity,
                        qt.delivered_total, qt.validity_days,
                        qt.created_at.strftime("%Y-%m-%d") if qt.created_at else ""])
        pipeline.audit(session, user, "export", None, "quotes_export", {"rows": len(quotes)})
        session.commit()
    return PlainTextResponse(out.getvalue(),
                             headers={"Content-Disposition": "attachment; filename=go4it_quotes.csv"})


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
    # the hardened secure /q/ link is shown ONCE right after send (raw token is never stored) — pop the flash
    portal_link = request.session.pop("_portal_link", "")
    return templates.TemplateResponse(
        "quote_detail.html",
        {"request": request, "user": user, "q": q, "lead": lead, "product": product,
         "breakdown": breakdown, "fx": fx, "can_approve": role_at_least(user, "agent"),
         "share_url": share_url, "portal_link": portal_link},
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
        if q and q.status in ("draft", "needs_review"):
            ver = ensure_version(session, q, actor=user)     # freeze the immutable snapshot at approval
            ok, why = QWF.transition(session, q, "approved", actor=user, reason="admin approval")
            if ok:
                q.approved_by = user.email
                session.add(q)
                from .models import QuoteApproval
                session.add(QuoteApproval(quote_version_id=ver.id, approved_by=user.email,
                                          checks=json.dumps(_quote_approval_checks(session, q, ver))))
                _ensure_share_token(session, q)   # legacy /p/ link (kept for back-compat)
                pipeline.audit(session, user, "quote", q.id, "quote_approved", {"version": q.version})
            session.commit()
    return RedirectResponse(f"/quotes/{quote_id}", status_code=303)


def _quote_approval_checks(session, q, ver) -> dict:
    """The approval checklist results (recorded on QuoteApproval). Buyer-facing content must carry no seller
    identity / internal cost / margin — the buyer template + PDF are structurally cost-free (test-guarded)."""
    return {"product": bool(q.product_id), "quantity": q.quantity > 0, "price": q.delivered_total > 0,
            "currency": bool(q.quote_currency), "incoterm": bool(q.incoterm),
            "no_unresolved_vars": not PDF.has_unresolved_vars(ver.commercial_text or ""),
            "ddp_ok": not (q.incoterm == "DDP" and not q.dest_border)}


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
            ver = ensure_version(session, q, actor=user)
            ok, why = QWF.transition(session, q, "sent", actor=user, reason="quote sent")
            if not ok:
                return RedirectResponse(f"/quotes/{quote_id}?error=send", status_code=303)
            _ensure_share_token(session, q)
            raw_tok, _t = QP.mint_token(session, q, ver, actor=user, valid_days=q.validity_days)  # hashed buyer token
            request.session["_portal_link"] = f"{BASE_URL}/q/#{raw_tok}"   # secure FRAGMENT link (token client-side only), shown once
            session.add(q)
            # Supersede any prior live quote for this lead so its public link stops serving an
            # outdated price (the public routes only serve approved/sent — superseded ones 404).
            for old in session.exec(select(Quote).where(
                    Quote.lead_id == q.lead_id, Quote.id != q.id,
                    Quote.status.in_(["approved", "sent", "viewed"]))).all():
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


# ----------------------------------------------------------------------------- quote PDF + portal (Phase 6)
QUOTE_FILES_DIR = BASE_DIR.parent / "quote_files"   # PRIVATE, outside /static


@app.post("/quotes/{quote_id}/generate-pdf")
def quote_generate_pdf(request: Request, quote_id: int):
    """Generate the immutable branded quote PDF (admin). Only for an approved/sent version."""
    with Session(engine) as session:
        user = current_user(request, session)
        if not role_at_least(user, "agent"):
            return _forbidden()
        q = session.get(Quote, quote_id)
        if not q or not owns(q.owner_id, user):
            return _not_found()
        if q.status not in ("approved", "sent", "viewed", "accepted"):
            return RedirectResponse(f"/quotes/{quote_id}?error=notapproved", status_code=303)
        ver = ensure_version(session, q, actor=user)
        link = f"{BASE_URL}/p/{q.share_token}" if q.share_token else ""
        doc, err = QPDF.generate_quote_pdf(session, q, ver, QUOTE_FILES_DIR, actor=user, link=link)
        if doc:
            pipeline.audit(session, user, "quote", q.id, "quote_pdf_generated",
                           {"doc_id": doc.id, "sha256": doc.sha256[:12]})
        session.commit()
    return RedirectResponse(f"/quotes/{quote_id}", status_code=303)


@app.get("/quotes/{quote_id}/pdf")
def quote_pdf_download(request: Request, quote_id: int):
    """Admin download of the generated quote PDF (path re-validated inside QUOTE_FILES_DIR)."""
    with Session(engine) as session:
        user = current_user(request, session)
        q = session.get(Quote, quote_id)
        if not q or not owns(q.owner_id, user):
            return _not_found()
        from .models import QuoteDocument, QuoteVersion
        ver = session.get(QuoteVersion, q.current_version_id) if q.current_version_id else None
        doc = session.get(QuoteDocument, ver.pdf_document_id) if ver and ver.pdf_document_id else None
        if not doc or doc.status != "active":
            return _not_found()
        rel = doc.file_path
    path = (QUOTE_FILES_DIR / rel).resolve()
    if not str(path).startswith(str(QUOTE_FILES_DIR.resolve()) + os.sep) or not path.exists():
        return _not_found()
    return FileResponse(str(path), filename=f"quote_{quote_id}.pdf", media_type="application/pdf")


@app.post("/quotes/{quote_id}/revise")
def quote_revise(request: Request, quote_id: int):
    """Duplicate a quote into a new DRAFT version (never edits the sent/approved/accepted one)."""
    with Session(engine) as session:
        user = current_user(request, session)
        if not role_at_least(user, "agent"):
            return _forbidden()
        q = session.get(Quote, quote_id)
        if not q or not owns(q.owner_id, user):
            return _not_found()
        dup = revise_quote(session, q, actor=user)
        newid = dup.id
    return RedirectResponse(f"/quotes/{newid}", status_code=303)


@app.post("/quotes/{quote_id}/create-deal")
def quote_create_deal(request: Request, quote_id: int):
    """Admin action from the 'accepted_quote_needs_deal' task — idempotently create the Deal for the accepted
    version (unique per version; double-submit safe)."""
    with Session(engine) as session:
        user = current_user(request, session)
        if not role_at_least(user, "agent"):
            return _forbidden()
        q = session.get(Quote, quote_id)
        if not q or not owns(q.owner_id, user):
            return _not_found()
        from .models import QuoteVersion
        ver = session.get(QuoteVersion, q.current_version_id) if q.current_version_id else None
        if not ver or q.status != "accepted":
            return RedirectResponse(f"/quotes/{quote_id}?error=notaccepted", status_code=303)
        deal, created = ensure_deal_for_quote_version(session, ver, actor=user)
        WQ.resolve_by_key(session, f"accepted_quote_needs_deal:qv:{ver.id}", user, "deal created")
        session.commit()
        dealid = deal.id if deal else 0
    return RedirectResponse(f"/deals/{dealid}" if dealid else "/quotes", status_code=303)


def _portal_headers_apply(resp, nonce):
    for k, v in QP.portal_headers(nonce).items():
        resp.headers[k] = v
    return resp


def _portal_load(request, session):
    """Resolve the opaque cookie sid → the SERVER-SIDE PortalSession → (quote, version, csrf) or None. The
    cookie holds only the opaque sid; all validity lives server-side."""
    loaded = QP.load_session(session, request.session.get("qp_sid"))
    if not loaded:
        return None
    q, ver, csrf = loaded
    if q.status not in QWF.PORTAL_VIEWABLE:
        return None
    return q, ver, csrf


_PORTAL_BOOTSTRAP = """<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><meta name="robots" content="noindex, nofollow">
<title>Opening quotation…</title><style>body{{font-family:'Helvetica Neue',Arial,sans-serif;background:#0f172a;
color:#e2e8f0;margin:0;padding:48px;text-align:center}}#msg{{color:#94a3b8;font-size:15px}}</style></head>
<body><div id="msg">Opening your quotation…</div><script nonce="{nonce}">
(function(){{var h=(location.hash||'').replace(/^#/,'');history.replaceState(null,'','/q/');
if(!h){{document.getElementById('msg').textContent='This link is invalid.';return;}}
fetch('/q/exchange',{{method:'POST',headers:{{'Content-Type':'application/x-www-form-urlencoded'}},
body:'token='+encodeURIComponent(h),credentials:'same-origin'}}).then(function(r){{return r.json();}})
.then(function(d){{if(d&&d.ok){{location.replace('/q/session');}}else{{
document.getElementById('msg').textContent='This quotation link is not available.';}}}})
.catch(function(){{document.getElementById('msg').textContent='Unable to open the quotation.';}});}})();
</script></body></html>"""


@app.get("/q/", response_class=HTMLResponse)
def quote_portal_bootstrap(request: Request):
    """Bootstrap page for the buyer link `/q/#<token>`. The token lives ONLY in the URL FRAGMENT — never sent
    to the server or a proxy access log. A nonce'd script reads the fragment, strips it via history.replaceState,
    and POSTs it to /q/exchange. GET reaches the proxy as just `/q/` (no token)."""
    if not RL.allow(RL.client_key(request, "qboot"), limit=60, window=60):
        return HTMLResponse("Too many requests.", status_code=429)
    nonce = secrets.token_urlsafe(16)
    resp = HTMLResponse(_PORTAL_BOOTSTRAP.format(nonce=nonce))
    return _portal_headers_apply(resp, nonce)


@app.post("/q/exchange")
def quote_portal_exchange(request: Request, token: str = Form("")):
    """One-time POST exchange: atomically CONSUME the link token (single-use) and open a SERVER-SIDE
    PortalSession; the browser gets only an opaque sid cookie. The raw token never appears in a URL/log."""
    if not RL.allow(RL.client_key(request, "qexch"), limit=30, window=60):
        return JSONResponse({"ok": False}, status_code=429)
    caller_sid = request.session.get("qp_sid")   # this browser's existing session, if any (for legit re-open)
    with Session(engine) as session:
        opened = QP.open_session(session, token, current_sid=caller_sid)
        if not opened:
            # unknown/expired/revoked token, OR a consumed token presented without its matching cookie
            return JSONResponse({"ok": False}, status_code=410)
        ps, q, ver = opened
        QWF.mark_expired_if_due(session, q)
        if q.status not in QWF.PORTAL_VIEWABLE:
            QP.revoke_session(session, ps.sid)
            session.commit()
            return JSONResponse({"ok": False}, status_code=404)
        sid = ps.sid
        session.commit()
    request.session["qp_sid"] = sid        # cookie carries ONLY the opaque server-session id
    return JSONResponse({"ok": True})


# NOTE: these fixed /q/session* routes are declared BEFORE /q/{token} so the token catch-all never captures
# them. They live under the already-auth-exempt "/q/" prefix and are tokenless (cookie-session backed).
@app.get("/q/session", response_class=HTMLResponse)
def quote_portal_view(request: Request):
    """Render the buyer portal from the cookie session (TOKENLESS url). Pure GET — the `viewed` event is
    recorded by a separate idempotent POST (/q/session/view) fired after the page renders."""
    nonce = secrets.token_urlsafe(16)
    with Session(engine) as session:
        loaded = _portal_load(request, session)
        if not loaded:
            return _portal_headers_apply(
                HTMLResponse("This quotation link is not available.", status_code=404), nonce)
        q, ver, csrf = loaded
        lead = session.get(Lead, q.lead_id)
        product = session.get(Product, q.product_id)
        options = QP.buyer_options(ver)
        expires = QWF.expiry_at(q)
    resp = templates.TemplateResponse("q_portal.html", {
        "request": request, "q": q, "ver": ver, "lead": lead, "product": product, "options": options,
        "csrf": csrf, "nonce": nonce, "expires": expires, "today": datetime.utcnow()})
    return _portal_headers_apply(resp, nonce)


@app.post("/q/session/view")
def quote_portal_mark_viewed(request: Request, csrf: str = Form("")):
    """Idempotent controlled view event — fired by the page AFTER it renders (never on the document GET).
    CSRF-checked. Records sent→viewed once."""
    with Session(engine) as session:
        loaded = _portal_load(request, session)
        if not loaded:
            return JSONResponse({"ok": False}, status_code=404)
        q, ver, good_csrf = loaded
        if not QP.csrf_ok(good_csrf, csrf):
            return JSONResponse({"ok": False}, status_code=403)
        QP.record_view(session, q, ver)
        session.commit()
    return JSONResponse({"ok": True})


@app.post("/q/session/respond", response_class=HTMLResponse)
def quote_portal_decide(request: Request, action: str = Form(...), message: str = Form(""),
                        csrf: str = Form("")):
    """Buyer accept/reject/change — POST-only, CSRF-protected, idempotent for EVERY decision, against the exact
    version the buyer saw. Never creates a Deal (raises an admin task). Never exposes seller identity."""
    if not RL.allow(RL.client_key(request, "qrespond"), limit=20, window=60):
        return HTMLResponse("Too many requests. Please retry shortly.", status_code=429)
    nonce = secrets.token_urlsafe(16)
    with Session(engine) as session:
        loaded = _portal_load(request, session)
        if not loaded:
            return _portal_headers_apply(
                HTMLResponse("This quotation link is not available.", status_code=404), nonce)
        q, ver, good_csrf = loaded
        if not QP.csrf_ok(good_csrf, csrf):
            return _portal_headers_apply(HTMLResponse("Invalid request token.", status_code=403), nonce)
        result, why = QP.record_buyer_action(session, q, ver, action, message=message)
        if result == "invalid":
            return _portal_headers_apply(HTMLResponse("Bad request", status_code=400), nonce)
        lead = session.get(Lead, q.lead_id)
        if lead and result in ("accepted", "rejected", "change_requested"):
            if lead.buyer_replied_at is None:
                lead.buyer_replied_at = datetime.utcnow()
            if lead.status in ("new", "quoted"):
                lead.status = "negotiating"
            session.add(Outreach(lead_id=lead.id, direction="in", channel="portal",
                                 from_addr=(lead.email or "buyer"), subject=f"Quote {q.tracking_code}",
                                 body=f"Buyer {result} {q.tracking_code}."[:2000], status="received"))
            session.add(lead)
        code = q.tracking_code
        session.commit()
    accepted = result in ("accepted", "already_accepted")
    resp = templates.TemplateResponse("proforma_thanks.html",
                                      {"request": request, "accepted": accepted, "code": code})
    return _portal_headers_apply(resp, nonce)


@app.get("/q/session/pdf")
def quote_portal_pdf(request: Request):
    """Buyer PDF download via the cookie session (tokenless). Only a presentable version with a generated PDF."""
    if not RL.allow(RL.client_key(request, "qpdf"), limit=20, window=60):
        return HTMLResponse("Too many requests.", status_code=429)
    with Session(engine) as session:
        loaded = _portal_load(request, session)
        if not loaded:
            return _not_found()
        q, ver, _csrf = loaded
        from .models import QuoteDocument
        doc = session.get(QuoteDocument, ver.pdf_document_id) if ver.pdf_document_id else None
        if not doc or doc.status != "active":
            return _not_found()
        rel = doc.file_path
    path = (QUOTE_FILES_DIR / rel).resolve()
    if not str(path).startswith(str(QUOTE_FILES_DIR.resolve()) + os.sep) or not path.exists():
        return _not_found()
    resp = FileResponse(str(path), filename=f"quote_{ver.quote_id}.pdf", media_type="application/pdf")
    resp.headers["Cache-Control"] = "no-store"
    resp.headers["Referrer-Policy"] = "no-referrer"
    return resp


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
        progress = SP.deal_seller_progress(session, deal)   # sanitized operational view (safe for everyone)
    return templates.TemplateResponse(
        "deal_detail.html",
        {"request": request, "user": user, "deal": deal, "lead": lead, "docs": docs,
         "stages": DEAL_STAGES, "next": nxt, "missing": missing,
         "required_for": REQUIRED_DOCS, "doc_types": DOC_TYPES, "progress": progress,
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


# ----------------------------------------------------------------------------- contracts (Phase 6, B)
CONTRACT_FILES_DIR = BASE_DIR.parent / "contract_files"   # PRIVATE, outside /static


@app.get("/contracts", response_class=HTMLResponse)
def contracts_list(request: Request, q: str = "", ctype: str = "", side: str = "", status: str = "",
                   country: str = "", page: int = 1):
    """Admin-only contracts workspace — search + filters + pagination."""
    per = 50
    with Session(engine) as session:
        user = current_user(request, session)
        if not is_admin(user):
            return _forbidden()
        stmt = select(Contract)
        if q:
            like = f"%{q.strip()}%"
            stmt = stmt.where(Contract.tracking_code.ilike(like))
        if ctype:
            stmt = stmt.where(Contract.contract_type == ctype)
        if side:
            stmt = stmt.where(Contract.side == side)
        if status:
            stmt = stmt.where(Contract.status == status)
        if country:
            stmt = stmt.where(Contract.country == country.upper())
        total = session.exec(select(func.count()).select_from(stmt.subquery())).one()
        pages = max(1, (total + per - 1) // per)
        page = min(max(1, page), pages)
        contracts = session.exec(stmt.order_by(Contract.id.desc()).offset((page - 1) * per).limit(per)).all()
        companies = {c.id: c for c in session.exec(select(Company)).all()}
    return templates.TemplateResponse("contracts.html", {
        "request": request, "user": user, "active": "contracts", "contracts": contracts,
        "companies": companies, "total": total, "page": page, "pages": pages,
        "types": CONTRACT.CONTRACT_TYPES, "statuses": CONTRACT.STATUSES,
        "f": {"q": q, "ctype": ctype, "side": side, "status": status, "country": country}})


@app.post("/contracts")
def contract_new(request: Request, contract_type: str = Form(...), side: str = Form(...),
                 company_id: str = Form(""), country: str = Form(""), quote_id: str = Form(""),
                 deal_id: str = Form(""), terms: str = Form("")):
    """Create a contract — the admin EXPLICITLY chooses type + side + party (never auto-assumed)."""
    with Session(engine) as session:
        user = current_user(request, session)
        if not is_admin(user):
            return _forbidden()
        c, _v = CONTRACT.create_contract(
            session, contract_type=contract_type, side=side,
            company_id=int(company_id) if company_id.isdigit() else None,
            country=country, quote_id=int(quote_id) if quote_id.isdigit() else None,
            deal_id=int(deal_id) if deal_id.isdigit() else None, terms=terms,
            owner_id=user.id, actor=user)
        cid = c.id
    return RedirectResponse(f"/contracts/{cid}", status_code=303)


@app.get("/contracts/{contract_id}", response_class=HTMLResponse)
def contract_detail(request: Request, contract_id: int):
    with Session(engine) as session:
        user = current_user(request, session)
        if not is_admin(user):
            return _forbidden()
        c = session.get(Contract, contract_id)
        if not c:
            return _not_found()
        versions = session.exec(select(ContractVersion).where(
            ContractVersion.contract_id == c.id).order_by(ContractVersion.version.desc())).all()
        docs = session.exec(select(ContractDocument).where(
            ContractDocument.contract_id == c.id, ContractDocument.status == "active")).all()
        sigs = session.exec(select(SignatureEvent).where(SignatureEvent.contract_id == c.id)).all()
        events = session.exec(select(ContractStatusEvent).where(
            ContractStatusEvent.contract_id == c.id).order_by(ContractStatusEvent.id.desc())).all()
        company = session.get(Company, c.company_id) if c.company_id else None
    return templates.TemplateResponse("contract_detail.html", {
        "request": request, "user": user, "active": "contracts", "c": c, "versions": versions, "docs": docs,
        "sigs": sigs, "events": events, "company": company, "statuses": CONTRACT.STATUSES,
        "next_states": sorted(CONTRACT.TRANSITIONS.get(c.status, set())),
        "esign_status": ESIGN.provider_status(), "legal_notice": CONTRACT.LEGAL_NOTICE})


@app.post("/contracts/{contract_id}/action")
def contract_action(request: Request, contract_id: int, action: str = Form(...), reason: str = Form(""),
                    signer_name: str = Form(""), signer_email: str = Form(""), party_role: str = Form("buyer")):
    with Session(engine) as session:
        user = current_user(request, session)
        if not is_admin(user):
            return _forbidden()
        c = session.get(Contract, contract_id)
        if not c:
            return _not_found()
        if action == "revise":
            CONTRACT.revise_contract(session, c, actor=user)
        elif action == "amend":
            CONTRACT.revise_contract(session, c, actor=user, is_amendment=True)
        elif action == "manual_sign":
            ver = session.get(ContractVersion, c.current_version_id)
            ESIGN.record_manual_signature(session, c, ver, party_role=party_role, signer_name=signer_name,
                                          signer_email=signer_email, actor=user)
            CONTRACT.transition(session, c, "signed", actor=user, reason="manual signature recorded")
        elif action in CONTRACT.STATUSES:
            CONTRACT.transition(session, c, action, actor=user, reason=reason)
        session.commit()
    return RedirectResponse(f"/contracts/{contract_id}", status_code=303)


@app.post("/contracts/{contract_id}/documents")
def contract_doc_upload(request: Request, contract_id: int, file: UploadFile = File(...)):
    """Upload a SIGNED contract copy — private, quarantined, admin-only until scan-cleared, hashed, never
    overwrites the generated original."""
    with Session(engine) as session:
        user = current_user(request, session)
        if not is_admin(user):
            return _forbidden()
        c = session.get(Contract, contract_id)
        if not c:
            return _not_found()
        data = file.file.read(ATT.MAX_BYTES + 1)
        ok, reason = ATT._secure_validate(file.filename or "", file.content_type or "", len(data))
        if not ok or len(data) > ATT.MAX_BYTES:
            return RedirectResponse(f"/contracts/{contract_id}?err=1", status_code=303)
        doc = ContractDocument(contract_id=c.id, contract_version_id=c.current_version_id, kind="signed_upload",
                               original_filename=file.filename or "", content_type=file.content_type or "",
                               size_bytes=len(data), sha256=PDF.sha256_bytes(data), quarantine="quarantined",
                               uploaded_by=user.email)
        session.add(doc); session.flush()
        dest = CONTRACT_FILES_DIR / str(c.id)
        dest.mkdir(parents=True, exist_ok=True)
        fname = f"{doc.id}_{_safe_name(file.filename)}"
        (dest / fname).write_bytes(data)
        doc.file_path = f"{c.id}/{fname}"
        session.add(doc)
        pipeline.audit(session, user, "contract", c.id, "signed_doc_upload", {"doc_id": doc.id})
        session.commit()
    return RedirectResponse(f"/contracts/{contract_id}", status_code=303)


@app.get("/contracts/{contract_id}/documents/{doc_id}/download")
def contract_doc_download(request: Request, contract_id: int, doc_id: int):
    """ADMIN-ONLY download of a contract document (generated or signed upload)."""
    with Session(engine) as session:
        user = current_user(request, session)
        if not is_admin(user):
            return _not_found()
        doc = session.get(ContractDocument, doc_id)
        if not doc or doc.contract_id != contract_id or doc.status != "active":
            return _not_found()
        rel = doc.file_path
    path = (CONTRACT_FILES_DIR / rel).resolve()
    if not str(path).startswith(str(CONTRACT_FILES_DIR.resolve()) + os.sep) or not path.exists():
        return _not_found()
    return FileResponse(str(path), filename=doc.original_filename or path.name,
                        media_type=doc.content_type or "application/octet-stream")


@app.post("/contracts/{contract_id}/documents/{doc_id}/scan-clear")
def contract_doc_scan_clear(request: Request, contract_id: int, doc_id: int):
    with Session(engine) as session:
        user = current_user(request, session)
        if not is_admin(user):
            return _forbidden()
        doc = session.get(ContractDocument, doc_id)
        if not doc or doc.contract_id != contract_id:
            return _not_found()
        doc.quarantine = "scanned"; session.add(doc)
        pipeline.audit(session, user, "contract", contract_id, "signed_doc_scan_clear", {"doc_id": doc_id})
        session.commit()
    return RedirectResponse(f"/contracts/{contract_id}", status_code=303)


@app.get("/contract-templates", response_class=HTMLResponse)
def contract_templates_list(request: Request):
    with Session(engine) as session:
        user = current_user(request, session)
        if not is_admin(user):
            return _forbidden()
        tpls = session.exec(select(ContractTemplate).order_by(ContractTemplate.id.desc())).all()
    return templates.TemplateResponse("contract_templates.html", {
        "request": request, "user": user, "active": "ctemplates", "templates_list": tpls,
        "types": CONTRACT.CONTRACT_TYPES, "legal_notice": CONTRACT.LEGAL_NOTICE})


@app.post("/contract-templates")
def contract_template_save(request: Request, name: str = Form(...), contract_type: str = Form("buyer_sales"),
                           side: str = Form("buyer"), body: str = Form(""), allowed_vars: str = Form("")):
    with Session(engine) as session:
        user = current_user(request, session)
        if not is_admin(user):
            return _forbidden()
        allow = [v.strip() for v in allowed_vars.replace(",", " ").split() if v.strip()]
        # reject a template whose body uses a variable not on the allowlist
        unknown = CONTRACT.template_vars(body) - set(allow)
        if unknown:
            return RedirectResponse("/contract-templates?err=vars", status_code=303)
        session.add(ContractTemplate(name=name, contract_type=contract_type, side=side, body=body,
                                     allowed_vars=json.dumps(allow), created_by=user.email))
        pipeline.audit(session, user, "contract_template", None, "template_save", {"name": name})
        session.commit()
    return RedirectResponse("/contract-templates", status_code=303)


@app.post("/contract-templates/{tpl_id}/archive")
def contract_template_archive(request: Request, tpl_id: int):
    with Session(engine) as session:
        user = current_user(request, session)
        if not is_admin(user):
            return _forbidden()
        t = session.get(ContractTemplate, tpl_id)
        if t:
            t.status = "archived"; session.add(t)
            pipeline.audit(session, user, "contract_template", tpl_id, "template_archive", {})
            session.commit()
    return RedirectResponse("/contract-templates", status_code=303)


# ----------------------------------------------------------------------------- commercial analytics (Phase 6)
@app.get("/commercial/analytics", response_class=HTMLResponse)
def commercial_analytics(request: Request):
    """Admin-only commercial analytics. Values are reported PER CURRENCY (never summed across currencies
    without an explicit FX snapshot). Count / value / conversion-rate are shown distinctly."""
    with Session(engine) as session:
        user = current_user(request, session)
        if not is_admin(user):
            return _forbidden()
        quotes = session.exec(select(Quote)).all()
        by_status = {}
        value_by_ccy = {}
        accepted_value_by_ccy = {}
        for q in quotes:
            by_status[q.status] = by_status.get(q.status, 0) + 1
            value_by_ccy.setdefault(q.quote_currency, 0.0)
            value_by_ccy[q.quote_currency] += q.delivered_total or 0
            if q.status == "accepted":
                accepted_value_by_ccy.setdefault(q.quote_currency, 0.0)
                accepted_value_by_ccy[q.quote_currency] += q.delivered_total or 0
        sent = sum(1 for q in quotes if q.status in ("sent", "viewed", "accepted", "rejected", "change_requested"))
        viewed = sum(1 for q in quotes if q.viewed_at is not None)
        accepted = by_status.get("accepted", 0)
        rejected = by_status.get("rejected", 0)
        expired = by_status.get("expired", 0)
        rates = {
            "sent_to_viewed": round(viewed / sent * 100, 1) if sent else 0,
            "viewed_to_accepted": round(accepted / viewed * 100, 1) if viewed else 0,
            "rejection": round(rejected / sent * 100, 1) if sent else 0,
            "expiry": round(expired / max(1, len(quotes)) * 100, 1),
        }
        contracts = session.exec(select(Contract)).all()
        contracts_review = sum(1 for c in contracts if c.status == "needs_review")
        contracts_sig = sum(1 for c in contracts if c.status in ("sent", "viewed"))
        deals = session.exec(select(Deal)).all()
        deal_value_by_ccy = {}
        planned = realized = 0.0
        for d in deals:
            planned += d.planned_margin or 0
            realized += d.realized_margin or 0
    return templates.TemplateResponse("commercial_analytics.html", {
        "request": request, "user": user, "active": "canalytics", "by_status": by_status,
        "value_by_ccy": value_by_ccy, "accepted_value_by_ccy": accepted_value_by_ccy, "rates": rates,
        "quote_total": len(quotes), "contracts_review": contracts_review, "contracts_sig": contracts_sig,
        "deal_count": len(deals), "planned_margin": round(planned, 2), "realized_margin": round(realized, 2)})


# ============================================================================= OPERATIONS (Phase 7)
# Admin-first operational execution: freight, shipments, documentation, payments & remittance, exceptions.
# Every management route is admin-only; sellers see only sanitized progress through their existing dashboard.
OPERATION_FILES_DIR = BASE_DIR.parent / "operation_files"   # PRIVATE, outside /static


def _ops_admin(request, session):
    """(user, None) if admin, else (user, response) to return. Keeps the routes terse."""
    user = current_user(request, session)
    if not is_admin(user):
        return user, _forbidden()
    return user, None


def _parse_dt(v):
    v = (v or "").strip()
    if not v:
        return None
    for fmt in ("%Y-%m-%dT%H:%M", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            return datetime.strptime(v, fmt)
        except ValueError:
            continue
    return None


def _ops_overview(session):
    """Counts for the operations dashboard — every actionable operational queue, computed server-side."""
    def c(model, *w):
        stmt = select(func.count()).select_from(model)
        for cond in w:
            stmt = stmt.where(cond)
        return session.exec(stmt).one()
    now = datetime.utcnow()
    return {
        "active_cases": c(OperationCase, OperationCase.status.in_(("open", "in_progress"))),
        "shipments_planning": c(Shipment, Shipment.current_milestone == "planning", Shipment.status == "active"),
        "offers_awaiting": c(FreightRequest, FreightRequest.status == "quoting"),
        "bookings_awaiting": c(Shipment, Shipment.current_milestone == "planning",
                               Shipment.freight_offer_id != None),  # noqa: E711
        "in_transit": c(Shipment, Shipment.current_milestone == "in_transit"),
        "customs_due": c(CustomsCase, CustomsCase.status.in_(("documents_required", "submitted", "query_hold"))),
        "docs_missing": c(DocumentRequirement, DocumentRequirement.status.in_(("missing", "requested"))),
        "payments_awaiting": c(PaymentMilestone, PaymentMilestone.status.in_(("awaiting", "partially_received"))),
        "payments_overdue": c(PaymentMilestone, PaymentMilestone.status.in_(("planned", "awaiting")),
                              PaymentMilestone.due_date != None, PaymentMilestone.due_date < now),  # noqa: E711
        "exceptions_open": c(OperationalException, OperationalException.status.in_(OPSX.OPEN_STATUSES)),
        "deliveries_awaiting": c(Shipment, Shipment.current_milestone == "import_cleared",
                                 Shipment.status == "active"),
        "settlements_awaiting": c(Deal, Deal.stage == "delivered"),
        "remittance_active": c(RemittanceCase, RemittanceCase.status.notin_(
            ("confirmed", "cancelled", "rejected", "failed"))),
    }


def _ops_analytics(session):
    """Basic operational analytics — counts + per-currency money (currencies are NEVER summed together without
    an explicit FX snapshot). No invented provider ratings or transit averages where evidence is insufficient."""
    ships = session.exec(select(Shipment)).all()
    by_mode, by_milestone = {}, {}
    transit_days = []
    for s in ships:
        by_mode[s.mode or "—"] = by_mode.get(s.mode or "—", 0) + 1
        by_milestone[s.current_milestone] = by_milestone.get(s.current_milestone, 0) + 1
        if s.actual_departure and s.actual_arrival and s.actual_arrival >= s.actual_departure:
            transit_days.append((s.actual_arrival - s.actual_departure).days)
    pays = session.exec(select(PaymentMilestone)).all()
    received_by_ccy, due_by_ccy = {}, {}
    for p in pays:
        if p.status in ("received", "partially_received"):
            received_by_ccy[p.currency] = received_by_ccy.get(p.currency, Decimal("0")) + PRICING._d(p.confirmed_amount)
        if p.status in ("planned", "awaiting", "partially_received"):
            due_by_ccy[p.currency] = due_by_ccy.get(p.currency, Decimal("0")) + PRICING._d(p.expected_amount)
    excs = session.exec(select(OperationalException)).all()
    open_exc = sum(1 for e in excs if e.status in OPSX.OPEN_STATUSES)
    return {
        "ship_total": len(ships),
        "by_mode": by_mode,
        "by_milestone": by_milestone,
        # only report an average when real departure+arrival data exists — never invented
        "avg_transit_days": round(sum(transit_days) / len(transit_days), 1) if transit_days else None,
        "exception_rate": round(open_exc / len(ships) * 100, 1) if ships else 0.0,
        "received_by_ccy": {k: str(PRICING._q(v)) for k, v in received_by_ccy.items()},
        "due_by_ccy": {k: str(PRICING._q(v)) for k, v in due_by_ccy.items()},
        "remittance_by_status": _count_by(session, RemittanceCase, RemittanceCase.status),
        "delivered": sum(1 for d in session.exec(select(Deal)).all() if d.stage == "delivered"),
        "settled": session.exec(select(func.count()).select_from(Settlement)).one(),
    }


def _count_by(session, model, col):
    out = {}
    for v in session.exec(select(col)).all():
        out[v or "—"] = out.get(v or "—", 0) + 1
    return out


@app.get("/operations", response_class=HTMLResponse)
def operations_overview(request: Request):
    with Session(engine) as session:
        user, deny = _ops_admin(request, session)
        if deny:
            return deny
        counts = _ops_overview(session)
        analytics = _ops_analytics(session)
        recent = session.exec(select(OperationCase).order_by(OperationCase.id.desc()).limit(20)).all()
    return templates.TemplateResponse("operations_overview.html", {
        "request": request, "user": user, "counts": counts, "analytics": analytics, "recent": recent})


@app.get("/operations/freight", response_class=HTMLResponse)
def operations_freight(request: Request, q: str = "", status: str = "", mode: str = "",
                       origin: str = "", dest: str = "", page: int = 1):
    per = 50
    with Session(engine) as session:
        user, deny = _ops_admin(request, session)
        if deny:
            return deny
        stmt = select(FreightRequest)
        if q:
            stmt = stmt.where(FreightRequest.reference.ilike(f"%{q.strip()}%"))
        if status:
            stmt = stmt.where(FreightRequest.status == status)
        if mode:
            stmt = stmt.where(FreightRequest.mode == mode)
        if origin:
            stmt = stmt.where(FreightRequest.origin_country == origin.upper())
        if dest:
            stmt = stmt.where(FreightRequest.dest_country == dest.upper())
        total = session.exec(select(func.count()).select_from(stmt.subquery())).one()
        pages = max(1, (total + per - 1) // per)
        page = min(max(1, page), pages)
        reqs = session.exec(stmt.order_by(FreightRequest.id.desc())
                            .offset((page - 1) * per).limit(per)).all()
        fr_ids = {r.id for r in reqs} or {0}
        offers = {}
        for o in session.exec(select(FreightOffer).where(FreightOffer.freight_request_id.in_(fr_ids))).all():
            offers.setdefault(o.freight_request_id, []).append(o)
    return templates.TemplateResponse("operations_freight.html", {
        "request": request, "user": user, "reqs": reqs, "offers": offers, "total": total,
        "page": page, "pages": pages, "f": {"q": q, "status": status, "mode": mode,
        "origin": origin, "dest": dest}})


@app.post("/operations/freight")
def operations_freight_create(request: Request, cargo_description: str = Form(""), quantity: str = Form("0"),
                              unit: str = Form(""), mode: str = Form(""), origin_country: str = Form(""),
                              dest_country: str = Form(""), incoterm: str = Form(""),
                              gross_weight_kg: str = Form(""), volume_cbm: str = Form(""),
                              hazardous: str = Form(""), customs_required: str = Form(""),
                              deal_id: str = Form(""), operation_case_id: str = Form("")):
    with Session(engine) as session:
        user, deny = _ops_admin(request, session)
        if deny:
            return deny
        case = session.get(OperationCase, int(operation_case_id)) if operation_case_id.strip() else None
        fields = dict(cargo_description=cargo_description, quantity=quantity or "0", unit=unit, mode=mode,
                      origin_country=origin_country.upper(), dest_country=dest_country.upper(),
                      incoterm=incoterm.upper(), tenant_id=(case.tenant_id if case else None),
                      deal_id=int(deal_id) if deal_id.strip() else None)
        # unknowns stay NULL (never guessed) unless explicitly answered
        if gross_weight_kg.strip():
            fields["gross_weight_kg"] = gross_weight_kg
        if volume_cbm.strip():
            fields["volume_cbm"] = volume_cbm
        if hazardous in ("yes", "no"):
            fields["hazardous"] = (hazardous == "yes")
        if customs_required in ("yes", "no"):
            fields["customs_required"] = (customs_required == "yes")
        fr, _missing = FREIGHT.create_freight_request(session, case=case, actor=user, **fields)
        session.commit()
    return RedirectResponse("/operations/freight", status_code=303)


@app.post("/operations/freight/{fr_id}/offers")
def operations_add_offer(request: Request, fr_id: int, provider_company_id: str = Form(""),
                         provider_name_cache: str = Form(""), mode: str = Form(""),
                         route_summary: str = Form(""), currency: str = Form(""),
                         base_freight: str = Form("0"), surcharges: str = Form("0"),
                         insurance_cost: str = Form("0"), customs_cost: str = Form("0"),
                         lead_time_days: str = Form(""), valid_until: str = Form("")):
    with Session(engine) as session:
        user, deny = _ops_admin(request, session)
        if deny:
            return deny
        fr = session.get(FreightRequest, fr_id)
        if not fr:
            return _not_found()
        FREIGHT.add_offer(session, fr, actor=user,
                          provider_company_id=int(provider_company_id) if provider_company_id.strip() else None,
                          provider_name_cache=provider_name_cache, mode=mode, route_summary=route_summary,
                          currency=currency.upper(), base_freight=base_freight, surcharges=surcharges,
                          insurance_cost=insurance_cost, customs_cost=customs_cost,
                          lead_time_days=int(lead_time_days) if lead_time_days.strip() else None,
                          valid_until=_parse_dt(valid_until))
        session.commit()
    return RedirectResponse("/operations/freight", status_code=303)


@app.post("/operations/offers/{offer_id}/select")
def operations_select_offer(request: Request, offer_id: int, reason: str = Form("")):
    with Session(engine) as session:
        user, deny = _ops_admin(request, session)
        if deny:
            return deny
        offer = session.get(FreightOffer, offer_id)
        if not offer:
            return _not_found()
        ok, err = FREIGHT.select_offer(session, offer, actor=user, reason=reason)
        session.commit()
    return RedirectResponse(f"/operations/freight?err={'' if ok else 'expired'}", status_code=303)


@app.get("/operations/shipments", response_class=HTMLResponse)
def operations_shipments(request: Request, q: str = "", milestone: str = "", mode: str = "", page: int = 1):
    per = 50
    with Session(engine) as session:
        user, deny = _ops_admin(request, session)
        if deny:
            return deny
        stmt = select(Shipment)
        if q:
            stmt = stmt.where(Shipment.reference.ilike(f"%{q.strip()}%"))
        if milestone:
            stmt = stmt.where(Shipment.current_milestone == milestone)
        if mode:
            stmt = stmt.where(Shipment.mode == mode)
        total = session.exec(select(func.count()).select_from(stmt.subquery())).one()
        pages = max(1, (total + per - 1) // per)
        page = min(max(1, page), pages)
        ships = session.exec(stmt.order_by(Shipment.id.desc()).offset((page - 1) * per).limit(per)).all()
    return templates.TemplateResponse("operations_shipments.html", {
        "request": request, "user": user, "ships": ships, "total": total, "page": page, "pages": pages,
        "f": {"q": q, "milestone": milestone, "mode": mode}})


@app.get("/operations/shipments/{sh_id}", response_class=HTMLResponse)
def operations_shipment_detail(request: Request, sh_id: int):
    with Session(engine) as session:
        user, deny = _ops_admin(request, session)
        if deny:
            return deny
        sh = session.get(Shipment, sh_id)
        if not sh:
            return _not_found()
        legs = session.exec(select(ShipmentLeg).where(ShipmentLeg.shipment_id == sh_id)
                            .order_by(ShipmentLeg.sequence)).all()
        events = session.exec(select(ShipmentEvent).where(ShipmentEvent.shipment_id == sh_id)
                             .order_by(ShipmentEvent.id.desc())).all()
    return templates.TemplateResponse("operations_shipment_detail.html", {
        "request": request, "user": user, "sh": sh, "legs": legs, "events": events})


@app.post("/operations/shipments")
def operations_book_shipment(request: Request, mode: str = Form(""), origin: str = Form(""),
                             destination: str = Form(""), booking_reference: str = Form(""),
                             deal_id: str = Form(""), operation_case_id: str = Form(""),
                             cargo_summary: str = Form("")):
    with Session(engine) as session:
        user, deny = _ops_admin(request, session)
        if deny:
            return deny
        case = session.get(OperationCase, int(operation_case_id)) if operation_case_id.strip() else None
        sh = SHIP.book_shipment(session, case=case, actor=user, mode=mode, origin=origin,
                                destination=destination, booking_reference=booking_reference,
                                cargo_summary=cargo_summary,
                                deal_id=int(deal_id) if deal_id.strip() else None)
        session.commit()
        sid = sh.id
    return RedirectResponse(f"/operations/shipments/{sid}", status_code=303)


@app.post("/operations/shipments/{sh_id}/legs")
def operations_add_leg(request: Request, sh_id: int, mode: str = Form(""), origin: str = Form(""),
                       destination: str = Form(""), sequence: str = Form("")):
    with Session(engine) as session:
        user, deny = _ops_admin(request, session)
        if deny:
            return deny
        sh = session.get(Shipment, sh_id)
        if not sh:
            return _not_found()
        kw = dict(mode=mode, origin=origin, destination=destination)
        if sequence.strip().isdigit():
            kw["sequence"] = int(sequence)
        SHIP.add_leg(session, sh, actor=user, **kw)
        session.commit()
    return RedirectResponse(f"/operations/shipments/{sh_id}", status_code=303)


@app.post("/operations/shipments/{sh_id}/events")
def operations_record_event(request: Request, sh_id: int, event_type: str = Form(""),
                            location: str = Form(""), event_at: str = Form(""),
                            seller_safe_summary: str = Form(""), admin_note: str = Form("")):
    with Session(engine) as session:
        user, deny = _ops_admin(request, session)
        if deny:
            return deny
        sh = session.get(Shipment, sh_id)
        if not sh:
            return _not_found()
        SHIP.record_event(session, sh, event_type=event_type, location=location,
                          event_at=_parse_dt(event_at), seller_safe_summary=seller_safe_summary,
                          admin_note=admin_note, source="manual", actor=user)
        # a verified operational event may advance the deal journey
        if sh.deal_id:
            deal = session.get(Deal, sh.deal_id)
            if deal:
                OPS.project_deal_stage(session, deal, actor=user)
        session.commit()
    return RedirectResponse(f"/operations/shipments/{sh_id}", status_code=303)


@app.post("/operations/shipments/{sh_id}/deliver")
def operations_confirm_delivery(request: Request, sh_id: int, source: str = Form("admin"),
                                recipient_role: str = Form(""), condition_notes: str = Form(""),
                                has_damage: str = Form(""), has_shortage: str = Form(""),
                                failed: str = Form("")):
    with Session(engine) as session:
        user, deny = _ops_admin(request, session)
        if deny:
            return deny
        sh = session.get(Shipment, sh_id)
        if not sh:
            return _not_found()
        SHIP.confirm_delivery(session, sh, source=source, recipient_role=recipient_role,
                              condition_notes=condition_notes, has_damage=(has_damage == "1"),
                              has_shortage=(has_shortage == "1"), failed=(failed == "1"), actor=user)
        if sh.deal_id:
            deal = session.get(Deal, sh.deal_id)
            if deal:
                OPS.project_deal_stage(session, deal, actor=user)
        session.commit()
    return RedirectResponse(f"/operations/shipments/{sh_id}", status_code=303)


@app.post("/operations/customs")
def operations_create_customs(request: Request, side: str = Form("export"), country: str = Form(""),
                              shipment_id: str = Form(""), deal_id: str = Form(""),
                              operation_case_id: str = Form("")):
    with Session(engine) as session:
        user, deny = _ops_admin(request, session)
        if deny:
            return deny
        case = session.get(OperationCase, int(operation_case_id)) if operation_case_id.strip() else None
        CUSTOMS.create_customs_case(session, case=case, side=side, country=country.upper(), actor=user,
                                    shipment_id=int(shipment_id) if shipment_id.strip() else None,
                                    deal_id=int(deal_id) if deal_id.strip() else None)
        session.commit()
    return RedirectResponse("/operations", status_code=303)


@app.post("/operations/customs/{cc_id}/status")
def operations_customs_status(request: Request, cc_id: int, to_status: str = Form(""),
                              reason: str = Form("")):
    with Session(engine) as session:
        user, deny = _ops_admin(request, session)
        if deny:
            return deny
        cc = session.get(CustomsCase, cc_id)
        if not cc:
            return _not_found()
        CUSTOMS.set_customs_status(session, cc, to_status, reason=reason, actor=user)
        session.commit()
    return RedirectResponse("/operations", status_code=303)


@app.get("/operations/documentation", response_class=HTMLResponse)
def operations_documentation(request: Request, status: str = "", doc_type: str = "", page: int = 1):
    per = 50
    with Session(engine) as session:
        user, deny = _ops_admin(request, session)
        if deny:
            return deny
        stmt = select(DocumentRequirement)
        if status:
            stmt = stmt.where(DocumentRequirement.status == status)
        if doc_type:
            stmt = stmt.where(DocumentRequirement.doc_type == doc_type)
        total = session.exec(select(func.count()).select_from(stmt.subquery())).one()
        pages = max(1, (total + per - 1) // per)
        page = min(max(1, page), pages)
        reqs = session.exec(stmt.order_by(DocumentRequirement.id.desc())
                            .offset((page - 1) * per).limit(per)).all()
        docs = session.exec(select(TradeDocument).where(TradeDocument.status == "active")
                           .order_by(TradeDocument.id.desc()).limit(50)).all()
    return templates.TemplateResponse("operations_documentation.html", {
        "request": request, "user": user, "reqs": reqs, "docs": docs, "doc_types": TDOCS.DOC_TYPES,
        "av_configured": TDOCS.av_configured(), "scan_status": TDOCS.scan_status,
        "total": total, "page": page, "pages": pages, "f": {"status": status, "doc_type": doc_type}})


@app.post("/operations/documentation/requirements")
def operations_create_requirement(request: Request, doc_type: str = Form(""),
                                  required_from: str = Form("seller"), due_date: str = Form(""),
                                  operation_case_id: str = Form(""), deal_id: str = Form(""),
                                  request_id: str = Form(""), tenant_id: str = Form("")):
    with Session(engine) as session:
        user, deny = _ops_admin(request, session)
        if deny:
            return deny
        TDOCS.create_requirement(
            session, doc_type=doc_type, required_from=required_from, due_date=_parse_dt(due_date),
            operation_case_id=int(operation_case_id) if operation_case_id.strip() else None,
            deal_id=int(deal_id) if deal_id.strip() else None,
            request_id=int(request_id) if request_id.strip() else None,
            tenant_id=int(tenant_id) if tenant_id.strip() else None, actor=user)
        session.commit()
    return RedirectResponse("/operations/documentation", status_code=303)


@app.post("/operations/documentation/upload")
async def operations_upload_document(request: Request, doc_type: str = Form(""),
                                     requirement_id: str = Form(""), operation_case_id: str = Form(""),
                                     tenant_id: str = Form(""), file: UploadFile = File(...)):
    with Session(engine) as session:
        user, deny = _ops_admin(request, session)
        if deny:
            return deny
        data = await file.read(ATT.MAX_BYTES + 1)
        if len(data) > ATT.MAX_BYTES:
            return HTMLResponse("File too large", 400)
        req = session.get(DocumentRequirement, int(requirement_id)) if requirement_id.strip() else None
        doc, err = TDOCS.store_document(
            session, files_dir=OPERATION_FILES_DIR, data=data, original_filename=file.filename or "file",
            content_type=file.content_type or "", doc_type=doc_type, uploaded_by_role="admin",
            requirement=req, operation_case_id=int(operation_case_id) if operation_case_id.strip() else None,
            tenant_id=int(tenant_id) if tenant_id.strip() else None, actor=user)
        session.commit()
        if err:
            return HTMLResponse(f"Rejected: {err}", 400)
    return RedirectResponse("/operations/documentation", status_code=303)


@app.post("/operations/documents/{doc_id}/attest")
def operations_attest_document(request: Request, doc_id: int):
    """Admin ATTESTATION (human review) — releases a document from quarantine. This is NOT a malware scan and is
    never presented as one; a real AV scan uses a configured provider (tradedocs.record_scan)."""
    with Session(engine) as session:
        user, deny = _ops_admin(request, session)
        if deny:
            return deny
        doc = session.get(TradeDocument, doc_id)
        if not doc:
            return _not_found()
        _doc, err = TDOCS.admin_attest(session, doc, actor=user)
        session.commit()
        if err:
            return HTMLResponse(f"Cannot attest: {err}", 400)
    return RedirectResponse("/operations/documentation", status_code=303)


@app.get("/operations/documents/{doc_id}/download")
def operations_download_document(request: Request, doc_id: int):
    with Session(engine) as session:
        user, deny = _ops_admin(request, session)
        if deny:
            return deny                                    # admin-only download (non-admin already forbidden)
        doc = session.get(TradeDocument, doc_id)
        if not doc or not doc.file_path:
            return _not_found()
        path = (OPERATION_FILES_DIR / doc.file_path).resolve()
        if not str(path).startswith(str(OPERATION_FILES_DIR.resolve()) + os.sep) or not path.exists():
            return _not_found()
        return FileResponse(str(path), media_type=doc.content_type or "application/octet-stream",
                            filename=doc.original_filename or path.name,
                            headers={"Cache-Control": "no-store", "X-Robots-Tag": "noindex"})


@app.post("/operations/documentation/requirements/{req_id}/request-seller")
def operations_request_seller_doc(request: Request, req_id: int, instructions: str = Form(""),
                                  due_date: str = Form("")):
    with Session(engine) as session:
        user, deny = _ops_admin(request, session)
        if deny:
            return deny
        req = session.get(DocumentRequirement, req_id)
        if not req:
            return _not_found()
        ok, err = TDOCS.request_seller_document(session, req, instructions=instructions,
                                                due_date=_parse_dt(due_date), actor=user)
        session.commit()
        if not ok:
            return HTMLResponse(f"Not sent: {err}", 400)
    return RedirectResponse("/operations/documentation", status_code=303)


@app.post("/operations/cases")
def operations_create_case(request: Request, deal_id: str = Form(""), request_id: str = Form(""),
                           category: str = Form(""), origin_country: str = Form(""),
                           dest_country: str = Form("")):
    with Session(engine) as session:
        user, deny = _ops_admin(request, session)
        if deny:
            return deny
        if deal_id.strip():
            deal = session.get(Deal, int(deal_id))
            if not deal:
                return _not_found()
            OPS.ensure_case_for_deal(session, deal, actor=user)
        elif request_id.strip():
            req = session.get(ServiceRequest, int(request_id))
            if not req:
                return _not_found()
            OPS.ensure_case_for_request(session, req, actor=user)
        else:
            OPS.create_standalone_case(session, actor=user, category=category,
                                       origin_country=origin_country.upper(),
                                       dest_country=dest_country.upper())
        session.commit()
    return RedirectResponse("/operations", status_code=303)


@app.post("/operations/deals/{deal_id}/project")
def operations_project_deal(request: Request, deal_id: int):
    with Session(engine) as session:
        user, deny = _ops_admin(request, session)
        if deny:
            return deny
        deal = session.get(Deal, deal_id)
        if not deal:
            return _not_found()
        OPS.project_deal_stage(session, deal, actor=user)
        session.commit()
    return RedirectResponse(f"/deals/{deal_id}", status_code=303)


@app.get("/operations/payments", response_class=HTMLResponse)
def operations_payments(request: Request, status: str = "", kind: str = "", page: int = 1):
    per = 50
    with Session(engine) as session:
        user, deny = _ops_admin(request, session)
        if deny:
            return deny
        stmt = select(PaymentMilestone)
        if status:
            stmt = stmt.where(PaymentMilestone.status == status)
        if kind:
            stmt = stmt.where(PaymentMilestone.milestone_type == kind)
        total = session.exec(select(func.count()).select_from(stmt.subquery())).one()
        pages = max(1, (total + per - 1) // per)
        page = min(max(1, page), pages)
        pays = session.exec(stmt.order_by(PaymentMilestone.id.desc())
                            .offset((page - 1) * per).limit(per)).all()
        remits = session.exec(select(RemittanceCase).order_by(RemittanceCase.id.desc()).limit(50)).all()
        # value strictly PER CURRENCY — never summed across currencies without an explicit FX snapshot
        due_by_ccy, received_by_ccy = {}, {}
        for p in session.exec(select(PaymentMilestone)).all():
            if p.status in ("planned", "awaiting", "partially_received"):
                due_by_ccy[p.currency] = due_by_ccy.get(p.currency, Decimal("0")) + PRICING._d(p.expected_amount)
            if p.status in ("received", "partially_received"):
                received_by_ccy[p.currency] = received_by_ccy.get(p.currency, Decimal("0")) + PRICING._d(p.confirmed_amount)
    return templates.TemplateResponse("operations_payments.html", {
        "request": request, "user": user, "pays": pays, "remits": remits,
        "due_by_ccy": {k: str(PRICING._q(v)) for k, v in due_by_ccy.items()},
        "received_by_ccy": {k: str(PRICING._q(v)) for k, v in received_by_ccy.items()},
        "remit_configured": OPSPROV.remittance_status()["configured"],
        "total": total, "page": page, "pages": pages, "f": {"status": status, "kind": kind}})


@app.get("/operations/exceptions", response_class=HTMLResponse)
def operations_exceptions(request: Request, status: str = "", severity: str = "", page: int = 1):
    per = 50
    with Session(engine) as session:
        user, deny = _ops_admin(request, session)
        if deny:
            return deny
        stmt = select(OperationalException)
        if status:
            stmt = stmt.where(OperationalException.status == status)
        else:
            stmt = stmt.where(OperationalException.status.in_(OPSX.OPEN_STATUSES))
        if severity:
            stmt = stmt.where(OperationalException.severity == severity)
        total = session.exec(select(func.count()).select_from(stmt.subquery())).one()
        pages = max(1, (total + per - 1) // per)
        page = min(max(1, page), pages)
        excs = session.exec(stmt.order_by(OperationalException.id.desc())
                            .offset((page - 1) * per).limit(per)).all()
    return templates.TemplateResponse("operations_exceptions.html", {
        "request": request, "user": user, "excs": excs, "severities": OPSX.SEVERITIES,
        "total": total, "page": page, "pages": pages, "f": {"status": status, "severity": severity}})


@app.post("/operations/payments")
def operations_create_payment(request: Request, milestone_type: str = Form(""), currency: str = Form(""),
                              expected_amount: str = Form("0"), deal_id: str = Form(""),
                              due_date: str = Form(""), payer_role: str = Form(""),
                              payee_role: str = Form(""), seller_visible: str = Form("")):
    with Session(engine) as session:
        user, deny = _ops_admin(request, session)
        if deny:
            return deny
        PAY.create_milestone(session, milestone_type=milestone_type, currency=currency,
                             expected_amount=expected_amount,
                             deal_id=int(deal_id) if deal_id.strip() else None, due_date=_parse_dt(due_date),
                             payer_role=payer_role, payee_role=payee_role,
                             seller_visible=(seller_visible == "1"), actor=user)
        session.commit()
    return RedirectResponse("/operations/payments", status_code=303)


@app.post("/operations/payments/{pm_id}/confirm")
def operations_confirm_payment(request: Request, pm_id: int, confirmed_amount: str = Form("0"),
                               reference_code: str = Form(""), evidence_document_id: str = Form("")):
    with Session(engine) as session:
        user, deny = _ops_admin(request, session)
        if deny:
            return deny
        if not authz.has_permission(session, user, "payment.confirm"):   # confirming money is high-risk
            return _forbidden()
        pm = session.get(PaymentMilestone, pm_id)
        if not pm:
            return _not_found()
        ok, err = PAY.confirm_payment(session, pm, confirmed_amount=confirmed_amount,
                                      reference_code=reference_code,
                                      evidence_document_id=int(evidence_document_id) if evidence_document_id.strip() else None,
                                      actor=user)
        if ok and pm.deal_id:
            deal = session.get(Deal, pm.deal_id)
            if deal:
                OPS.project_deal_stage(session, deal, actor=user)
        session.commit()
        if not ok:
            return HTMLResponse(f"Not confirmed: {err}", 400)
    return RedirectResponse("/operations/payments", status_code=303)


@app.post("/operations/remittance")
def operations_create_remittance(request: Request, source_currency: str = Form(""),
                                 dest_currency: str = Form(""), source_amount: str = Form("0"),
                                 route_method_category: str = Form(""), deal_id: str = Form(""),
                                 tenant_id: str = Form("")):
    with Session(engine) as session:
        user, deny = _ops_admin(request, session)
        if deny:
            return deny
        REMIT.create_remittance(session, source_currency=source_currency, dest_currency=dest_currency,
                                source_amount=source_amount, route_method_category=route_method_category,
                                deal_id=int(deal_id) if deal_id.strip() else None,
                                tenant_id=int(tenant_id) if tenant_id.strip() else None, actor=user)
        session.commit()
    return RedirectResponse("/operations/payments", status_code=303)


@app.post("/operations/remittance/{rc_id}/status")
def operations_remittance_status(request: Request, rc_id: int, to_status: str = Form(""),
                                 compliance_reason: str = Form(""), reason: str = Form("")):
    with Session(engine) as session:
        user, deny = _ops_admin(request, session)
        if deny:
            return deny
        rc = session.get(RemittanceCase, rc_id)
        if not rc:
            return _not_found()
        REMIT.set_status(session, rc, to_status, compliance_reason=compliance_reason, reason=reason, actor=user)
        session.commit()
    return RedirectResponse("/operations/payments", status_code=303)


@app.post("/operations/deals/{deal_id}/settle")
def operations_settle_deal(request: Request, deal_id: int, revenue: str = Form("0"),
                           verified_costs: str = Form("0"), supplier_proceeds: str = Form("0"),
                           operational_costs: str = Form("0"), currency: str = Form(""),
                           force: str = Form("")):
    with Session(engine) as session:
        user, deny = _ops_admin(request, session)
        if deny:
            return deny
        if not role_at_least(user, "manager"):
            return _forbidden()                            # settlement is manager+ only
        deal = session.get(Deal, deal_id)
        if not deal:
            return _not_found()
        st, err = PAY.record_settlement(session, deal, revenue=revenue, verified_costs=verified_costs,
                                        supplier_proceeds=supplier_proceeds,
                                        operational_costs=operational_costs, currency=currency,
                                        force=(force == "1"), actor=user)
        session.commit()
        if err:
            return HTMLResponse(f"Not settled: {err}", 400)
    return RedirectResponse(f"/deals/{deal_id}", status_code=303)


@app.post("/operations/exceptions")
def operations_create_exception(request: Request, exc_type: str = Form(""), severity: str = Form("medium"),
                                internal_description: str = Form(""),
                                seller_safe_description: str = Form(""), deal_id: str = Form(""),
                                shipment_id: str = Form("")):
    with Session(engine) as session:
        user, deny = _ops_admin(request, session)
        if deny:
            return deny
        sh = session.get(Shipment, int(shipment_id)) if shipment_id.strip() else None
        OPSX.raise_exception(session, exc_type=exc_type, severity=severity, actor=user,
                             internal_description=internal_description,
                             seller_safe_description=seller_safe_description,
                             deal_id=int(deal_id) if deal_id.strip() else None,
                             shipment_id=sh.id if sh else None,
                             tenant_id=(sh.tenant_id if sh else None))
        session.commit()
    return RedirectResponse("/operations/exceptions", status_code=303)


@app.post("/operations/exceptions/{exc_id}/resolve")
def operations_resolve_exception(request: Request, exc_id: int, resolution: str = Form(""),
                                 status: str = Form("resolved")):
    with Session(engine) as session:
        user, deny = _ops_admin(request, session)
        if deny:
            return deny
        exc = session.get(OperationalException, exc_id)
        if not exc:
            return _not_found()
        OPSX.resolve_exception(session, exc, resolution=resolution, status=status, actor=user)
        session.commit()
    return RedirectResponse("/operations/exceptions", status_code=303)


@app.post("/ops/webhook/tracking")
async def ops_webhook_tracking(request: Request, x_signature: str = Header(""),
                               x_delivery_id: str = Header("")):
    """Inbound carrier-tracking webhook. Signature-verified (HMAC) + replay-protected + rate-limited. With NO
    provider configured (no OPS_WEBHOOK_SECRET) every call is rejected — there is no unauthenticated path in."""
    if not RL.allow(RL.client_key(request, "opswh"), limit=60, window=60):
        return JSONResponse({"ok": False}, status_code=429)
    secret = OPSPROV.webhook_secret()
    raw = await request.body()
    if not OPSPROV.verify_webhook(secret, raw, x_signature):
        return JSONResponse({"ok": False, "error": "unverified"}, status_code=401)
    if OPSPROV.replay_seen(x_delivery_id):
        return JSONResponse({"ok": False, "error": "replay"}, status_code=409)
    # a configured provider would parse `raw` and call shipments.record_event(source="webhook:<provider>", ...);
    # that ingestion is idempotent on (source, external_event_id). No provider is configured in this build.
    return JSONResponse({"ok": True})


# ============================================================================= INTELLIGENCE (Phase 8)
# Admin-only analytics/demand/opportunity/reports. Every route is is_admin-gated; GETs never mutate business
# data; no buyer identity / contact / internal pricing / provider / margin ever reaches a chart, label or export.

def _intel_window(days: str):
    """A bounded (since, until) window from a `days` query param (default 30, capped 365). Open-ended only when
    days == 'all'."""
    if (days or "").strip() == "all":
        return None, None, "all-time"
    try:
        d = min(365, max(1, int(days or "30")))
    except ValueError:
        d = 30
    until = datetime.utcnow()
    return until - timedelta(days=d), until, f"{d}d"


@app.get("/intelligence", response_class=HTMLResponse)
def intelligence_overview(request: Request, days: str = "30"):
    with Session(engine) as session:
        user, deny = _ops_admin(request, session)
        if deny:
            return deny
        since, until, rng = _intel_window(days)
        kpis = ANALYTICS.dashboard_kpis(session, since=since, until=until)
        fnl = ANALYTICS.funnel(session, since=since, until=until)
        replies = ANALYTICS.replies_by_outcome(session, since=since, until=until)
        sources = DATASRC.source_health(session)
        stale_sources = [s for s in sources if s["freshness"] in ("Stale", "Failed")]
        # evidence-based rankings (populated once demand signals exist; honest "insufficient" until then)
        demand_by_product = {k: v for k, v in session.exec(
            select(DemandSignal.product, func.count()).where(DemandSignal.product != "")
            .group_by(DemandSignal.product).order_by(func.count().desc()).limit(8)).all()}
        demand_by_market = {k: v for k, v in session.exec(
            select(DemandSignal.dest_country, func.count()).where(DemandSignal.dest_country != "")
            .group_by(DemandSignal.dest_country).order_by(func.count().desc()).limit(8)).all()}
        opp_open = session.exec(select(func.count()).select_from(Opportunity)
                                .where(Opportunity.status.notin_(("archived", "rejected", "converted")))).one()
    return templates.TemplateResponse("intelligence_overview.html", {
        "request": request, "user": user, "range": rng, "days": days, "kpis": kpis, "funnel": fnl,
        "funnel_rows": CHARTS.funnel_rows(fnl["stages"]), "replies": replies,
        "reply_bars": CHARTS.bar_rows(replies), "sources": sources, "stale_sources": stale_sources,
        "demand_product_bars": CHARTS.bar_rows(demand_by_product),
        "demand_market_bars": CHARTS.bar_rows(demand_by_market), "opp_open": opp_open})


@app.get("/intelligence/sources", response_class=HTMLResponse)
def intelligence_sources(request: Request):
    with Session(engine) as session:
        user, deny = _ops_admin(request, session)
        if deny:
            return deny
        sources = DATASRC.source_health(session)
    return templates.TemplateResponse("intelligence_sources.html", {
        "request": request, "user": user, "sources": sources})


def _performance_rows(session, *, min_activity=5):
    """Quality-weighted per-user performance. Rewards completed work, positive commercial outcomes, resolved
    exceptions and response time — NEVER raw email/lead volume. Users below the minimum activity are shown but
    'Not yet ranked'."""
    users = session.exec(select(User).order_by(User.id.asc())).all()

    def by_owner(model, owner_col, *conds):
        stmt = select(owner_col, func.count(model.id))
        for c in conds:
            stmt = stmt.where(c)
        return dict(session.exec(stmt.group_by(owner_col)).all())

    accepted = by_owner(Quote, Quote.owner_id, Quote.status == "accepted")
    deals_delivered = by_owner(Deal, Deal.owner_id, Deal.stage.in_(("delivered", "settled", "closed")))
    completed_wi = dict(session.exec(select(WorkItem.resolved_by, func.count(WorkItem.id))
                        .where(WorkItem.status == "completed").group_by(WorkItem.resolved_by)).all())
    exc_resolved = by_owner(OperationalException, OperationalException.owner_id,
                            OperationalException.status.in_(("resolved", "dismissed")))
    leads_by = by_owner(Lead, Lead.owner_id)
    # avg response hours (first_response_at - created_at) over owned leads with both timestamps
    resp = {}
    for ld in session.exec(select(Lead).where(Lead.first_response_at != None)).all():  # noqa: E711
        if ld.owner_id and ld.created_at and ld.first_response_at >= ld.created_at:
            resp.setdefault(ld.owner_id, []).append((ld.first_response_at - ld.created_at).total_seconds() / 3600.0)
    rows = []
    for u in users:
        acc = accepted.get(u.id, 0); dd = deals_delivered.get(u.id, 0)
        cw = completed_wi.get(u.id, 0); ex = exc_resolved.get(u.id, 0)
        activity = acc + dd + cw + ex + leads_by.get(u.id, 0)
        avg_resp = round(sum(resp[u.id]) / len(resp[u.id]), 1) if resp.get(u.id) else None
        # quality score: outcomes + resolved work, NOT volume. Response-time factor rewards speed when sampled.
        quality = acc * 4 + dd * 5 + cw * 1 + ex * 2
        rows.append({"u": u, "accepted": acc, "deals_delivered": dd, "completed_work": cw,
                     "exceptions_resolved": ex, "avg_response_h": avg_resp, "activity": activity,
                     "quality": quality, "ranked": activity >= min_activity})
    # rank only sufficiently-active users; keep others listed but unranked
    ranked = sorted([r for r in rows if r["ranked"]], key=lambda r: -r["quality"])
    unranked = [r for r in rows if not r["ranked"]]
    return ranked, unranked


@app.get("/intelligence/performance", response_class=HTMLResponse)
def intelligence_performance(request: Request):
    with Session(engine) as session:
        user, deny = _ops_admin(request, session)
        if deny:
            return deny
        ranked, unranked = _performance_rows(session)
    return templates.TemplateResponse("intelligence_performance.html", {
        "request": request, "user": user, "ranked": ranked, "unranked": unranked})


@app.get("/intelligence/demand", response_class=HTMLResponse)
def intelligence_demand(request: Request, period: str = "month", product: str = "", country: str = "",
                        page: int = 1):
    per = 50
    with Session(engine) as session:
        user, deny = _ops_admin(request, session)
        if deny:
            return deny
        stmt = select(DemandSignal)
        if product:
            stmt = stmt.where(DemandSignal.product.ilike(f"%{product.strip()}%"))
        if country:
            stmt = stmt.where(DemandSignal.dest_country == country.upper())
        total = session.exec(select(func.count()).select_from(stmt.subquery())).one()
        pages = max(1, (total + per - 1) // per)
        page = min(max(1, page), pages)
        signals = session.exec(stmt.order_by(DemandSignal.id.desc())
                               .offset((page - 1) * per).limit(per)).all()
        by_product = {k: v for k, v in session.exec(
            select(DemandSignal.product, func.count()).where(DemandSignal.product != "")
            .group_by(DemandSignal.product).order_by(func.count().desc()).limit(10)).all()}
        by_type = {k: v for k, v in session.exec(
            select(DemandSignal.signal_type, func.count()).group_by(DemandSignal.signal_type)).all()}
    return templates.TemplateResponse("intelligence_demand.html", {
        "request": request, "user": user, "signals": signals, "total": total, "page": page, "pages": pages,
        "period": period, "product_bars": CHARTS.bar_rows(by_product), "by_type": by_type,
        "f": {"product": product, "country": country}})


@app.get("/intelligence/opportunities", response_class=HTMLResponse)
def intelligence_opportunities(request: Request, status: str = "", q: str = "", page: int = 1):
    per = 50
    with Session(engine) as session:
        user, deny = _ops_admin(request, session)
        if deny:
            return deny
        stmt = select(Opportunity)
        if status:
            stmt = stmt.where(Opportunity.status == status)
        if q:
            stmt = stmt.where(Opportunity.title.ilike(f"%{q.strip()}%"))
        total = session.exec(select(func.count()).select_from(stmt.subquery())).one()
        pages = max(1, (total + per - 1) // per)
        page = min(max(1, page), pages)
        opps = session.exec(stmt.order_by(Opportunity.score.desc(), Opportunity.id.desc())
                            .offset((page - 1) * per).limit(per)).all()
    return templates.TemplateResponse("intelligence_opportunities.html", {
        "request": request, "user": user, "opps": opps, "total": total, "page": page, "pages": pages,
        "f": {"status": status, "q": q}})


@app.get("/intelligence/reports", response_class=HTMLResponse)
def intelligence_reports(request: Request):
    with Session(engine) as session:
        user, deny = _ops_admin(request, session)
        if deny:
            return deny
        reports = session.exec(select(AnalyticsReport).order_by(AnalyticsReport.id.desc()).limit(50)).all()
    return templates.TemplateResponse("intelligence_reports.html", {
        "request": request, "user": user, "reports": reports})


REPORT_FILES_DIR = BASE_DIR.parent / "report_files"   # PRIVATE, outside /static


@app.get("/intelligence/opportunities/{opp_id}", response_class=HTMLResponse)
def intelligence_opportunity_detail(request: Request, opp_id: int):
    with Session(engine) as session:
        user, deny = _ops_admin(request, session)
        if deny:
            return deny
        opp = session.get(Opportunity, opp_id)
        if not opp:
            return _not_found()
        d = INTEL_OPP.detail(session, opp)
        users = session.exec(select(User).where(User.role.in_(("admin", "manager", "agent")))).all()
    return templates.TemplateResponse("intelligence_opportunity_detail.html", {
        "request": request, "user": user, "opp": opp, "detail": d, "statuses": INTEL_OPP.STATUSES,
        "transitions": INTEL_OPP.TRANSITIONS.get(opp.status, ()), "users": users})


@app.post("/intelligence/opportunities/{opp_id}/status")
def intelligence_opp_status(request: Request, opp_id: int, to_status: str = Form(""), reason: str = Form("")):
    with Session(engine) as session:
        user, deny = _ops_admin(request, session)
        if deny:
            return deny
        opp = session.get(Opportunity, opp_id)
        if not opp:
            return _not_found()
        ok, err = INTEL_OPP.set_status(session, opp, to_status, reason=reason, actor=user)
        session.commit()
        if not ok:
            return HTMLResponse(f"Cannot change status: {err}", 400)
    return RedirectResponse(f"/intelligence/opportunities/{opp_id}", status_code=303)


@app.post("/intelligence/opportunities/{opp_id}/assign")
def intelligence_opp_assign(request: Request, opp_id: int, owner_id: str = Form("")):
    with Session(engine) as session:
        user, deny = _ops_admin(request, session)
        if deny:
            return deny
        opp = session.get(Opportunity, opp_id)
        if not opp:
            return _not_found()
        INTEL_OPP.assign(session, opp, int(owner_id) if owner_id.strip() else None, actor=user)
        session.commit()
    return RedirectResponse(f"/intelligence/opportunities/{opp_id}", status_code=303)


@app.post("/intelligence/opportunities/{opp_id}/rescore")
def intelligence_opp_rescore(request: Request, opp_id: int):
    with Session(engine) as session:
        user, deny = _ops_admin(request, session)
        if deny:
            return deny
        opp = session.get(Opportunity, opp_id)
        if not opp:
            return _not_found()
        INTEL_OPP.match_supply(session, opp, actor=user)
        INTEL_OPP.rescore(session, opp, actor=user)
        session.commit()
    return RedirectResponse(f"/intelligence/opportunities/{opp_id}", status_code=303)


@app.post("/intelligence/refresh")
def intelligence_refresh(request: Request):
    """Regenerate demand signals + opportunities from deterministic evidence (accepted quotes, Deals, confirmed
    positive replies). Idempotent + bounded. Admin action, not a GET."""
    with Session(engine) as session:
        user, deny = _ops_admin(request, session)
        if deny:
            return deny
        WQ.sync_demand_signals(session, actor=user, budget=500)
        session.commit()
    return RedirectResponse("/intelligence/demand", status_code=303)


@app.post("/intelligence/reports")
def intelligence_generate_report(request: Request, report_type: str = Form(""), days: str = Form("30"),
                                 fmt: str = Form("csv")):
    with Session(engine) as session:
        user, deny = _ops_admin(request, session)
        if deny:
            return deny
        since, until, _ = _intel_window(days)
        rpt, err = INTEL_REPORTS.generate(session, report_type=report_type, files_dir=REPORT_FILES_DIR,
                                          since=since, until=until, fmt=fmt, actor=user)
        session.commit()
        if err and (rpt is None or rpt.status == "failed"):
            return HTMLResponse(f"Report generation issue: {err}", 400 if rpt is None else 200)
    return RedirectResponse("/intelligence/reports", status_code=303)


@app.get("/intelligence/reports/{rpt_id}/download")
def intelligence_download_report(request: Request, rpt_id: int):
    with Session(engine) as session:
        user, deny = _ops_admin(request, session)
        if deny:
            return deny                                    # admin-only export (server-side authz)
        rpt = session.get(AnalyticsReport, rpt_id)
        if not rpt or not rpt.file_path or rpt.status != "generated":
            return _not_found()
        path = (REPORT_FILES_DIR / rpt.file_path).resolve()
        if not str(path).startswith(str(REPORT_FILES_DIR.resolve()) + os.sep) or not path.exists():
            return _not_found()
        pipeline.audit(session, user, "analytics_report", rpt.id, "report_downloaded", {})
        session.commit()
        return FileResponse(str(path), media_type=rpt.content_type or "application/octet-stream",
                            filename=f"{rpt.reference}{os.path.splitext(rpt.file_path)[1]}",
                            headers={"Cache-Control": "no-store", "X-Robots-Tag": "noindex"})


@app.get("/intelligence/alerts", response_class=HTMLResponse)
def intelligence_alerts(request: Request):
    with Session(engine) as session:
        user, deny = _ops_admin(request, session)
        if deny:
            return deny
        alerts = INTEL_ALERTS.active_alerts(session)
    return templates.TemplateResponse("intelligence_alerts.html", {
        "request": request, "user": user, "alerts": alerts})


@app.post("/intelligence/alerts/{alert_id}/{action}")
def intelligence_alert_action(request: Request, alert_id: int, action: str):
    with Session(engine) as session:
        user, deny = _ops_admin(request, session)
        if deny:
            return deny
        alert = session.get(IntelAlert, alert_id)
        if not alert or action not in ("reviewed", "dismissed", "snoozed"):
            return _not_found()
        snooze = datetime.utcnow() + timedelta(days=7) if action == "snoozed" else None
        INTEL_ALERTS.set_status(session, alert, action, snooze_until=snooze, actor=user)
        session.commit()
    return RedirectResponse("/intelligence/alerts", status_code=303)


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

# The concierge service catalog. buyer_hunt delivers leads (via scripts/load_managed_buyers.py); the other
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


PRODUCT_FILES_DIR = BASE_DIR.parent / "product_files"   # PRIVATE, outside /static (Phase 5 product docs)


def _save_product_file(product_id, doc_id, upload, data):
    """Persist a validated product file named <doc_id>_<safe_name> under product_files/<product_id>/.
    `data` is already-read + validated bytes. Returns the stored relative path."""
    dest_dir = PRODUCT_FILES_DIR / str(product_id)
    dest_dir.mkdir(parents=True, exist_ok=True)
    fname = f"{doc_id}_{_safe_name(upload.filename)}"
    (dest_dir / fname).write_bytes(data)
    return f"{product_id}/{fname}"


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
        _flash(request, f"Request {sr.tracking_code} sent to admin. You'll see anonymized progress and updates here.")
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
        if not is_admin(user):      # the poll target too: sellers only ever see files the admin marked PII-free
            deliv_map = {rid: [d for d in dvs if d.seller_safe] for rid, dvs in deliv_map.items()}
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
        # buyers per request = the managed funnel's total_prospects (leads_delivered stays the legacy hand-over count)
        managed_counts = (dict(session.exec(select(Lead.request_id, func.count(Lead.id)).where(
            Lead.request_id.in_(ids), Lead.managed == True).group_by(Lead.request_id)).all())  # noqa: E712
            if ids else {})
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
            "managed_counts": managed_counts,
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
        flashes = request.session.pop("_flash", [])
        return templates.TemplateResponse("admin_request_detail.html", {
            "request": request, "user": user, "active": "requests", "sr": sr, "flashes": flashes,
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
        legacy_before = sr.status
        RS.reconcile_legacy(sr, to)   # keep the seller-visible legacy status consistent (never contradict); BEFORE
        # the history row, so it records the legacy status the request really moved to (not done→done)
        ok, err = RS.advance_workflow(session, sr, to, user, reason, legacy_before=legacy_before)
        if ok:
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
            RS.advance_workflow(session, sr, "approved", user,           # keep additive workflow in sync
                                legacy_before="submitted")
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
            legacy_before = sr.status
            sr.status, sr.admin_note, sr.done_at = "rejected", (reason or "").strip()[:500], datetime.utcnow()
            RS.advance_workflow(session, sr, "rejected", user, reason=(reason or "").strip(),
                                legacy_before=legacy_before)
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
            RS.advance_workflow(session, sr, "in_progress", user, legacy_before="approved")
            session.add(sr); session.commit()
    return RedirectResponse("/admin/requests", status_code=303)


@app.post("/admin/requests/{req_id}/done")
def admin_request_done(request: Request, req_id: int, result: str = Form(""),
                       url: str = Form(""), seller_safe: str = Form(""), file: UploadFile = File(None)):
    """Deliver a request, with a result note + optional file/link. Can be called REPEATEDLY (even after
    'done') — each delivery APPENDS a RequestDeliverable, so re-sends stack instead of overwriting. Tick
    seller_safe ONLY when the file carries no buyer PII (else sellers can't download it). Only a seller-safe
    delivery's note/link is copied onto the request card (result/result_url), and a seller-safe note/link that
    carries contact details or names one of the request's buyers is refused."""
    with Session(engine) as session:
        user = current_user(request, session)
        if not is_admin(user):
            return _forbidden()
        sr = session.get(ServiceRequest, req_id)
        if sr and sr.status in ("approved", "running", "done"):
            note = (result or "").strip()[:1000]
            link = (url or "").strip()[:500]
            safe = seller_safe == "1"
            has_file = file is not None and (file.filename or "").strip()
            if safe and (note or link):
                needles = pipeline.request_denylist(session, sr)
                hits = ((pipeline.seller_text_hits(session, sr, note, needles) if note else [])
                        + (pipeline.link_hits(link, needles) if link else []))
                if hits:
                    _flash(request, f"Delivery refused — the seller-safe note/link contains buyer contact details or "
                                    f"identity: {pipeline.hits_summary(hits)}. Remove them, or deliver it as internal "
                                    f"only.", "rose")
                    return RedirectResponse(f"/admin/requests/{req_id}?tab=files", status_code=303)
            if has_file or link or note:
                dv = RequestDeliverable(request_id=sr.id, note=note, url=link, delivered_by=user.email,
                                        seller_safe=safe)
                session.add(dv); session.commit(); session.refresh(dv)
                if has_file:
                    rel = _save_deliverable_file(sr.id, dv.id, file)
                    if rel:
                        dv.file_path, sr.result_file_path = rel, rel
                if link and safe:          # the request card shows result/result_url to the seller
                    sr.result_url = link
                session.add(dv)
            legacy_before = sr.status
            sr.status, sr.done_at = "done", datetime.utcnow()
            if note and safe:
                sr.result = note
            RS.advance_workflow(session, sr, "delivered", user, legacy_before=legacy_before)
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
    """Board of the managed (confidential) buyers for a request — FULL PII, so it requires buyer.pii.view."""
    with Session(engine) as session:
        user = current_user(request, session)
        if not is_admin(user) or not authz.has_permission(session, user, "buyer.pii.view"):
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


def _update_problems(session, sr, anon_ref, *texts):
    """(hits, lead) for a seller update: contact/PII + buyer-denylist hits over every seller-visible field (anon_ref
    included), and the managed buyer anon_ref names — it must be '' (request-level) or a buyer of THIS request."""
    ref = (anon_ref or "").strip()
    needles = pipeline.request_denylist(session, sr)
    # each field on its own: a summary's last word and the next field's first word never read as one buyer name
    hits = [h for x in (ref, *texts) if x for h in pipeline.seller_text_hits(session, sr, x, needles)]
    lead = None
    if ref:
        lead = session.exec(select(Lead).where(Lead.request_id == sr.id, Lead.managed == True,  # noqa: E712
                                               Lead.anon_ref == ref)).first()
        if lead is None:
            hits.append({"kind": "buyer ref", "match": "not a buyer reference of this request"})
    return hits, lead


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
        hits, _lead = _update_problems(session, sr, anon_ref, summary, next_action, seller_question, public_status)
    u = {"anon_ref": anon_ref.strip(), "public_status": public_status.strip(), "summary": summary.strip(),
         "next_action": next_action.strip(), "seller_question": seller_question.strip(),
         "deadline": deadline.strip()}
    return templates.TemplateResponse("publish_update_preview.html", {
        "request": request, "user": user, "sr": sr, "u": u, "hits": hits})


@app.post("/admin/requests/{req_id}/publish")
def admin_publish(request: Request, req_id: int, anon_ref: str = Form(""), public_status: str = Form(""),
                  summary: str = Form(""), next_action: str = Form(""), seller_question: str = Form(""),
                  deadline: str = Form("")):
    """Publish a SANITIZED seller-visible update. Refuses if any contact/PII or buyer identity slips through, or if
    anon_ref is not one of THIS request's buyers (re-checked here — the preview is never trusted)."""
    with Session(engine) as session:
        user = current_user(request, session)
        if not is_admin(user):
            return _forbidden()
        sr = session.get(ServiceRequest, req_id)
        if not sr:
            return _not_found()
        hits, lead = _update_problems(session, sr, anon_ref, summary, next_action, seller_question, public_status)
        if hits:
            _flash(request, f"Blocked — the update still contains contact details / buyer-identifying data, or an "
                            f"unknown buyer reference: {pipeline.hits_summary(hits)}. Remove them and preview again.",
                   "rose")
            return RedirectResponse(f"/admin/requests/{req_id}/pipeline", status_code=303)
        dl = None
        if deadline:
            try:
                dl = datetime.fromisoformat(deadline)
            except ValueError:
                dl = None
        q = seller_question.strip()[:300]
        su = SellerUpdate(request_id=req_id, seller_id=sr.owner_id, anon_ref=anon_ref.strip()[:40],
                          lead_id=lead.id if lead else None,      # resolving its question clears the buyer's flag
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
            hits = pipeline.seller_text_hits(session, sr, text)      # + the request's buyer names/sites/cities
            if hits:
                _flash(request, f"Message blocked - it contains buyer contact details / PII: "
                                f"{pipeline.hits_summary(hits)}. Remove them before sending.", "rose")
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
            hits = pipeline.seller_text_hits(session, sr, answer_text)
            if hits:
                _flash(request, f"Answer blocked - it contains buyer contact details / PII: "
                                f"{pipeline.hits_summary(hits)}. Remove them first.", "rose")
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


_EXCLUDES = ("exclude_customers", "exclude_negative", "exclude_negotiating", "exclude_recent")


def _audience_filter(c, product="", country="", engagement="", flags=()):
    """The audience filter — ALWAYS scoped to the campaign's own request, so a broad (or empty) filter can never
    enrol another seller's / request's buyers."""
    f = {"product": (product or "").strip(), "country": (country or "").strip(),
         "engagement": (engagement or "").strip(), "request_id": c.request_id or ""}
    for k in _EXCLUDES:
        f[k] = k in flags
    return f


@app.get("/campaigns/{cid}", response_class=HTMLResponse)
def campaign_detail(request: Request, cid: int, product: str = "", country: str = "", engagement: str = "",
                    preview: str = "", exclude_customers: str = "", exclude_negative: str = "",
                    exclude_negotiating: str = "", exclude_recent: str = ""):
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
        flags = {k for k, v in zip(_EXCLUDES, (exclude_customers, exclude_negative, exclude_negotiating,
                                               exclude_recent)) if v == "1"}
        f = _audience_filter(c, product, country, engagement, flags)
        want = preview == "1" or product or country or engagement
        aud = CAMP.audience_preview(session, c, f) if want else None
        mailbox = session.get(MailAccount, c.mailbox_id) if c.mailbox_id else None
        mailboxes = session.exec(select(MailAccount).where(MailAccount.admin_owned == True)).all()  # noqa: E712
        hdr = _out_hdr("Campaign", c.name, f"{c.status} · {len(rcpts)} recipients · context {c.context_kind}")
        return templates.TemplateResponse("campaign_detail.html", {
            "request": request, "user": user, "active": "campaigns", "c": c, "steps": steps,
            "rcpts": rcpts, "rc_status": dict(rc_status), "preview": aud, "f": f, "hdr": hdr,
            "mailbox": mailbox, "mailboxes": mailboxes, "STATUSES": CAMP.CAMPAIGN_STATUSES,
            "paused_all": SG.outreach_paused(session), "start_problems": CAMP.start_problems(session, c),
            "can_manage": _outreach_admin(session, user)})


@app.post("/campaigns/{cid}/audience")
def campaign_audience(request: Request, cid: int, do: str = Form("preview"), product: str = Form(""),
                      country: str = Form(""), engagement: str = Form(""), exclude_customers: str = Form(""),
                      exclude_negative: str = Form(""), exclude_negotiating: str = Form(""),
                      exclude_recent: str = Form(""), expected: str = Form("")):
    with Session(engine) as session:
        user = current_user(request, session)
        if not is_admin(user):
            return _forbidden()
        c = session.get(Campaign, cid)
        if not c:
            return _not_found()
        flags = {k for k, v in zip(_EXCLUDES, (exclude_customers, exclude_negative, exclude_negotiating,
                                               exclude_recent)) if v == "1"}
        f = _audience_filter(c, product, country, engagement, flags)
        if do == "enroll":
            if not _outreach_admin(session, user):
                return _forbidden()
            if not c.request_id:          # an unscoped audience would be every lead in the database
                _flash(request, "Link this campaign to a request first — only that request's buyers can be enrolled.",
                       "rose")
                return RedirectResponse(f"/campaigns/{cid}", status_code=303)
            if not expected.isdigit():
                _flash(request, "Preview the audience first, then confirm the count.", "rose")
                return RedirectResponse(f"/campaigns/{cid}", status_code=303)
            res = CAMP.enroll(session, c, user, f, expected=int(expected))
            if res.get("error"):
                _flash(request, res["error"], "rose")
            else:
                _flash(request, f"Enrolled {res['created']} recipient(s); {res['skipped_suppressed']} suppressed, "
                                f"{res['skipped_existing']} already in.")
            return RedirectResponse(f"/campaigns/{cid}", status_code=303)
        qs = "&".join([f"{k}={quote_plus(v)}" for k, v in [("product", product), ("country", country),
                                                           ("engagement", engagement)] if v]
                      + [f"{k}=1" for k in sorted(flags)] + ["preview=1"])
        return RedirectResponse(f"/campaigns/{cid}?{qs}", status_code=303)


@app.post("/campaigns/{cid}/sequence")
def campaign_sequence(request: Request, cid: int, subjects: List[str] = Form(default=[]),
                      bodies: List[str] = Form(default=[]), bodies_html: List[str] = Form(default=[]),
                      delays: List[str] = Form(default=[]), confirm: str = Form("")):
    with Session(engine) as session:
        user = current_user(request, session)
        if not _outreach_admin(session, user):          # what buyers read is a campaign-manager decision
            return _forbidden()
        c = session.get(Campaign, cid)
        if not c:
            return _not_found()
        if c.status == "running" and confirm != "1":
            _flash(request, "Editing a running campaign creates a new sequence version — confirm to proceed.",
                   "rose")
            return RedirectResponse(f"/campaigns/{cid}", status_code=303)
        steps, problems = [], []
        for i, subj in enumerate(subjects):
            body = bodies[i] if i < len(bodies) else ""
            html = bodies_html[i] if i < len(bodies_html) else ""
            if not (subj or "").strip() and not body.strip() and not html.strip():
                continue
            # an HTML-only step gets its text part from the design (same rule as set_sequence)
            text_part = body or (CR.html_to_text(CR.sanitize_html(html)) if html.strip() else "")
            problems += [f"email {len(steps) + 1}: {e}" for e in CR.validate_step(subj, text_part, html)]
            steps.append({"subject": subj, "body": body, "body_html": html,
                          "delay_days": (delays[i] if i < len(delays) else "0")})
        if not steps:
            _flash(request, "Add at least an initial email.", "rose")
            return RedirectResponse(f"/campaigns/{cid}", status_code=303)
        if problems:
            _flash(request, "Not saved — " + "; ".join(problems)[:600], "rose")
            return RedirectResponse(f"/campaigns/{cid}", status_code=303)
        v = CAMP.set_sequence(session, c, steps, user)
        _flash(request, f"Saved sequence v{v} ({len(steps)} step(s)).")
    return RedirectResponse(f"/campaigns/{cid}", status_code=303)


@app.post("/campaigns/{cid}/controls")
def campaign_controls(request: Request, cid: int, daily_limit: str = Form(""), mailbox_id: str = Form(""),
                      send_window_start: str = Form(""), send_window_end: str = Form(""),
                      send_days: List[str] = Form(default=[]), warmup_plan: Optional[str] = Form(None),
                      daily_limit_was: str = Form("")):
    """Daily limit (+ the automatic warm-up plan), sending mailbox, and the UTC sending window/days. `daily_limit_was`
    = the limit the page showed: an unchanged field never reverts a limit the warm-up raised while the page was open."""
    with Session(engine) as session:
        user = current_user(request, session)
        if not _outreach_admin(session, user):
            return _forbidden()
        c = session.get(Campaign, cid)
        if not c:
            return _not_found()
        kept = False
        try:
            new_limit = max(0, min(500, int(daily_limit)))
            if daily_limit_was.strip().isdigit() and new_limit == int(daily_limit_was) and new_limit != c.daily_limit:
                kept = True                     # a stale page: keep the current (e.g. automatically raised) limit
            else:
                c.daily_limit = new_limit
        except (TypeError, ValueError):
            pass
        if warmup_plan is not None:
            plan = ",".join(str(p) for p in CAMP.parse_warmup_plan(warmup_plan))
            if plan != (c.warmup_plan or ""):
                c.warmup_plan = plan
                c.warmup_checked_on = datetime.utcnow().strftime("%Y-%m-%d")   # a new plan starts the next UTC day
                if not plan:
                    WQ.resolve_by_key(session, f"campaign_warmup_held:{c.id}", user, note="warm-up plan cleared")
        if mailbox_id.isdigit():
            mb = session.get(MailAccount, int(mailbox_id))
            if mb and mb.admin_owned:
                c.mailbox_id = mb.id
        try:
            ws, we = int(send_window_start), int(send_window_end)
            if 0 <= ws < we <= 24:
                c.send_window_start, c.send_window_end = ws, we
        except (TypeError, ValueError):
            pass
        days = sorted({d for d in send_days if d in {"0", "1", "2", "3", "4", "5", "6"}})
        if days:
            c.send_days = ",".join(days)
        c.updated_at = datetime.utcnow()
        session.add(c)
        pipeline.audit(session, user, "campaign", c.id, "controls",
                       {"daily_limit": c.daily_limit, "mailbox_id": c.mailbox_id,
                        "window": f"{c.send_window_start}-{c.send_window_end}", "days": c.send_days,
                        "warmup_plan": c.warmup_plan},
                       tenant_id=c.tenant_id)
        session.commit()
        _flash(request, f"Saved: {c.daily_limit}/day, {c.send_window_start}:00–{c.send_window_end}:00 UTC"
                        + (f", warm-up {c.warmup_plan}" if c.warmup_plan else "")
                        + (" (the daily limit was changed while this page was open — kept)" if kept else "") + ".")
    return RedirectResponse(f"/campaigns/{cid}", status_code=303)


@app.get("/campaigns/{cid}/preview", response_class=HTMLResponse)
def campaign_preview(request: Request, cid: int, step: int = 0):
    """Exactly what a buyer receives — rendered by the SAME function the sender uses, for the first enrolled
    buyer. Admin-only; the HTML is shown in a sandboxed frame (no scripts, no navigation)."""
    with Session(engine) as session:
        user = current_user(request, session)
        if not is_admin(user):
            return _forbidden()
        c = session.get(Campaign, cid)
        if not c:
            return _not_found()
        steps = CAMP.steps_for(session, c)
        rc = session.exec(select(CampaignRecipient).where(CampaignRecipient.campaign_id == cid)
                          .order_by(CampaignRecipient.id)).first()
        ld = session.get(Lead, rc.lead_id) if rc and rc.lead_id else None
        mb = session.get(MailAccount, c.mailbox_id) if c.mailbox_id else None
        if not steps or step < 0 or step >= len(steps) or ld is None or mb is None:
            _flash(request, "Preview needs a saved sequence, a mailbox and at least one enrolled buyer.", "rose")
            return RedirectResponse(f"/campaigns/{cid}", status_code=303)
        msg = CR.render_campaign_message(session, c, steps[step], ld, mb)
        from_hdr = f"{mb.from_name} <{mb.email}>" if mb.from_name else mb.email
    esc = html_lib.escape
    head = "".join(f"<div><b>{esc(k)}:</b> {esc(v)}</div>" for k, v in
                   [("From", from_hdr), ("To", rc.to_email), ("Subject", msg["subject"])]
                   + list(msg["headers"].items()))
    body = (f'<iframe sandbox="" srcdoc="{esc(msg["html"], quote=True)}" '
            'style="width:100%;height:560px;border:1px solid #334155;background:#fff;border-radius:8px"></iframe>'
            f'<pre style="white-space:pre-wrap;margin-top:12px">{esc(msg["text"])}</pre>'
            if msg["ok"] else f'<div style="color:#fb7185">Blocked ({esc(msg["scope"])}): {esc(msg["error"])}</div>')
    return HTMLResponse(f'<!DOCTYPE html><html><head><meta charset="utf-8"><title>Preview</title></head>'
                        f'<body style="font-family:Arial,sans-serif;background:#0f172a;color:#e2e8f0;padding:16px">'
                        f'<div style="margin-bottom:10px"><a href="/campaigns/{cid}" style="color:#93c5fd">← back</a>'
                        f' · email {step + 1} of {len(steps)}</div><div style="font-size:13px;margin-bottom:10px">'
                        f'{head}</div>{body}</body></html>')


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
            if not _outreach_admin(session, user):
                return _forbidden()
            problems = CAMP.start_problems(session, c)
            if problems:
                _flash(request, "Can't start yet — " + "; ".join(problems)[:600], "rose")
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
        if outcome in ("positive", "negative", "neutral", "auto_reply", "follow_up_later", "wrong_contact",
                       "unsubscribed"):           # the reply is reviewed → its Work Queue task is done
            WQ.resolve_by_key(session, f"review_inbound_reply:lead:{lead_id}", user, f"outcome: {outcome}")
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


def _require(session, user, perm):
    """Central route guard: returns a 403 response if the user lacks `perm`, else None."""
    return None if (user is not None and authz.has_permission(session, user, perm)) else _forbidden()


@app.get("/admin/users", response_class=HTMLResponse)
def admin_users(request: Request, error: str = "", ok: str = "", q: str = "", account_class: str = "",
                role: str = "", status: str = ""):
    """Administration → Users. Requires users.manage. Internal staff and sellers are clearly distinguished."""
    with Session(engine) as session:
        user = current_user(request, session)
        deny = _require(session, user, "users.manage")
        if deny:
            return deny
        from .models import UserProfile
        profs = {p.user_id: p for p in session.exec(select(UserProfile)).all()}
        users = session.exec(select(User).order_by(User.id.asc())).all()
        lead_counts = dict(session.exec(select(Lead.owner_id, func.count(Lead.id)).group_by(Lead.owner_id)).all())
        req_counts = dict(session.exec(select(ServiceRequest.owner_id, func.count(ServiceRequest.id))
                                       .group_by(ServiceRequest.owner_id)).all())
        rows = []
        for u in users:
            p = profs.get(u.id)
            ac = p.account_class if p else ("internal" if u.role == "admin" else "seller")
            rk = p.role_key if p else ("founder" if u.role == "admin" else "seller")
            st = p.account_status if p else ("active" if u.active else "disabled")
            if q and q.lower() not in (u.email + " " + (u.name or "")).lower():
                continue
            if account_class and ac != account_class:
                continue
            if role and rk != role:
                continue
            if status and st != status:
                continue
            rows.append({"u": u, "class": ac, "role_key": rk, "role_name": P.ROLE_TEMPLATES.get(rk, {}).get("name", rk),
                         "status": st, "leads": lead_counts.get(u.id, 0), "requests": req_counts.get(u.id, 0),
                         "last_login": p.last_login_at if p else None})
    return templates.TemplateResponse("admin_users.html", {
        "request": request, "user": user, "active": "users", "rows": rows,
        "role_templates": P.ROLE_TEMPLATES, "f": {"q": q, "account_class": account_class, "role": role,
        "status": status}, "error": error, "ok": ok})


@app.post("/admin/users")
def admin_user_create(request: Request, email: str = Form(...), name: str = Form(""),
                      account_class: str = Form("seller"), role_key: str = Form(""), password: str = Form(...)):
    with Session(engine) as session:
        actor = current_user(request, session)
        if _require(session, actor, "users.manage"):
            return _forbidden()
        ok, msg = ACCESS.create_user(session, actor, email=email, name=name, account_class=account_class,
                                     role_key=role_key, password=password)
        session.commit()
    return RedirectResponse(f"/admin/users?{'ok=created' if ok else 'error=' + msg[:40]}", status_code=303)


@app.post("/admin/users/{user_id}/password")
def admin_user_password(request: Request, user_id: int, password: str = Form(...)):
    with Session(engine) as session:
        actor = current_user(request, session)
        if _require(session, actor, "users.manage"):
            return _forbidden()
        u = session.get(User, user_id)
        if not u or len(password) < 6:
            return RedirectResponse("/admin/users?error=input", status_code=303)
        ok, msg = ACCESS.reset_password(session, actor, u, password)   # Founder guard lives in the service
        session.commit()
    return RedirectResponse(f"/admin/users/{user_id}?{'ok=password' if ok else 'error=' + msg[:60]}",
                            status_code=303)


@app.post("/admin/users/{user_id}/email")
def admin_user_email(request: Request, user_id: int, email: str = Form(""), reason: str = Form("")):
    """Change a user's login email (users.manage; the Founder guard, format/duplicate checks, session revocation
    and the audit row live in the service)."""
    with Session(engine) as session:
        actor = current_user(request, session)
        if _require(session, actor, "users.manage"):
            return _forbidden()
        u = session.get(User, user_id)
        if not u:
            return _not_found()
        ok, msg = ACCESS.change_email(session, actor, u, email, reason=reason)
        session.commit()
    return RedirectResponse(f"/admin/users/{user_id}?tab=security&{'ok' if ok else 'error'}={quote_plus(msg[:60])}",
                            status_code=303)


@app.get("/admin/users/{user_id}", response_class=HTMLResponse)
def admin_user_detail(request: Request, user_id: int, tab: str = "profile", ok: str = "", error: str = ""):
    """User workspace: Profile · Access · Assigned work · Activity · Security tabs. No impersonation."""
    with Session(engine) as session:
        actor = current_user(request, session)
        deny = _require(session, actor, "users.manage")
        if deny:
            return deny
        u = session.get(User, user_id)
        if not u:
            return _not_found()
        p = authz.ensure_profile(session, u); session.commit()
        eff = sorted(authz.effective_permissions(session, u))
        overrides = {o.permission_key: o for o in session.exec(
            select(PermissionOverride).where(PermissionOverride.user_id == u.id)).all()}
        # effective-permission preview grouped by permission group, with high-risk + 'why'
        groups = {}
        for key, meta in P.PERMISSIONS.items():
            groups.setdefault(meta["group"], []).append({
                "key": key, "label": meta["label"], "high_risk": meta["high_risk"],
                "granted": key in eff, "override": overrides.get(key).effect if overrides.get(key) else "",
                "why": authz.why(session, u, key)})
        assignable = [rk for rk, t in P.ROLE_TEMPLATES.items()
                      if t["account_class"] == p.account_class and authz.can_assign_role(session, actor, rk)]
        assigned = {
            "leads": session.exec(select(func.count()).select_from(Lead).where(Lead.owner_id == u.id)).one(),
            "requests": session.exec(select(func.count()).select_from(ServiceRequest)
                                     .where(ServiceRequest.owner_id == u.id)).one(),
        }
        activity = session.exec(select(AccessAuditLog).where(AccessAuditLog.target_user_id == u.id)
                                .order_by(AccessAuditLog.id.desc()).limit(50)).all()
        umap = {x.id: x for x in session.exec(select(User)).all()}
    return templates.TemplateResponse("admin_user_detail.html", {
        "request": request, "user": actor, "active": "users", "tab": tab, "u": u, "p": p, "eff": eff,
        "groups": groups, "assignable": assignable, "scopes": P.SCOPES, "scope_labels": P.SCOPE_LABELS,
        "assigned": assigned, "activity": activity, "umap": umap, "role_templates": P.ROLE_TEMPLATES,
        "is_last_founder": authz.is_last_founder(session, u.id), "ok": ok, "error": error})


@app.post("/admin/users/{user_id}/access")
def admin_user_access(request: Request, user_id: int, role_key: str = Form(""), scope: str = Form(""),
                      reason: str = Form("")):
    with Session(engine) as session:
        actor = current_user(request, session)
        if _require(session, actor, "users.manage"):
            return _forbidden()
        u = session.get(User, user_id)
        if not u:
            return _not_found()
        ok, msg = ACCESS.set_role(session, actor, u, role_key, scope=scope, reason=reason)
        session.commit()
    return RedirectResponse(f"/admin/users/{user_id}?tab=access&{'ok' if ok else 'error'}={msg[:40]}",
                            status_code=303)


@app.post("/admin/users/{user_id}/permission")
def admin_user_permission(request: Request, user_id: int, permission_key: str = Form(...),
                          effect: str = Form("grant"), reason: str = Form("")):
    with Session(engine) as session:
        actor = current_user(request, session)
        if _require(session, actor, "users.manage"):
            return _forbidden()
        u = session.get(User, user_id)
        if not u:
            return _not_found()
        ok, msg = ACCESS.set_override(session, actor, u, permission_key, effect, reason=reason)
        session.commit()
    return RedirectResponse(f"/admin/users/{user_id}?tab=access&{'ok' if ok else 'error'}={msg[:40]}",
                            status_code=303)


@app.post("/admin/users/{user_id}/status")
def admin_user_status(request: Request, user_id: int, status: str = Form(...), reason: str = Form("")):
    with Session(engine) as session:
        actor = current_user(request, session)
        if _require(session, actor, "users.manage"):
            return _forbidden()
        u = session.get(User, user_id)
        if not u:
            return _not_found()
        ok, msg = ACCESS.set_status(session, actor, u, status, reason=reason)
        session.commit()
    return RedirectResponse(f"/admin/users/{user_id}?tab=security&{'ok' if ok else 'error'}={msg[:40]}",
                            status_code=303)


@app.get("/admin/roles", response_class=HTMLResponse)
def admin_roles(request: Request):
    """Administration → Roles & Access: the role templates + their permissions. Requires users.manage."""
    with Session(engine) as session:
        actor = current_user(request, session)
        deny = _require(session, actor, "users.manage")
        if deny:
            return deny
        counts = dict(session.exec(select(UserProfile.role_key, func.count(UserProfile.id))
                                   .group_by(UserProfile.role_key)).all())
    return templates.TemplateResponse("admin_roles.html", {
        "request": request, "user": actor, "active": "roles", "templates_": P.ROLE_TEMPLATES,
        "perm_meta": P.PERMISSIONS, "counts": counts, "scope_labels": P.SCOPE_LABELS})


@app.get("/admin/access-log", response_class=HTMLResponse)
def admin_access_log(request: Request):
    """Administration → Activity: the immutable access-change audit trail. Requires audit.view."""
    with Session(engine) as session:
        actor = current_user(request, session)
        deny = _require(session, actor, "audit.view")
        if deny:
            return deny
        logs = session.exec(select(AccessAuditLog).order_by(AccessAuditLog.id.desc()).limit(300)).all()
        umap = {u.id: u for u in session.exec(select(User)).all()}
    return templates.TemplateResponse("admin_access_log.html", {
        "request": request, "user": actor, "active": "access_log", "logs": logs, "umap": umap})


@app.get("/me/profile", response_class=HTMLResponse)
def my_profile(request: Request, ok: str = ""):
    """Self-service profile for any signed-in user (internal or seller). Internal-only fields are never shown
    to sellers; a seller's profile never grants access to buyer data."""
    with Session(engine) as session:
        u = current_user(request, session)
        if not u:
            return _forbidden()
        p = authz.ensure_profile(session, u); session.commit()
        eff = sorted(authz.effective_permissions(session, u)) if authz.account_class(session, u) == "internal" else []
    return templates.TemplateResponse("profile.html", {
        "request": request, "user": u, "active": "profile", "p": p, "eff": eff,
        "is_seller": (p.account_class == "seller"), "role_name": P.ROLE_TEMPLATES.get(p.role_key, {}).get("name", p.role_key)})


@app.post("/me/profile")
def my_profile_save(request: Request, full_name: str = Form(""), display_name: str = Form(""),
                    job_title: str = Form(""), department: str = Form(""), company: str = Form(""),
                    country: str = Form(""), timezone: str = Form(""), preferred_language: str = Form("en"),
                    phone: str = Form(""), trading_interests: str = Form(""), preferred_markets: str = Form("")):
    with Session(engine) as session:
        u = current_user(request, session)
        if not u:
            return _forbidden()
        ACCESS.update_profile(session, u, u, {
            "full_name": full_name, "display_name": display_name, "job_title": job_title,
            "department": department, "company": company, "country": country, "timezone": timezone,
            "preferred_language": preferred_language, "phone": phone,
            "trading_interests": trading_interests, "preferred_markets": preferred_markets}, self_edit=True)
        session.commit()
    return RedirectResponse("/me/profile?ok=1", status_code=303)


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
