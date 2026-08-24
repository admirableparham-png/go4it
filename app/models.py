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
    company_id: Optional[int] = Field(default=None, foreign_key="company.id", index=True)  # Trade Network link (additive)
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
    active: bool = True            # False = "unlisted" by the trader (hidden from their list, reversible)
    owner_id: Optional[int] = Field(default=None, foreign_key="user.id")
    lost_reason: str = ""
    first_response_at: Optional[datetime] = None   # OUR first touch (outreach/note/call)
    buyer_replied_at: Optional[datetime] = None    # the buyer's first inbound reply (email/whatsapp)
    accepted_at: Optional[datetime] = None         # buyer accepted a quote on the public pro-forma
    next_action_at: Optional[datetime] = None      # follow-up date (the "contact today" queue)
    next_action_note: str = ""
    notes: str = ""                # PRIVATE admin notes — never sent to a seller
    posted_at: Optional[datetime] = None
    created_at: datetime = Field(default_factory=datetime.utcnow)

    # --- confidential managed-outreach pipeline (buyer PII is admin-only) ---
    managed: bool = False                                    # in the confidential pipeline (admin-owned)
    seller_id: Optional[int] = Field(default=None, foreign_key="user.id", index=True)  # the seller this buyer is FOR
    request_id: Optional[int] = Field(default=None, foreign_key="servicerequest.id", index=True)
    pipeline_stage: str = "identified"                       # 13-stage (see app/pipeline.PIPELINE_STAGES)
    anon_ref: str = Field(default="", index=True)            # Buyer-<ISO>-<NNN>, unique within a request, not the DB id
    assigned_admin_id: Optional[int] = Field(default=None, foreign_key="user.id")
    buyer_category: str = ""                                 # buyer's business category (distinct from product `category`)
    company_size_band: str = ""                              # e.g. "1-10" | "11-50" | "51-200" | "200+"
    fit_score: float = 0                                     # 0-100 admin fit rating
    seller_action_required: bool = False                     # a seller action is outstanding on this buyer

    # --- Trade Network (Phase 2, additive; admin-only canonical layer) ---
    company_id: Optional[int] = Field(default=None, foreign_key="company.id", index=True)
    engagement_class: str = ""     # prospect|contacted|engaged|qualified|customer|invalid|archived (derived, admin-correctable)
    reply_outcome: str = ""        # none|positive|negative|neutral|bounced|auto_reply (structured, never invented)


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
    # --- campaign send linkage (Phase 4, additive; the Outreach row stays the single event spine) ---
    campaign_id: Optional[int] = Field(default=None, foreign_key="campaign.id", index=True)
    campaign_recipient_id: Optional[int] = Field(default=None, foreign_key="campaignrecipient.id")
    campaign_version: int = 0
    campaign_step: int = 0
    in_reply_to: str = ""          # inbound: the outbound Message-ID this reply threads to (header match)
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
    owner_id: Optional[int] = Field(default=None, foreign_key="user.id", index=True)  # tenant scope (=lead owner)
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


