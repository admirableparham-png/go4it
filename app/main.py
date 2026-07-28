"""go4it web app — add demands & offers, auto-match, alert the team."""
from pathlib import Path

from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlmodel import Session, select

from .config import MATCH_THRESHOLD
from .db import engine, init_db
from .matching import score_pair
from .models import Demand, Match, Offer
from .telegram import notify_match

BASE_DIR = Path(__file__).parent
app = FastAPI(title="go4it")
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))
app.mount("/static", StaticFiles(directory=str(BASE_DIR / "static")), name="static")


@app.on_event("startup")
def on_startup() -> None:
    init_db()


def _open(session, model):
    """All rows of `model` still open for matching."""
    return session.exec(select(model).where(model.status == "open")).all()


def _run_match(session, *, demand=None, offer=None):
    """Match a newly added demand (or offer) against the opposite open pool,
    persist any new matches, and fire a Telegram alert for each one."""
    pairs = []
    if demand is not None:
        for off in _open(session, Offer):
            s, r = score_pair(demand, off)
            if s >= MATCH_THRESHOLD:
                pairs.append((demand, off, s, r))
    elif offer is not None:
        for dem in _open(session, Demand):
            s, r = score_pair(dem, offer)
            if s >= MATCH_THRESHOLD:
                pairs.append((dem, offer, s, r))

    saved = []
    for dem, off, s, r in pairs:
        exists = session.exec(
            select(Match).where(Match.demand_id == dem.id, Match.offer_id == off.id)
        ).first()
        if exists:
            continue
        session.add(Match(demand_id=dem.id, offer_id=off.id, score=s, reasons=r))
        saved.append((dem, off, s, r))
    session.commit()

    for dem, off, s, r in saved:
        notify_match(dem, off, s, r)
    return saved


@app.get("/", response_class=HTMLResponse)
def index(request: Request):
    with Session(engine) as session:
        demands = session.exec(select(Demand).order_by(Demand.id.desc())).all()
        offers = session.exec(select(Offer).order_by(Offer.id.desc())).all()
        matches = session.exec(
            select(Match).order_by(Match.score.desc(), Match.id.desc())
        ).all()
        dmap = {d.id: d for d in demands}
        omap = {o.id: o for o in offers}
        rows = [
            {"m": m, "demand": dmap.get(m.demand_id), "offer": omap.get(m.offer_id)}
            for m in matches
        ]
    return templates.TemplateResponse(
        "index.html",
        {"request": request, "demands": demands, "offers": offers, "rows": rows},
    )


@app.post("/demands")
def add_demand(
    product: str = Form(...),
    category: str = Form(""),
    spec: str = Form(""),
    quantity: float = Form(0),
    unit: str = Form(""),
    target_price: float = Form(0),
    currency: str = Form("USD"),
    location: str = Form(""),
    contact: str = Form(""),
    notes: str = Form(""),
):
    with Session(engine) as session:
        d = Demand(
            product=product, category=category, spec=spec, quantity=quantity,
            unit=unit, target_price=target_price, currency=currency,
            location=location, contact=contact, notes=notes,
        )
        session.add(d)
        session.commit()
        session.refresh(d)
        _run_match(session, demand=d)
    return RedirectResponse("/", status_code=303)


@app.post("/offers")
def add_offer(
    product: str = Form(...),
    category: str = Form(""),
    spec: str = Form(""),
    quantity: float = Form(0),
    unit: str = Form(""),
    price: float = Form(0),
    currency: str = Form("USD"),
    location: str = Form(""),
    contact: str = Form(""),
    notes: str = Form(""),
):
    with Session(engine) as session:
        o = Offer(
            product=product, category=category, spec=spec, quantity=quantity,
            unit=unit, price=price, currency=currency,
            location=location, contact=contact, notes=notes,
        )
        session.add(o)
        session.commit()
        session.refresh(o)
        _run_match(session, offer=o)
    return RedirectResponse("/", status_code=303)


@app.post("/matches/{match_id}/status/{status}", response_class=HTMLResponse)
def set_match_status(request: Request, match_id: int, status: str):
    with Session(engine) as session:
        m = session.get(Match, match_id)
        if not m:
            return HTMLResponse("", status_code=404)
        if status in {"new", "contacted", "won", "lost"}:
            m.status = status
            session.add(m)
            session.commit()
            session.refresh(m)
        demand = session.get(Demand, m.demand_id)
        offer = session.get(Offer, m.offer_id)
    return templates.TemplateResponse(
        "partials/match_card.html",
        {"request": request, "m": m, "demand": demand, "offer": offer},
    )
