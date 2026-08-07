"""go4it data model.

Asymmetric brokerage pipeline: external **buyer Leads** are matched against our
internal **Product** catalog (sourced from **Suppliers**), then priced into
**Quotes** using **RateCard** / **CostParam** / **FxRate** inputs.

Money is stored as float, but every quote value is computed in Decimal and
quantized to 2 dp before storage (see app/quoting.py) — so no float arithmetic
error ever reaches a buyer. Phase 2 only *adds* tables, so no migration of the
catalog is needed; Alembic is introduced when the first altering migration lands.
"""
from datetime import datetime
from typing import Optional

from sqlalchemy import UniqueConstraint
from sqlmodel import Field, SQLModel


class Supplier(SQLModel, table=True):
    """A source we can buy from (typically in Iran)."""
    id: Optional[int] = Field(default=None, primary_key=True)
    name: str
    name_normalized: str = Field(default="", index=True)   # for dedup
    country: str = "IR"
    city: str = ""
    contact: str = ""
    email: str = ""
    phone: str = ""
    reliability: int = 3           # 1-5, team's own rating
    payment_terms: str = ""
    active: bool = True
    created_at: datetime = Field(default_factory=datetime.utcnow)


class Product(SQLModel, table=True):
    """A product we can supply — the catalog / knowledge base we price against."""
    id: Optional[int] = Field(default=None, primary_key=True)
    name: str
    category: str = ""
    spec: str = ""
    hs_code: str = ""
    exw_price: float = 0           # factory-gate price per unit
    currency: str = "USD"
    unit: str = ""
    weight_kg_per_unit: float = 0
    cbm_per_unit: float = 0        # volume per unit, for freight
    packaging: str = ""
    min_order_qty: float = 0
    origin_region: str = ""        # e.g. Tabriz, Isfahan, Tehran
    supplier_id: Optional[int] = Field(default=None, foreign_key="supplier.id")
    active: bool = True
    updated_at: datetime = Field(default_factory=datetime.utcnow)
    updated_by: str = ""


class Lead(SQLModel, table=True):
    """A buyer request (from go4worldbusiness, CSV, or manual entry)."""
    id: Optional[int] = Field(default=None, primary_key=True)
    tracking_code: str = Field(default="", index=True)      # G4-YYYYMM-####
    source: str = "manual"         # manual | csv | go4world | research | ...
    external_id: str = ""          # id at the source, for dedup
    content_hash: str = Field(default="", index=True)       # dedup identical leads
    website: str = ""              # the buyer's own website (clickable)
    source_url: str = ""           # where this lead was found (provenance)

    # buyer
    buyer_company: str = ""
    contact_name: str = ""
    email: str = ""
    phone: str = ""

    # request
    product: str
    category: str = ""
    spec: str = ""
    quantity: float = 0
    unit: str = ""
    target_price: float = 0
    currency: str = "USD"
    dest_country: str = ""         # GE | TR | ...
    dest_city: str = ""

    # workflow — the 5-stage CRM pipeline
    status: str = "new"            # new | quoted | negotiating | won | lost
    owner_id: Optional[int] = Field(default=None, foreign_key="user.id")
    lost_reason: str = ""
    first_response_at: Optional[datetime] = None   # OUR first touch (outreach/note/call)
    buyer_replied_at: Optional[datetime] = None    # the buyer's first inbound reply (email/whatsapp)
    accepted_at: Optional[datetime] = None         # buyer accepted a quote on the public pro-forma
    next_action_at: Optional[datetime] = None      # follow-up date (the "contact today" queue)
    next_action_note: str = ""
    notes: str = ""
    posted_at: Optional[datetime] = None
    created_at: datetime = Field(default_factory=datetime.utcnow)


class Match(SQLModel, table=True):
    """A scored pairing of a buyer Lead and a catalog Product."""
    __table_args__ = (UniqueConstraint("lead_id", "product_id", name="uq_match_lead_product"),)

    id: Optional[int] = Field(default=None, primary_key=True)
    lead_id: int = Field(foreign_key="lead.id", index=True)
    product_id: int = Field(foreign_key="product.id", index=True)
    score: float = 0
    reasons: str = ""
    is_dismissed: bool = False
    created_at: datetime = Field(default_factory=datetime.utcnow)