class ServiceRequest(SQLModel, table=True):
    """A concierge request a trader submits (buyer search first; other services later) that the founder
    approves + fulfils. Isolated like everything else: owner_id = the requester, so it shows only on their
    dashboard; delivered buyers are attached to them via result_source_tag."""
    id: Optional[int] = Field(default=None, primary_key=True)
    tracking_code: str = Field(default="", index=True)      # SR-YYYYMM-####
    requester_id: Optional[int] = Field(default=None, foreign_key="user.id", index=True)
    owner_id: Optional[int] = Field(default=None, foreign_key="user.id", index=True)   # = requester (scope)
    request_type: str = "buyer_hunt"    # buyer_hunt | remittance | freight | contract | docs | other
    product: str = ""
    market: str = ""                    # target country/region, free text
    details: str = ""                   # the trader's prompt / brief
    status: str = "submitted"           # submitted | approved | rejected | running | done
    admin_note: str = ""                # founder note / rejection reason
    result: str = ""                    # delivery summary
    result_source_tag: str = ""         # Lead.source tag used to attach delivered buyers (req-<id>)
    result_file_path: str = ""          # a delivered file (contract PDF, remittance confirmation, ...)
    result_url: str = ""                # or a delivered link
    leads_delivered: int = 0
    approved_by: str = ""
    approved_at: Optional[datetime] = None
    started_at: Optional[datetime] = None
    done_at: Optional[datetime] = None
    created_at: datetime = Field(default_factory=datetime.utcnow)
    # --- Phase 3 (Requests + Work Queue) additive columns. Legacy status/request_type above are UNCHANGED
    #     and remain the operational drivers; these enrich the admin surface only. ---
    direction: str = "sell"        # sell (seller wants buyers) | buy (buyer wants suppliers) | service
    workflow_status: str = ""      # submitted|under_review|approved|in_progress|waiting_requester|
    # waiting_external|ready_for_delivery|delivered|completed|rejected|cancelled ("" until migrated/derived)
    priority: str = "normal"       # low | normal | high | urgent
    assigned_admin_id: Optional[int] = Field(default=None, foreign_key="user.id", index=True)
    due_at: Optional[datetime] = None
    last_activity_at: Optional[datetime] = None
    next_action_note: str = ""
    action_required_admin: bool = False
    action_required_requester: bool = False
    on_behalf_company_id: Optional[int] = Field(default=None, foreign_key="company.id")  # admin buy-side on behalf of a buyer
    admin_last_read_at: Optional[datetime] = None       # for unread-message indicators (updated via POST only)
    requester_last_read_at: Optional[datetime] = None


class RequestDeliverable(SQLModel, table=True):
    """One delivered artifact on a ServiceRequest. A request can be delivered MORE THAN ONCE — each
    delivery appends a row here (files named <id>_<name> so they never collide), while
    ServiceRequest.result_file_path/result keep pointing at the latest for back-compat."""
    id: Optional[int] = Field(default=None, primary_key=True)
    request_id: int = Field(foreign_key="servicerequest.id", index=True)
    file_path: str = ""            # relative path under REQUEST_FILES_DIR (<req_id>/<deliverable_id>_<name>)
    url: str = ""                  # or an external link
    note: str = ""                 # short delivery note
    delivered_by: str = ""         # admin email
    seller_safe: bool = False      # True = the admin confirmed this file carries NO buyer PII → seller may download
    created_at: datetime = Field(default_factory=datetime.utcnow)


class RequestMessage(SQLModel, table=True):
    """One message in the per-request chat between the requesting trader and the admin."""
    id: Optional[int] = Field(default=None, primary_key=True)
    request_id: int = Field(foreign_key="servicerequest.id", index=True)
    sender_id: Optional[int] = Field(default=None, foreign_key="user.id")
    sender_role: str = ""          # "admin" | "agent" | ... (from user.role at send time)
    body: str = ""
    created_at: datetime = Field(default_factory=datetime.utcnow)


class MailAccount(SQLModel, table=True):
    """A trader's OWN connected sending mailbox (e.g. a Gmail via App Password). A user may connect
    several and choose which one to send from at send time. The SMTP password is stored ENCRYPTED at
    rest (Fernet keyed off SECRET_KEY — see app/outreach.py)."""
    id: Optional[int] = Field(default=None, primary_key=True)
    user_id: int = Field(foreign_key="user.id", index=True)
    email: str = ""                # the From / login address
    from_name: str = ""            # display name on the From header
    provider: str = "gmail"        # gmail | custom
    smtp_host: str = "smtp.gmail.com"
    smtp_port: int = 587
    smtp_password_enc: str = ""     # Fernet-encrypted app password
    is_default: bool = False
    active: bool = True
    last_verified_at: Optional[datetime] = None
    created_at: datetime = Field(default_factory=datetime.utcnow)
    admin_owned: bool = False       # True = a Go4it-controlled mailbox used for confidential buyer outreach
    # --- Phase 4 operational fields (additive) ---
    daily_limit: int = 200          # max sends/day for this mailbox (never auto-increased)
    sent_today: int = 0
    sent_today_date: str = ""       # YYYY-MM-DD the counter is for (reset when the date rolls)
    paused: bool = False            # admin paused sending on this mailbox
    imap_host: str = ""             # optional per-mailbox inbound (else global IMAP_* is used)
    imap_port: int = 993
    imap_user: str = ""
    imap_password_enc: str = ""     # Fernet-encrypted IMAP password (same cipher as smtp_password_enc)
    last_inbound_at: Optional[datetime] = None
    last_outbound_at: Optional[datetime] = None
    last_send_error: str = ""
    spf_status: str = ""            # display-only when known (pass|fail|unknown)
    dkim_status: str = ""
    dmarc_status: str = ""
    updated_at: datetime = Field(default_factory=datetime.utcnow)


class StageEvent(SQLModel, table=True):
    """Authoritative history of a managed buyer's pipeline stage changes. The seller funnel is computed
    from THIS (the stages a buyer actually visited), never inferred from the current stage — so a Lost or
    Disqualified buyer is never counted as having reached Won."""
    id: Optional[int] = Field(default=None, primary_key=True)
    lead_id: int = Field(foreign_key="lead.id", index=True)
    request_id: Optional[int] = Field(default=None, foreign_key="servicerequest.id", index=True)
    from_stage: str = ""
    to_stage: str = ""
    actor_id: Optional[int] = Field(default=None, foreign_key="user.id")
    note: str = ""
    inferred: bool = False         # True = a migration-seeded event, NOT observed activity (funnel marks it)
    created_at: datetime = Field(default_factory=datetime.utcnow)


class SellerUpdate(SQLModel, table=True):
    """A SANITIZED, seller-visible progress update the admin publishes. Kept strictly separate from the
    private Lead.notes; scoped to the seller via seller_id. Buyer PII must never reach these fields."""
    id: Optional[int] = Field(default=None, primary_key=True)
    request_id: int = Field(foreign_key="servicerequest.id", index=True)
    lead_id: Optional[int] = Field(default=None, foreign_key="lead.id")   # None = request-level (aggregate) update
    seller_id: Optional[int] = Field(default=None, foreign_key="user.id", index=True)  # tenant scope
    anon_ref: str = ""             # the buyer's anonymized reference (or "" for aggregate)
    public_status: str = ""        # sanitized public stage label
    summary: str = ""              # sanitized summary (PII-scanned before publish)
    next_action: str = ""
    seller_question: str = ""      # optional question the seller must answer
    deadline: Optional[datetime] = None
    status: str = "open"           # open | resolved — so an "action required" can never go stale
    resolved_at: Optional[datetime] = None
    resolved_by: str = ""          # who resolved it (seller email or "admin")
    published: bool = True
    published_by: str = ""         # admin email
    created_at: datetime = Field(default_factory=datetime.utcnow)


class AuditLog(SQLModel, table=True):
    """Admin-only audit trail: stage changes, seller-update publishes, buyer-PII views/exports, and any
    explicit identity disclosure. Not tenant-scoped (admin pool)."""
    id: Optional[int] = Field(default=None, primary_key=True)
    actor_id: Optional[int] = Field(default=None, foreign_key="user.id")
    tenant_id: Optional[int] = Field(default=None, foreign_key="user.id", index=True)  # the seller the action relates to
    entity_type: str = ""          # lead | request | seller_update | quote | deal | company | contact | export
    entity_id: Optional[int] = None
    action: str = ""               # stage_change | publish_update | pii_view | pii_export | disclosure | ...
    meta: str = ""                 # JSON blob (before/after, fields, etc.)
    created_at: datetime = Field(default_factory=datetime.utcnow)


# ============================================================================
# Trade Network (Phase 2) — an ADMIN-ONLY canonical layer over the existing
# denormalized Lead/Supplier records. Strictly additive: existing tables keep
# their PKs and remain the source of truth for their workflows; these tables
# only ORGANIZE the network around them. Buyer identities stay admin-only.
# ============================================================================