# --------------------------------------------------------------------------- pricing inputs

class RateCard(SQLModel, table=True):
    """A freight lane rate (inland Iran, or international to the border)."""
    id: Optional[int] = Field(default=None, primary_key=True)
    lane_from: str = ""            # e.g. Isfahan
    lane_to: str = ""             # e.g. Sadakhlo (GE border)
    leg: str = "international"      # inland | international
    dest_country: str = ""         # ISO2 this lane serves (""=legacy/any); scopes corridors per market
    rate_per_truck: float = 0
    rate_per_tonne: float = 0      # LCL/small-volume lane: freight scales per tonne, not per truck
    truck_capacity_t: float = 25
    currency: str = "USD"
    active: bool = True


class CostParam(SQLModel, table=True):
    """A single tunable pricing parameter, e.g. insurance_pct or coo_fee."""
    id: Optional[int] = Field(default=None, primary_key=True)
    key: str = Field(index=True)   # export_clearance, coo_fee, insurance_pct, margin_pct, ...
    value: float = 0
    unit: str = ""                 # "%", "USD/shipment", ...
    dest_country: str = ""
    note: str = ""


class FxRate(SQLModel, table=True):
    """Exchange rate the team actually gets (manual override; sanctions mean the
    published rate != the real one)."""
    id: Optional[int] = Field(default=None, primary_key=True)
    base: str
    quote: str = "USD"
    rate: float = 1
    note: str = ""


# --------------------------------------------------------------------------- team & activity

class User(SQLModel, table=True):
    """A member of the trading team."""
    id: Optional[int] = Field(default=None, primary_key=True)
    email: str = Field(index=True, unique=True)
    name: str = ""
    password_hash: str = ""
    role: str = "agent"            # admin | manager | agent | viewer
    active: bool = True
    telegram_user_id: str = ""
    created_at: datetime = Field(default_factory=datetime.utcnow)


class Activity(SQLModel, table=True):
    """One entry in a lead's timeline (note, call, status change, quote sent…)."""
    id: Optional[int] = Field(default=None, primary_key=True)
    lead_id: int = Field(foreign_key="lead.id", index=True)
    user_id: Optional[int] = Field(default=None, foreign_key="user.id")
    kind: str = "note"             # note | call | status_change | quote_sent | assignment
    body: str = ""
    created_at: datetime = Field(default_factory=datetime.utcnow)


class IngestionRun(SQLModel, table=True):
    """One pass of a lead source (for observability / no-silent-caps)."""
    id: Optional[int] = Field(default=None, primary_key=True)
    source: str = ""
    started_at: datetime = Field(default_factory=datetime.utcnow)
    finished_at: Optional[datetime] = None
    leads_seen: int = 0
    leads_new: int = 0
    leads_duplicate: int = 0
    status: str = "running"        # running | ok | error
    error: str = ""


class CommandJob(SQLModel, table=True):
    """A founder command typed into the dashboard box: free-text prompt -> parsed intent
    (country + category) -> a background harvest that creates leads. Same observability
    shape as IngestionRun, plus the prompt + parsed params + a human-readable result note."""
    id: Optional[int] = Field(default=None, primary_key=True)
    prompt: str = ""
    action: str = ""               # harvest_uae | market | unknown
    params: str = ""               # JSON blob of the parsed intent
    status: str = "queued"         # queued | running | ok | error
    note: str = ""                 # human-readable result summary
    leads_seen: int = 0
    leads_new: int = 0
    leads_duplicate: int = 0
    error: str = ""
    owner_id: Optional[int] = Field(default=None, foreign_key="user.id")
    created_at: datetime = Field(default_factory=datetime.utcnow)
    started_at: Optional[datetime] = None
    finished_at: Optional[datetime] = None