class Company(SQLModel, table=True):
    """One real organization, represented once even when it holds multiple roles (buyer/seller/supplier).

    tenant_id is the ISOLATION scope for dedup + confidentiality: for a MANAGED buyer it is the seller the
    buyer is FOR (Lead.seller_id) — NOT the NULL owner_id — so seller A can never learn seller B got the same
    buyer; for a non-managed lead it is Lead.owner_id; for global suppliers/sellers it is NULL. Dedup runs
    only WITHIN a tenant partition, so cross-tenant matches/merges are structurally impossible."""
    id: Optional[int] = Field(default=None, primary_key=True)
    tenant_id: Optional[int] = Field(default=None, foreign_key="user.id", index=True)
    name: str = ""
    name_normalized: str = Field(default="", index=True)
    primary_role: str = "buyer"    # buyer|seller|supplier (convenience; authoritative roles in CompanyRole)
    country: str = ""
    city: str = ""
    website: str = ""
    domain: str = Field(default="", index=True)   # normalized registrable domain; "" if generic/none
    account_user_id: Optional[int] = Field(default=None, foreign_key="user.id")  # the agent-User that IS this seller; NULL=external
    external_ref: str = ""
    verification_status: str = "unverified"        # unverified|partial|verified|rejected
    verification_method: str = ""                  # email_reply|phone_call|website|registry|customs|manual
    verification_confidence: int = 0               # 0-100 (evidence-backed, not presented as factual accuracy)
    verified_at: Optional[datetime] = None
    verified_by: str = ""
    verification_notes: str = ""
    status: str = "active"         # active|archived (archived when merged away)
    merged_into_id: Optional[int] = Field(default=None, foreign_key="company.id")  # canonical survivor (reversible)
    notes: str = ""                # internal admin notes
    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)


class CompanyRole(SQLModel, table=True):
    """A role a company plays. One company → many roles (a trading house is buyer + supplier)."""
    __table_args__ = (UniqueConstraint("company_id", "role", name="uq_companyrole_company_role"),)
    id: Optional[int] = Field(default=None, primary_key=True)
    company_id: int = Field(foreign_key="company.id", index=True)
    role: str = ""                 # buyer|seller|supplier
    created_at: datetime = Field(default_factory=datetime.utcnow)


class Contact(SQLModel, table=True):
    """A person/mailbox at a Company. tenant_id is denormalized from the company for scope without a join."""
    id: Optional[int] = Field(default=None, primary_key=True)
    company_id: int = Field(foreign_key="company.id", index=True)
    tenant_id: Optional[int] = Field(default=None, foreign_key="user.id", index=True)
    name: str = ""
    title: str = ""
    email: str = ""
    email_normalized: str = Field(default="", index=True)
    phone: str = ""
    phone_normalized: str = Field(default="", index=True)   # last-9-digit tail (reuses find_lead_by_contact rule)
    website: str = ""
    email_health: str = "unknown"  # unknown|valid|role|generic|bounced|invalid
    contactability: int = 0        # 0-100
    is_primary: bool = False
    last_verified_at: Optional[datetime] = None
    source_ref: str = ""
    active: bool = True            # False = archived
    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)


class Provenance(SQLModel, table=True):
    """How a Company or Contact entered Go4it. Polymorphic (entity_type company|contact). Never invented —
    an undeterminable origin is recorded as source_type='unknown' with the raw slug kept for reclassification."""
    id: Optional[int] = Field(default=None, primary_key=True)
    entity_type: str = Field(default="company", index=True)   # company|contact
    entity_id: int = Field(default=0, index=True)
    tenant_id: Optional[int] = Field(default=None, foreign_key="user.id", index=True)
    source_type: str = Field(default="unknown", index=True)
    # research_agent|command|seller_request|manual|csv_import|directory|marketplace|customs|tender_rfq|referral|existing_supplier|unknown
    source_name: str = ""          # readable ("OpenStreetMap Georgia", "Concierge request 12")
    source_ref: str = ""           # external_id / row id at the source
    source_url: str = ""
    run_ref: str = ""              # "command:464" | "req-12" | "ingest:osm-ge" | backfill run tag
    inferred: bool = False         # True = migration-seeded, not observed
    collected_at: Optional[datetime] = None
    last_seen_at: Optional[datetime] = None
    created_at: datetime = Field(default_factory=datetime.utcnow)


class DuplicateCandidate(SQLModel, table=True):
    """A conservative, tenant-scoped duplicate pair for admin review. NEVER auto-merged."""
    __table_args__ = (UniqueConstraint("left_id", "right_id", name="uq_dupcand_pair"),)
    id: Optional[int] = Field(default=None, primary_key=True)
    tenant_id: Optional[int] = Field(default=None, foreign_key="user.id", index=True)
    entity_type: str = "company"
    left_id: int = Field(foreign_key="company.id", index=True)    # lower id (canonical ordering)
    right_id: int = Field(foreign_key="company.id", index=True)   # higher id
    signals: str = ""              # JSON list, e.g. ["email_exact","domain_exact"]
    match_type: str = "potential"  # strong|potential
    strength: int = 0              # 0-100 composite
    status: str = "open"           # open|confirmed|not_duplicate|merged|linked|deferred
    reviewer: str = ""
    reviewed_at: Optional[datetime] = None
    merged_into_id: Optional[int] = Field(default=None, foreign_key="company.id")
    created_at: datetime = Field(default_factory=datetime.utcnow)


# ============================================================================
# Requests + Work Queue (Phase 3) — an ADMIN-ONLY coordination layer that makes
# ServiceRequest the operational source of truth and gathers everything needing
# admin attention into one queue. Strictly additive: existing request columns,
# statuses, routes and the confidential buyer pipeline are unchanged. WorkItems
# are internal admin tasks; a requester-visible action is published ONLY through
# the existing sanitized SellerUpdate path and linked to (never merged with) it.
# ============================================================================


class WorkItem(SQLModel, table=True):
    """One unit of admin work. tenant_id = the seller/requester the item relates to (NULL = pure-internal /
    system, e.g. a failed job) so it never crosses a tenant boundary. Automatic items carry an
    idempotency_key guarded by a PARTIAL-unique index over OPEN statuses (see db._ensure_workitem_indexes),
    so re-running synchronization never duplicates an open task. Completed/dismissed items are retained."""
    id: Optional[int] = Field(default=None, primary_key=True)
    tenant_id: Optional[int] = Field(default=None, foreign_key="user.id", index=True)  # seller/requester scope; NULL=system
    title: str = ""
    description: str = ""          # internal only — NEVER shown to a requester
    type: str = "other"            # review_new_request | follow_up_buyer | follow_up_seller | follow_up_supplier |
    # review_reply | requester_action_required | admin_action_required | data_enrichment | replace_invalid_contact |
    # review_potential_duplicate | prepare_quote | approve_quote | missing_document | deliver_result |
    # failed_system_job | overdue_request | other
    status: str = Field(default="open", index=True)   # open | in_progress | waiting | completed | dismissed
    priority: str = "normal"       # low | normal | high | urgent (default normal; never auto-Urgent from overdue)
    assigned_admin_id: Optional[int] = Field(default=None, foreign_key="user.id", index=True)
    created_by: Optional[int] = Field(default=None, foreign_key="user.id")   # NULL = system-created
    source: str = "manual"         # manual | automatic
    visibility: str = "internal"   # internal | requester_visible (the public half lives in SellerUpdate)
    waiting_on: str = ""           # ""|buyer|seller|supplier|requester|internal|service_provider|system
    related_request_id: Optional[int] = Field(default=None, foreign_key="servicerequest.id", index=True)
    related_company_id: Optional[int] = Field(default=None, foreign_key="company.id")
    related_lead_id: Optional[int] = Field(default=None, foreign_key="lead.id", index=True)
    related_outreach_id: Optional[int] = Field(default=None, foreign_key="outreach.id")
    related_quote_id: Optional[int] = Field(default=None, foreign_key="quote.id")
    related_deal_id: Optional[int] = Field(default=None, foreign_key="deal.id")
    related_seller_update_id: Optional[int] = Field(default=None, foreign_key="sellerupdate.id")
    parent_id: Optional[int] = Field(default=None, foreign_key="workitem.id")
    idempotency_key: str = Field(default="", index=True)   # de-dups automatic items (partial-unique over OPEN)
    condition_version: str = ""    # identifies the underlying-condition INSTANCE+version; once a task for a
    # given (idempotency_key, condition_version) is dispositioned, sync never recreates it UNTIL the condition
    # materially changes (a new version) — so complete/dismiss is durable, not re-opened on the next sync.
    inferred: bool = False         # True = migration/backfill-seeded, not an observed event
    due_at: Optional[datetime] = None
    started_at: Optional[datetime] = None
    completed_at: Optional[datetime] = None
    resolved_by: Optional[int] = Field(default=None, foreign_key="user.id")
    resolution_note: str = ""
    dismissed_reason: str = ""
    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)