class Outreach(SQLModel, table=True):
    """One message in a lead's conversation thread. OUTBOUND = a contact we made (email/whatsapp/
    call/note; SMTP-sent when configured, else 'logged'); INBOUND = a buyer reply threaded in by the
    IMAP poller (app/inbound_email.py). direction + from_addr + message_id turn this into a two-way
    thread; the /leads/{id} Conversation panel renders these as bubbles."""
    id: Optional[int] = Field(default=None, primary_key=True)
    lead_id: int = Field(foreign_key="lead.id", index=True)
    direction: str = "out"         # out (we sent) | in (buyer replied)
    channel: str = "email"         # email | whatsapp | call | note
    recipient: str = ""            # email address or phone we sent to
    from_addr: str = ""            # inbound sender (email / phone)
    subject: str = ""
    body: str = ""
    message_id: str = Field(default="", index=True)   # email Message-ID (inbound dedup + threading)
    status: str = "logged"         # logged | sent | failed | received
    error: str = ""
    user_id: Optional[int] = Field(default=None, foreign_key="user.id")
    created_at: datetime = Field(default_factory=datetime.utcnow)


# --------------------------------------------------------------------------- post-win

class Deal(SQLModel, table=True):
    """A won lead being executed: sourcing -> logistics -> customs -> delivery ->
    settlement, with planned-vs-realized margin."""
    id: Optional[int] = Field(default=None, primary_key=True)
    tracking_code: str = Field(default="", index=True)      # G4-YYYYMM-####-D
    lead_id: int = Field(foreign_key="lead.id", index=True)
    quote_id: Optional[int] = Field(default=None, foreign_key="quote.id")
    owner_id: Optional[int] = Field(default=None, foreign_key="user.id")
    stage: str = "won"             # see deal_service.DEAL_STAGES
    planned_revenue: float = 0
    planned_cost: float = 0
    planned_margin: float = 0
    actual_revenue: float = 0
    actual_cost: float = 0
    realized_margin: float = 0
    payment_received: float = 0
    freight_ref: str = ""
    notes: str = ""
    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)
    closed_at: Optional[datetime] = None


class ComplianceDoc(SQLModel, table=True):
    """A trade document attached to a deal (CoO, invoice, packing list…)."""
    id: Optional[int] = Field(default=None, primary_key=True)
    deal_id: int = Field(foreign_key="deal.id", index=True)
    doc_type: str = ""             # certificate_of_origin, commercial_invoice, ...
    reference_no: str = ""
    issued_by: str = ""
    issued_at: Optional[datetime] = None
    expires_at: Optional[datetime] = None
    file_path: str = ""
    status: str = "received"       # pending | received | verified | expired
    created_at: datetime = Field(default_factory=datetime.utcnow)


class Quote(SQLModel, table=True):
    """A priced offer for a Lead: EXW + delivered, with a frozen breakdown so it
    reproduces identically months later."""
    id: Optional[int] = Field(default=None, primary_key=True)
    tracking_code: str = Field(default="", index=True)      # G4-YYYYMM-####-Qn
    lead_id: int = Field(foreign_key="lead.id", index=True)
    product_id: int = Field(foreign_key="product.id")
    quantity: float = 0
    incoterm: str = "DAP"          # EXW | CPT | DAP | DAF
    dest_border: str = ""
    quote_currency: str = "USD"

    exw_unit: float = 0
    exw_total: float = 0
    delivered_unit: float = 0
    delivered_total: float = 0
    margin_pct: float = 0

    breakdown: str = ""            # JSON: list of {label, basis, amount, per_unit}
    params_snapshot: str = ""      # JSON: the rate/cost params used
    fx_snapshot: str = ""          # JSON: {base, quote, rate}

    validity_days: int = 14
    status: str = "draft"          # draft | approved | sent | expired | superseded
    share_token: str = Field(default="", index=True)   # unguessable slug for the public buyer link
    version: int = 1
    created_by: str = ""
    approved_by: str = ""
    accepted_at: Optional[datetime] = None     # buyer accepted this pro-forma on the public link
    buyer_response: str = ""                    # "" | accepted | changes (buyer's action on /p/)
    created_at: datetime = Field(default_factory=datetime.utcnow)