class RequestStatusEvent(SQLModel, table=True):
    """Authoritative history of a ServiceRequest's workflow_status changes (who/when/why). The legacy
    ServiceRequest.status keeps its exact meaning and drivers; this records the richer Phase-3 workflow."""
    id: Optional[int] = Field(default=None, primary_key=True)
    request_id: int = Field(foreign_key="servicerequest.id", index=True)
    from_status: str = ""          # workflow_status before (submitted|under_review|approved|in_progress|...)
    to_status: str = ""            # workflow_status after
    from_legacy: str = ""          # legacy ServiceRequest.status before (for audit completeness)
    to_legacy: str = ""            # legacy ServiceRequest.status after
    actor_id: Optional[int] = Field(default=None, foreign_key="user.id")
    reason: str = ""
    inferred: bool = False         # True = backfill-seeded from legacy status, not an observed transition
    created_at: datetime = Field(default_factory=datetime.utcnow)


# ============================================================================
# Outreach, Campaigns & Email (Phase 4) — an ADMIN-ONLY sales-communication
# layer. Strictly additive: the Outreach event spine, MailAccount and Lead
# stay the source of truth; these tables ORGANIZE campaigns, sequences,
# templates, suppression and bounces around them. Two-way confidentiality is
# enforced in the service layer (buyers never learn the seller; sellers never
# learn the buyer). Recipients ALWAYS come from the Trade Network — a Campaign
# is never an independent contact database.
# ============================================================================


class Campaign(SQLModel, table=True):
    """One organized outreach effort, always linked to a meaningful context (request/product/segment/…).
    tenant_id = the seller the outreach is FOR (confidentiality scope; NULL = internal Go4it initiative).
    Sends go ONLY from an admin_owned mailbox. Never hard-deleted once it has message history."""
    id: Optional[int] = Field(default=None, primary_key=True)
    tenant_id: Optional[int] = Field(default=None, foreign_key="user.id", index=True)  # seller scope / NULL=internal
    name: str = ""
    context_kind: str = "request"   # request|product|market|segment|research|internal
    request_id: Optional[int] = Field(default=None, foreign_key="servicerequest.id", index=True)
    product_id: Optional[int] = Field(default=None, foreign_key="product.id")
    category: str = ""
    target_countries: str = ""      # CSV of ISO2 (structured facet, not free text)
    segment_ref: str = ""           # Trade Network saved-segment key/query the audience was built from
    owner_id: Optional[int] = Field(default=None, foreign_key="user.id")   # admin owner
    mailbox_id: Optional[int] = Field(default=None, foreign_key="mailaccount.id")  # sending mailbox (admin_owned)
    status: str = Field(default="draft", index=True)
    # draft|ready_for_review|scheduled|running|paused|completed|cancelled|archived
    timezone: str = "UTC"
    send_window_start: int = 8      # hour 0-23 (campaign timezone)
    send_window_end: int = 18
    send_days: str = "0,1,2,3,4"    # allowed weekdays (Mon=0 .. Sun=6)
    daily_limit: int = 50
    max_followups: int = 3
    sequence_version: int = 1       # the CURRENT sequence version; editing a running campaign bumps it
    stop_on_reply: bool = True
    stop_on_bounce: bool = True
    stop_on_unsubscribe: bool = True
    source_slug: str = ""           # legacy Lead.source this campaign maps to (backfill bridge)
    pause_reason: str = ""
    notes: str = ""                 # internal admin notes (never buyer/seller PII)
    inferred: bool = False          # True = backfill-seeded from legacy source grouping
    created_at: datetime = Field(default_factory=datetime.utcnow)
    scheduled_at: Optional[datetime] = None
    started_at: Optional[datetime] = None
    paused_at: Optional[datetime] = None
    completed_at: Optional[datetime] = None
    updated_at: datetime = Field(default_factory=datetime.utcnow)


class CampaignStep(SQLModel, table=True):
    """One step of a campaign's sequence, versioned. A running campaign's sequence is IMMUTABLE — editing
    creates a NEW version (new rows) so already-sent messages under the old version are preserved."""
    __table_args__ = (UniqueConstraint("campaign_id", "version", "step_index", name="uq_campaignstep_cvs"),)
    id: Optional[int] = Field(default=None, primary_key=True)
    campaign_id: int = Field(foreign_key="campaign.id", index=True)
    version: int = 1
    step_index: int = 0             # 0 = initial email, 1..N = follow-ups
    subject: str = ""
    body: str = ""                  # snapshot of the rendered-with-variables template body at version time
    template_id: Optional[int] = Field(default=None, foreign_key="emailtemplate.id")
    delay_days: int = 0             # days after the previous step
    manual_review: bool = False     # pause here for an admin to approve before sending
    status: str = "active"          # active|archived
    created_at: datetime = Field(default_factory=datetime.utcnow)


class CampaignRecipient(SQLModel, table=True):
    """A durable enrolment of ONE Trade Network company/contact into a campaign. Buyer contact detail lives
    on the linked Contact/Lead — only the structured send address is denormalized here (never into free
    text). Uniqueness on (campaign_id, contact_id) prevents duplicate enrolment."""
    __table_args__ = (UniqueConstraint("campaign_id", "contact_id", name="uq_camprcpt_campaign_contact"),)
    id: Optional[int] = Field(default=None, primary_key=True)
    campaign_id: int = Field(foreign_key="campaign.id", index=True)
    tenant_id: Optional[int] = Field(default=None, foreign_key="user.id", index=True)
    company_id: Optional[int] = Field(default=None, foreign_key="company.id", index=True)
    contact_id: Optional[int] = Field(default=None, foreign_key="contact.id")
    lead_id: Optional[int] = Field(default=None, foreign_key="lead.id", index=True)
    request_id: Optional[int] = Field(default=None, foreign_key="servicerequest.id")
    to_email: str = ""              # resolved send target (structured, from the Contact)
    sequence_version: int = 1       # the version this recipient is progressing through
    current_step: int = 0
    status: str = Field(default="pending", index=True)
    # pending|ready|sent|delivered|soft_bounced|hard_bounced|replied|positive_reply|negative_reply|
    # follow_up_later|unsubscribed|suppressed|completed|skipped
    last_sent_at: Optional[datetime] = None
    next_action_at: Optional[datetime] = None
    reply_outcome: str = ""         # mirrors the Inbox reply outcome
    suppressed: bool = False
    soft_bounce_count: int = 0
    inferred: bool = False
    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)


class EmailTemplate(SQLModel, table=True):
    """A reusable admin outreach template. tenant_id NULL = global. Only APPROVED variables render; never
    seller PII / mailbox creds / raw HTML / headers. Editing a USED template creates a new version."""
    id: Optional[int] = Field(default=None, primary_key=True)
    tenant_id: Optional[int] = Field(default=None, foreign_key="user.id", index=True)
    name: str = ""
    purpose: str = ""
    product: str = ""
    category: str = ""
    country: str = ""
    language: str = "en"
    subject: str = ""
    body: str = ""
    allowed_vars: str = ""          # CSV of approved variable names
    status: str = "active"          # active|archived
    version: int = 1
    created_by: Optional[int] = Field(default=None, foreign_key="user.id")
    updated_by: Optional[int] = Field(default=None, foreign_key="user.id")
    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)


class Suppression(SQLModel, table=True):
    """Central do-not-contact list. Every send path checks this immediately before sending. scope 'platform'
    applies to ALL tenants (enforced internally without revealing which tenant created it); scope 'tenant'
    applies within one seller. Addresses are never deleted — suppression is durable."""
    id: Optional[int] = Field(default=None, primary_key=True)
    email_normalized: str = Field(default="", index=True)
    email_hash: str = ""            # optional hashed lookup value
    reason: str = ""                # hard_bounce|persistent_soft|unsubscribe|spam_complaint|manual|legal
    scope: str = "platform"         # platform|tenant
    tenant_id: Optional[int] = Field(default=None, foreign_key="user.id", index=True)  # set when scope=tenant
    source_event: str = ""          # e.g. "outreach:123" | "bounce:45"
    note: str = ""
    suppressed_by: Optional[int] = Field(default=None, foreign_key="user.id")
    review_at: Optional[datetime] = None
    active: bool = True
    created_at: datetime = Field(default_factory=datetime.utcnow)


class BounceRecord(SQLModel, table=True):
    """Durable per-address bounce history (never deleted). Aggregates repeated bounces on one address with a
    count and first/latest timestamps, the classification, and the suppression + replacement decisions."""
    id: Optional[int] = Field(default=None, primary_key=True)
    email_normalized: str = Field(default="", index=True)
    tenant_id: Optional[int] = Field(default=None, foreign_key="user.id", index=True)
    company_id: Optional[int] = Field(default=None, foreign_key="company.id")
    contact_id: Optional[int] = Field(default=None, foreign_key="contact.id")
    lead_id: Optional[int] = Field(default=None, foreign_key="lead.id")
    mailbox_id: Optional[int] = Field(default=None, foreign_key="mailaccount.id")
    campaign_id: Optional[int] = Field(default=None, foreign_key="campaign.id")
    outreach_id: Optional[int] = Field(default=None, foreign_key="outreach.id")
    bounce_type: str = "unknown"    # hard|soft|blocked|mailbox_full|domain_failure|policy|spam_complaint|unknown
    smtp_status: str = ""
    enhanced_status: str = ""       # e.g. 5.1.1
    diagnostic: str = ""
    bounce_count: int = 1
    suppression_decision: str = ""  # suppressed|retry|manual_review
    replacement_status: str = "none"  # none|pending|found|verified|active
    first_bounce_at: datetime = Field(default_factory=datetime.utcnow)
    last_bounce_at: datetime = Field(default_factory=datetime.utcnow)
    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)


class OutreachControl(SQLModel, table=True):
    """Single-row global outreach kill switch. When paused_all is True, EVERY send path refuses new sends
    immediately (without deleting scheduled work). Admin-toggled + audited."""
    id: Optional[int] = Field(default=None, primary_key=True)
    paused_all: bool = False
    updated_by: Optional[int] = Field(default=None, foreign_key="user.id")
    updated_at: datetime = Field(default_factory=datetime.utcnow)


class CampaignSend(SQLModel, table=True):
    """Durable per-step send lifecycle (Phase 4 hardening) — the crash-safe unit of a campaign send. Exactly
    ONE row per (campaign, recipient, version, step) (unique index). A worker CLAIMS the row with a
    time-limited lease BEFORE touching SMTP; the Outreach event row is written only AFTER the provider
    accepts, so a crash can never leave a phantom 'sent'. A crash mid-send (status 'sending', lease expired)
    is flagged 'unknown_needs_review' rather than auto-resent — no silent duplicate. See campaign_service."""
    __table_args__ = (UniqueConstraint("campaign_id", "recipient_id", "sequence_version", "step_index",
                                       name="uq_campaignsend_crvs"),)
    id: Optional[int] = Field(default=None, primary_key=True)
    campaign_id: int = Field(foreign_key="campaign.id", index=True)
    recipient_id: int = Field(foreign_key="campaignrecipient.id", index=True)
    sequence_version: int = 1
    step_index: int = 0
    status: str = Field(default="pending", index=True)
    # pending|claimed|sending|sent|retryable|permanently_failed|unknown_needs_review
    claim_token: str = ""
    claimed_at: Optional[datetime] = None
    lease_expires_at: Optional[datetime] = None
    attempt_count: int = 0
    next_attempt_at: Optional[datetime] = None
    last_error: str = ""
    provider_message_id: str = ""
    sent_at: Optional[datetime] = None
    outreach_id: Optional[int] = Field(default=None, foreign_key="outreach.id")
    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)
