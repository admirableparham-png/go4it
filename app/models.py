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
    reliability: int = 3           # 1-5, team's MANUAL rating (only meaningful when reliability_rated=True)
    reliability_rated: bool = False  # Phase 5: False = never explicitly rated → show "Not yet rated", not stars
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
    supplier_id: Optional[int] = Field(default=None, foreign_key="supplier.id", index=True)
    active: bool = True
    updated_at: datetime = Field(default_factory=datetime.utcnow)
    updated_by: str = ""
    # --- Phase 5 canonical catalog (all additive; legacy fields above are preserved + kept in sync) ---
    sku: str = Field(default="", index=True)          # internal product code / SKU
    short_description: str = ""                        # short commercial description
    category_id: Optional[int] = Field(default=None, foreign_key="productcategory.id", index=True)
    subcategory: str = ""
    brand: str = ""
    grade: str = ""                                    # grade / purity / variant descriptor
    origin_country: str = Field(default="", index=True)
    origin_city: str = ""
    producer: str = ""                                 # producer / manufacturer
    units_per_package: float = 0
    production_capacity: str = ""                      # free text, e.g. "500 t/month"
    lead_time_days: int = 0
    shelf_life: str = ""
    storage_requirements: str = ""
    certifications: str = ""                           # comma-separated
    incoterms: str = ""                                # comma-separated available terms (EXW,FOB,CIF,...)
    status: str = "active"                             # draft | active | archived (mirrors `active`)
    completeness_score: int = 0                        # 0-100, cached; computed from field presence
    verification_status: str = "unverified"            # unverified | verified | rejected
    verified_at: Optional[datetime] = None
    verified_by: str = ""
    internal_notes: str = ""                           # ADMIN-ONLY; never shown to sellers
    created_at: datetime = Field(default_factory=datetime.utcnow)


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
    # --- Phase 5 provenance/staleness (additive; existing quote_service.fx_rate only reads `rate`) ---
    source: str = ""               # where the rate came from (manual entry, provider name, ...)
    kind: str = "manual"           # manual | live  (never present a manual/stale rate as live)
    retrieved_at: Optional[datetime] = None    # when it was entered/fetched
    expires_at: Optional[datetime] = None      # staleness threshold; past it → "Stale"
    verified_by: str = ""
    active: bool = True


# --------------------------------------------------------------------------- team & activity

class User(SQLModel, table=True):
    """A member of the trading team."""
    id: Optional[int] = Field(default=None, primary_key=True)
    email: str = Field(index=True, unique=True)
    name: str = ""
    password_hash: str = ""
    role: str = "agent"            # admin | manager | agent | viewer (LEGACY tier; Phase-10 authz is via UserProfile.role_key)
    active: bool = True
    telegram_user_id: str = ""
    created_at: datetime = Field(default_factory=datetime.utcnow)


# --------------------------------------------------------------------- Phase 10: profiles, roles, access control
class UserProfile(SQLModel, table=True):
    """One professional profile + access record per User (1:1). Additive — legacy code keeps using User.role/
    active; Phase-10 authorization resolves through `role_key` + `PermissionOverride` here. `account_class`
    ('internal' | 'seller') is the HARD boundary a seller can never cross. Seller-provided profile fields never
    grant access to buyer data."""
    id: Optional[int] = Field(default=None, primary_key=True)
    user_id: int = Field(foreign_key="user.id", index=True, unique=True)
    account_class: str = Field(default="internal", index=True)   # internal | seller
    role_key: str = Field(default="", index=True)                # RoleTemplate.key (e.g. 'founder','seller')
    scope: str = "own"                                           # platform|tenant|assigned|own|aggregate (override of template default)
    account_status: str = Field(default="active", index=True)    # active | disabled | archived
    # profile
    full_name: str = ""
    display_name: str = ""
    job_title: str = ""
    department: str = ""
    company: str = ""
    country: str = ""
    timezone: str = ""
    preferred_language: str = "en"
    phone: str = ""                # internal contact phone (NEVER a buyer's)
    avatar_url: str = ""           # metadata only (path/URL); no binary blobs here
    notification_prefs: str = ""   # JSON
    # seller-only profile facets (nullable; never expose to buyer data)
    trading_interests: str = ""    # JSON list (categories/products the seller trades)
    preferred_markets: str = ""    # JSON list
    # security timestamps
    last_login_at: Optional[datetime] = None
    password_changed_at: Optional[datetime] = None
    sessions_revoked_at: Optional[datetime] = None   # sessions issued before this are invalid (disable/critical change)
    disabled_at: Optional[datetime] = None
    disabled_by: Optional[int] = Field(default=None, foreign_key="user.id")
    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)


class RoleTemplate(SQLModel, table=True):
    """An editable bundle of permissions the founder assigns. Seeded from permissions.ROLE_TEMPLATES; system
    templates cannot be deleted. Authorization uses the resolved permission SET, never the template name."""
    id: Optional[int] = Field(default=None, primary_key=True)
    key: str = Field(index=True, unique=True)
    name: str = ""
    account_class: str = "internal"          # internal | seller
    scope_default: str = "own"
    permissions: str = ""                    # JSON list of permission keys
    is_system: bool = False                  # system templates are protected from deletion
    editable: bool = True
    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)


class PermissionOverride(SQLModel, table=True):
    """A per-user grant/deny of a single permission on top of the role template. A 'deny' always wins. A seller
    can never be granted a seller-forbidden permission (enforced in authz, not here)."""
    __table_args__ = (UniqueConstraint("user_id", "permission_key", name="uq_permoverride_user_perm"),)
    id: Optional[int] = Field(default=None, primary_key=True)
    user_id: int = Field(foreign_key="user.id", index=True)
    permission_key: str = Field(index=True)
    effect: str = "grant"                    # grant | deny
    reason: str = ""
    granted_by: Optional[int] = Field(default=None, foreign_key="user.id")
    created_at: datetime = Field(default_factory=datetime.utcnow)


class AccessAuditLog(SQLModel, table=True):
    """Immutable, append-only history of every access change (who/what/when/why). NEVER stores passwords, API
    keys, mailbox credentials or tokens."""
    id: Optional[int] = Field(default=None, primary_key=True)
    actor_id: Optional[int] = Field(default=None, foreign_key="user.id", index=True)
    target_user_id: Optional[int] = Field(default=None, foreign_key="user.id", index=True)
    action: str = Field(default="", index=True)   # role_changed|permission_granted|permission_revoked|scope_changed|account_disabled|account_enabled|account_archived|profile_updated|user_created
    field: str = ""
    before: str = ""
    after: str = ""
    reason: str = ""
    created_at: datetime = Field(default_factory=datetime.utcnow, index=True)


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
    quote_version_id: Optional[int] = Field(default=None, foreign_key="quoteversion.id")  # Phase 6: 1 deal / version
    ready_for_ops: bool = False    # Phase 6→7 handoff readiness (Phase 7 executes shipment/docs/remittance)
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
    # --- Phase 6 commercial (additive; status vocabulary expands to the 11-state workflow, see quote_workflow) ---
    current_version_id: Optional[int] = Field(default=None, foreign_key="quoteversion.id")
    viewed_at: Optional[datetime] = None        # first buyer view via a valid token (never set on a GET)


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
    # LEGACY: buyer identities handed INTO the seller's own account (retired scripts/deliver_request.py). It stays 0
    # under confidential delivery — the buyers Go4it works for a seller are counted by
    # pipeline.request_funnel(...)["total_prospects"]; load_managed_buyers.py must never set this.
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
    smtp_password_enc: str = ""     # Fernet-encrypted app password (dedicated credential key — see outreach.py)
    cred_enc_version: int = 1        # encryption-scheme version of the stored credentials (forward-compat)
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
    # --- Phase 11: the buyer-facing footer every campaign email carries (the legal sender behind this From) ---
    sender_company: str = ""        # e.g. the registered company name shown to buyers
    postal_address: str = ""        # physical postal address (required in commercial email: CASL / EU)


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
    related_product_id: Optional[int] = Field(default=None, foreign_key="product.id")  # Phase 5
    related_contract_id: Optional[int] = Field(default=None, foreign_key="contract.id")  # Phase 6
    related_operation_case_id: Optional[int] = Field(default=None, foreign_key="operationcase.id")  # Phase 7
    related_shipment_id: Optional[int] = Field(default=None, foreign_key="shipment.id")              # Phase 7
    related_payment_id: Optional[int] = Field(default=None, foreign_key="paymentmilestone.id")       # Phase 7
    related_exception_id: Optional[int] = Field(default=None, foreign_key="operationalexception.id") # Phase 7
    related_opportunity_id: Optional[int] = Field(default=None, foreign_key="opportunity.id")        # Phase 8
    related_alert_id: Optional[int] = Field(default=None, foreign_key="intelalert.id")               # Phase 8
    related_conversation_id: Optional[int] = Field(default=None, foreign_key="aiconversation.id")    # Phase 9
    related_proposal_id: Optional[int] = Field(default=None, foreign_key="aiactionproposal.id")      # Phase 9
    related_automation_id: Optional[int] = Field(default=None, foreign_key="automationrule.id")      # Phase 9
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
    bounce_baseline: str = ""       # Phase 11: "hard:sent" counts when last (re)started — the bounce breaker judges
                                    # only what was sent since, so a resumed campaign isn't re-paused by old bounces
    warmup_plan: str = ""           # Phase 12: daily limits by sending day, e.g. "10,20,35,50" ("" = manual limit)
    warmup_checked_on: str = ""     # Phase 12: YYYY-MM-DD (UTC) of the warm-up ramp's last daily decision
    local_hours: str = ""           # Phase 13: e.g. "09:00-11:00,14:00-16:00" = send in each BUYER's local hours
    #                                 (country/city time zone + working days); "" = the UTC window/days above
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
    body_html: str = ""             # Phase 11: optional admin-authored HTML design (sanitized); `body` stays the text part
    attachment_path: str = ""       # Phase 11: a PDF shipped under campaigns/ (e.g. the price list), attached to the email
    plain_text_only: bool = False   # Phase 11: send like a hand-typed email (no HTML part) — fewer "Promotions" tabs
    list_unsubscribe: bool = True   # Phase 11: send the List-Unsubscribe header (Gmail reads it as bulk mail)


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


class InboundSeen(SQLModel, table=True):
    """Phase 11 — the IMAP poller's processed-message ledger. The poller reads the mailbox READ-ONLY (a person's
    unread mail stays unread), so it remembers what it already handled by Message-ID instead of the \\Seen flag."""
    __table_args__ = (UniqueConstraint("mailbox", "message_key", name="uq_inboundseen_mailbox_key"),)
    id: Optional[int] = Field(default=None, primary_key=True)
    mailbox: str = Field(default="", index=True)
    message_key: str = ""           # the Message-ID, or a hash of date|from|subject when there is none
    outcome: str = ""               # threaded|unmatched|ignored|unsubscribed|duplicate|bounced|baseline|error
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
    # Durable RFC Message-ID generated + persisted BEFORE SMTP submission — the reply-correlation key. Reused
    # verbatim on a safe retry, never regenerated for the same step. Distinct from the provider's own id.
    rfc_message_id: str = Field(default="", index=True)
    provider_message_id: str = ""
    sent_at: Optional[datetime] = None
    outreach_id: Optional[int] = Field(default=None, foreign_key="outreach.id")
    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)


# ========================================================================================
# Phase 5 — Products, Catalogs, Suppliers & Pricing (admin-only canonical layer, additive)
# ========================================================================================

class ProductCategory(SQLModel, table=True):
    """A structured product category (replaces unrestricted free-text Product.category). Hierarchical
    (parent_id), archivable, and mergeable (merged_into_id points at the survivor — reversible, never deletes
    product history). tenant_id NULL = global admin catalog."""
    __table_args__ = (UniqueConstraint("tenant_id", "name_normalized", "parent_id",
                                       name="uq_productcategory_scope"),)
    id: Optional[int] = Field(default=None, primary_key=True)
    tenant_id: Optional[int] = Field(default=None, foreign_key="user.id", index=True)  # NULL = global
    name: str = ""
    name_normalized: str = Field(default="", index=True)
    slug: str = Field(default="", index=True)
    parent_id: Optional[int] = Field(default=None, foreign_key="productcategory.id", index=True)
    spec_fields: str = ""          # JSON list of optional category-specific spec field names
    status: str = "active"         # active | archived
    merged_into_id: Optional[int] = Field(default=None, foreign_key="productcategory.id")  # survivor (reversible)
    inferred: bool = False         # True = created by the conservative backfill from legacy free text
    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)


class ProductCategoryAlias(SQLModel, table=True):
    """An alternate spelling that maps to a canonical category — used for CSV import + research matching so
    inconsistent inbound category text resolves deterministically instead of spawning duplicates."""
    __table_args__ = (UniqueConstraint("alias_normalized", name="uq_prodcatalias_norm"),)
    id: Optional[int] = Field(default=None, primary_key=True)
    alias_normalized: str = Field(default="", index=True)
    category_id: int = Field(foreign_key="productcategory.id", index=True)
    created_at: datetime = Field(default_factory=datetime.utcnow)


class ProductVariant(SQLModel, table=True):
    """A concrete variant/grade of a canonical product (e.g. purity 99.99% vs 99.9%), each with its own SKU
    and optional price/MOQ. Additive — a product with no variants still prices off its base fields."""
    id: Optional[int] = Field(default=None, primary_key=True)
    product_id: int = Field(foreign_key="product.id", index=True)
    name: str = ""
    grade: str = ""
    sku: str = ""
    attributes: str = ""           # JSON dict of variant-specific spec values
    base_price: float = 0
    currency: str = "USD"
    min_order_qty: float = 0
    active: bool = True
    created_at: datetime = Field(default_factory=datetime.utcnow)


class ProductSupplier(SQLModel, table=True):
    """The canonical product↔supplier link. `company_id` points at the Trade Network Company that holds the
    supplier role (CompanyRole role='supplier') — NOT a separate contact store. Uniqueness on
    (product_id, company_id) prevents duplicate links. Supplier contacts/commercial detail stay admin-only."""
    __table_args__ = (UniqueConstraint("product_id", "company_id", name="uq_productsupplier_pc"),)
    id: Optional[int] = Field(default=None, primary_key=True)
    product_id: int = Field(foreign_key="product.id", index=True)
    company_id: int = Field(foreign_key="company.id", index=True)
    supplier_sku: str = ""
    supplier_price: float = 0
    currency: str = "USD"
    min_order_qty: float = 0
    lead_time_days: int = 0
    is_primary: bool = False
    verified: bool = False
    notes: str = ""                # ADMIN-ONLY
    inferred: bool = False         # True = created by the backfill from legacy Product.supplier_id
    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)


class CostRate(SQLModel, table=True):
    """A structured, reusable, time-bounded cost/rate component for the pricing calculator (the new layer;
    legacy RateCard/CostParam are untouched so existing quotes are unaffected). Expired rates are never used
    silently — the calculator surfaces them. tenant_id NULL = global."""
    id: Optional[int] = Field(default=None, primary_key=True)
    tenant_id: Optional[int] = Field(default=None, foreign_key="user.id", index=True)  # NULL = global
    name: str = ""
    rate_type: str = Field(default="", index=True)
    # base_price|packaging|inland_freight|export_clearance|coo|inspection|documentation|insurance|
    # intl_freight|import_clearance|duty|tax|finance|fx_adj|operational_fee|margin
    origin: str = ""
    destination: str = ""
    transport_mode: str = ""       # road|sea|air|rail|""
    carrier: str = ""
    currency: str = "USD"
    unit_basis: str = "per_shipment"   # per_unit|per_tonne|per_truck|per_shipment|pct
    amount: float = 0
    min_charge: float = 0
    capacity_assumption: str = ""
    valid_from: Optional[datetime] = None
    valid_until: Optional[datetime] = None
    source: str = ""
    confidence: str = "unverified"     # unverified|estimated|quoted|contracted
    status: str = "active"             # active|expired (expired is never auto-applied)
    internal_notes: str = ""           # ADMIN-ONLY
    created_by: str = ""
    updated_by: str = ""
    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)


class ProductPriceVersion(SQLModel, table=True):
    """An IMMUTABLE landed-price calculation for a product at a given Incoterm/route. Never rewritten — to
    revise, an admin DUPLICATES it to a new version (supersedes_id). Freezes the full breakdown, the cost
    components used, the FX snapshot (with source+timestamp), the rate validity, and both margin% and markup%
    so a historical calculation reproduces identically. Admin-only."""
    id: Optional[int] = Field(default=None, primary_key=True)
    product_id: int = Field(foreign_key="product.id", index=True)
    tenant_id: Optional[int] = Field(default=None, foreign_key="user.id", index=True)
    version: int = 1
    incoterm: str = "EXW"
    origin: str = ""
    destination: str = ""
    transport_mode: str = ""
    currency: str = "USD"
    quantity: float = 1
    weight_kg_per_unit: float = 0
    unit_basis: str = "per_unit"
    inputs: str = ""               # JSON: the resolved cost components + assumptions used
    breakdown: str = ""            # JSON: list of {label, basis, amount, currency}
    excluded_costs: str = ""       # JSON: components deliberately Not-included/Required (never guessed)
    unit_price: float = 0
    total_price: float = 0
    cost_total: float = 0
    margin_pct: float = 0          # margin / price   (distinct from markup)
    markup_pct: float = 0          # margin / cost
    fx_snapshot: str = ""          # JSON: {base, quote, rate, source, kind, retrieved_at}
    rate_valid_until: Optional[datetime] = None
    status: str = Field(default="draft", index=True)  # draft|needs_review|approved|expired|archived
    supersedes_id: Optional[int] = Field(default=None, foreign_key="productpriceversion.id")
    notes: str = ""
    created_by: str = ""
    approved_by: str = ""
    approved_at: Optional[datetime] = None
    created_at: datetime = Field(default_factory=datetime.utcnow)


class ProductDocument(SQLModel, table=True):
    """A private admin-managed product file (datasheet, certificate, lab report, product/packaging image,
    spec, commercial doc). Stored under PRODUCT_FILES_DIR (outside /static); admin-download-gated; archived,
    never destructively deleted. Seller access ONLY when explicitly published as a seller-safe deliverable."""
    id: Optional[int] = Field(default=None, primary_key=True)
    product_id: int = Field(foreign_key="product.id", index=True)
    doc_type: str = ""             # datasheet|certificate|lab_report|product_image|packaging_image|spec|commercial
    file_path: str = ""            # relative path under PRODUCT_FILES_DIR (<product_id>/<doc_id>_<safe_name>)
    original_filename: str = ""    # metadata only — never used for the on-disk name
    content_type: str = ""
    size_bytes: int = 0
    title: str = ""
    status: str = "active"         # active | archived
    quarantine: str = "quarantined"  # quarantined | scanned — admin-only until scanned (no auto-publish)
    seller_safe: bool = False      # True ONLY once published to a specific request as a seller-safe deliverable
    uploaded_by: str = ""
    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)


class CatalogGenerationJob(SQLModel, table=True):
    """One versioned Catalog Studio generation for a product. Runs as a bounded background job; a failure
    opens a work item and never blocks other workers. Stores the generated PDF privately, records the
    provider (builtin|higgs) + generation date, and retains the source ProductPriceVersion reference. Only
    APPROVED product fields are used; contact is Go4it (never supplier/buyer)."""
    id: Optional[int] = Field(default=None, primary_key=True)
    product_id: int = Field(foreign_key="product.id", index=True)
    tenant_id: Optional[int] = Field(default=None, foreign_key="user.id", index=True)
    version: int = 1
    status: str = Field(default="draft", index=True)  # draft|generating|needs_review|approved|failed|archived
    provider: str = "builtin"      # builtin (Playwright HTML->PDF) | higgs (not configured)
    provider_job_id: str = ""
    price_version_id: Optional[int] = Field(default=None, foreign_key="productpriceversion.id")
    template: str = "onepager"
    params: str = ""               # JSON: the APPROVED fields snapshot sent to the generator (no contacts/margins)
    file_path: str = ""            # relative path under PRODUCT_FILES_DIR (private PDF)
    error: str = ""
    seller_safe: bool = False      # approved catalog intentionally published as a seller-safe deliverable
    generated_by: str = ""
    generated_at: Optional[datetime] = None
    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)


# ========================================================================================
# Phase 6 — Commercial: Quotes, Contracts & Deals (admin-only; additive)
# ========================================================================================

class QuoteVersion(SQLModel, table=True):
    """An IMMUTABLE snapshot of a quote's full commercial terms at a point in time (1:1 with a Quote row —
    the legacy model already creates a separate Quote per version). Once approved/sent/accepted/expired it is
    NEVER edited in place; a revision duplicates it into a new draft. Future Product/CostRate/FX changes never
    alter a historical version. Buyer-facing; no internal margin/cost text is stored in buyer-shown fields."""
    id: Optional[int] = Field(default=None, primary_key=True)
    quote_id: int = Field(foreign_key="quote.id", index=True)
    version: int = 1
    status: str = Field(default="draft", index=True)   # see quote_workflow.STATUSES
    product_id: Optional[int] = Field(default=None, foreign_key="product.id")
    product_snapshot: str = ""     # JSON: identity + product version + description + spec + origin + packaging + lead_time
    quantity: float = 0
    unit: str = ""
    unit_price: float = 0
    currency: str = "USD"
    incoterm: str = "DAP"
    origin: str = ""
    destination: str = ""
    payment_terms: str = ""
    included_costs: str = ""       # JSON
    excluded_costs: str = ""       # JSON (Not included / Required — never guessed)
    total: float = 0
    margin_pct: float = 0          # INTERNAL — never rendered buyer-facing
    markup_pct: float = 0          # INTERNAL
    fx_snapshot: str = ""          # JSON incl. source + timestamp
    params_snapshot: str = ""      # JSON
    pricing_snapshot: str = ""     # JSON: the frozen prefill_for_quote result
    options: str = ""              # JSON: [{name, incoterm, included, excluded, unit_price, total}]
    commercial_text: str = ""      # approved buyer-facing text
    validity_at: Optional[datetime] = None
    content_hash: str = ""         # sha256 of the frozen terms (immutability anchor)
    pdf_document_id: Optional[int] = Field(default=None, foreign_key="quotedocument.id")
    approved_by: str = ""
    approved_at: Optional[datetime] = None
    approval_note: str = ""
    sent_at: Optional[datetime] = None
    supersedes_id: Optional[int] = Field(default=None, foreign_key="quoteversion.id")
    inferred: bool = False         # created by the conservative backfill from an existing Quote row
    created_by: str = ""
    created_at: datetime = Field(default_factory=datetime.utcnow)


class QuoteLineItem(SQLModel, table=True):
    """A line on a quote version (single-line default; supports multi-line quotes later)."""
    id: Optional[int] = Field(default=None, primary_key=True)
    quote_version_id: int = Field(foreign_key="quoteversion.id", index=True)
    product_id: Optional[int] = Field(default=None, foreign_key="product.id")
    description: str = ""
    spec: str = ""
    quantity: float = 0
    unit: str = ""
    unit_price: float = 0
    currency: str = "USD"
    incoterm: str = ""
    line_total: float = 0


class QuoteStatusEvent(SQLModel, table=True):
    """Authoritative quote status history — every transition, actor + reason. Never mutated."""
    id: Optional[int] = Field(default=None, primary_key=True)
    quote_id: int = Field(foreign_key="quote.id", index=True)
    quote_version_id: Optional[int] = Field(default=None, foreign_key="quoteversion.id")
    from_status: str = ""
    to_status: str = ""
    actor_id: Optional[int] = Field(default=None, foreign_key="user.id")
    actor_kind: str = "admin"      # admin | buyer | system
    reason: str = ""
    created_at: datetime = Field(default_factory=datetime.utcnow)


class QuoteApproval(SQLModel, table=True):
    """The approval record for a quote version — who, when, note + the checklist results (JSON)."""
    id: Optional[int] = Field(default=None, primary_key=True)
    quote_version_id: int = Field(foreign_key="quoteversion.id", index=True)
    approved_by: str = ""
    approved_at: datetime = Field(default_factory=datetime.utcnow)
    note: str = ""
    checks: str = ""               # JSON: {check_name: ok/fail/reason}


class QuoteAccessToken(SQLModel, table=True):
    """A secure buyer-portal token, HASHED at rest (sha256), scoped to ONE quote version, expiring, revocable,
    rotatable. The raw token is shown once (in the send link) and never stored or logged."""
    __table_args__ = (UniqueConstraint("token_hash", name="uq_quoteaccesstoken_hash"),)
    id: Optional[int] = Field(default=None, primary_key=True)
    quote_id: int = Field(foreign_key="quote.id", index=True)
    quote_version_id: int = Field(foreign_key="quoteversion.id", index=True)
    token_hash: str = Field(default="", index=True)
    expires_at: Optional[datetime] = None
    revoked: bool = False
    consumed_at: Optional[datetime] = None   # single-use: the FIRST exchange consumes the link (atomic)
    created_by: str = ""
    last_viewed_at: Optional[datetime] = None
    view_count: int = 0
    created_at: datetime = Field(default_factory=datetime.utcnow)


class PortalSession(SQLModel, table=True):
    """A SERVER-SIDE buyer-portal session. The browser cookie carries ONLY the opaque `sid`; the sensitive
    state (quote version, token hash, expiry, revocation, CSRF) lives HERE, never in a client-readable cookie.
    Created by the one-time link exchange; scoped to one quote version; short-lived + revocable."""
    __table_args__ = (UniqueConstraint("sid", name="uq_portalsession_sid"),)
    id: Optional[int] = Field(default=None, primary_key=True)
    sid: str = Field(default="", index=True)        # opaque random id stored in the cookie
    quote_id: int = Field(foreign_key="quote.id", index=True)
    quote_version_id: int = Field(foreign_key="quoteversion.id")
    token_id: Optional[int] = Field(default=None, foreign_key="quoteaccesstoken.id")
    token_hash: str = ""                            # the access-token hash this session was opened from
    csrf: str = ""
    expires_at: Optional[datetime] = None
    revoked: bool = False
    created_at: datetime = Field(default_factory=datetime.utcnow)


class QuoteDocument(SQLModel, table=True):
    """An immutable generated quote PDF stored privately (QUOTE_FILES_DIR), with its sha256 hash. Admin
    download-gated; buyer access only through the token portal. Never overwritten."""
    id: Optional[int] = Field(default=None, primary_key=True)
    quote_version_id: int = Field(foreign_key="quoteversion.id", index=True)
    file_path: str = ""
    sha256: str = ""
    content_type: str = "application/pdf"
    size_bytes: int = 0
    status: str = "active"         # active | archived
    created_by: str = ""
    created_at: datetime = Field(default_factory=datetime.utcnow)


# ---------------------------------------------------------------- Contracts (Checkpoint B)

class Contract(SQLModel, table=True):
    """A contract header. Admins EXPLICITLY choose the type + parties + side (never auto-assumed). Buyer-side
    and supplier-side contracts are separate documents (confidentiality by default)."""
    id: Optional[int] = Field(default=None, primary_key=True)
    tracking_code: str = Field(default="", index=True)
    contract_type: str = "buyer_sales"   # buyer_sales|supplier_purchase|service|nda|amendment|other
    side: str = "buyer"                   # buyer | supplier | internal
    tenant_id: Optional[int] = Field(default=None, foreign_key="user.id", index=True)
    country: str = ""
    jurisdiction: str = ""
    category: str = ""
    product_id: Optional[int] = Field(default=None, foreign_key="product.id")
    quote_id: Optional[int] = Field(default=None, foreign_key="quote.id", index=True)
    quote_version_id: Optional[int] = Field(default=None, foreign_key="quoteversion.id")
    deal_id: Optional[int] = Field(default=None, foreign_key="deal.id", index=True)
    request_id: Optional[int] = Field(default=None, foreign_key="servicerequest.id", index=True)  # concierge link
    company_id: Optional[int] = Field(default=None, foreign_key="company.id")   # the counterparty company
    status: str = Field(default="draft", index=True)   # see contract_service.STATUSES
    current_version_id: Optional[int] = Field(default=None, foreign_key="contractversion.id")
    owner_id: Optional[int] = Field(default=None, foreign_key="user.id")
    expires_at: Optional[datetime] = None
    notes: str = ""                # ADMIN-ONLY
    created_by: str = ""
    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)


class ContractVersion(SQLModel, table=True):
    """IMMUTABLE once approved/sent/signed. A revision or amendment is a new linked version/addendum — signed
    versions are never overwritten."""
    id: Optional[int] = Field(default=None, primary_key=True)
    contract_id: int = Field(foreign_key="contract.id", index=True)
    version: int = 1
    status: str = Field(default="draft", index=True)
    contract_type: str = ""
    parties_snapshot: str = ""     # JSON: the selected legal parties (buyer OR supplier side, never both external)
    terms: str = ""                # JSON / text
    payment_terms: str = ""
    delivery_terms: str = ""
    validity_at: Optional[datetime] = None
    approved_clauses: str = ""     # JSON
    template_id: Optional[int] = Field(default=None, foreign_key="contracttemplate.id")
    document_hash: str = ""        # sha256 of the generated PDF
    signature_state: str = "unsigned"   # unsigned | manual_pending | signed | declined
    approved_by: str = ""
    approved_at: Optional[datetime] = None
    supersedes_id: Optional[int] = Field(default=None, foreign_key="contractversion.id")
    is_amendment: bool = False
    inferred: bool = False
    created_by: str = ""
    created_at: datetime = Field(default_factory=datetime.utcnow)


class ContractParty(SQLModel, table=True):
    """A party on a contract version (explicitly chosen by the admin)."""
    id: Optional[int] = Field(default=None, primary_key=True)
    contract_id: int = Field(foreign_key="contract.id", index=True)
    role: str = ""                 # buyer | supplier | go4it | witness
    company_id: Optional[int] = Field(default=None, foreign_key="company.id")
    name: str = ""
    signatory_name: str = ""
    signatory_email: str = ""
    created_at: datetime = Field(default_factory=datetime.utcnow)


class ContractStatusEvent(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    contract_id: int = Field(foreign_key="contract.id", index=True)
    contract_version_id: Optional[int] = Field(default=None, foreign_key="contractversion.id")
    from_status: str = ""
    to_status: str = ""
    actor_id: Optional[int] = Field(default=None, foreign_key="user.id")
    reason: str = ""
    created_at: datetime = Field(default_factory=datetime.utcnow)


class ContractTemplate(SQLModel, table=True):
    """An admin-managed contract template. Variables use an allowlist; unknown/unresolved placeholders are
    rejected at generation. Archived, never destructively deleted. NOT legal advice."""
    id: Optional[int] = Field(default=None, primary_key=True)
    name: str = ""
    contract_type: str = "buyer_sales"
    side: str = "buyer"
    country: str = ""
    category: str = ""
    language: str = "en"
    body: str = ""                 # template text with {{allowlisted}} variables
    allowed_vars: str = ""         # JSON list of permitted variable names
    version: int = 1
    status: str = "active"         # active | archived
    approval_status: str = "draft" # draft | approved
    created_by: str = ""
    updated_by: str = ""
    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)


class ContractDocument(SQLModel, table=True):
    """A private contract file: either a Go4it-generated PDF or an UPLOADED signed copy. Uploaded copies are
    quarantined + admin-only until scan-cleared, linked to the exact version, hashed, archive-not-delete, and
    never overwrite the generated original."""
    id: Optional[int] = Field(default=None, primary_key=True)
    contract_id: int = Field(foreign_key="contract.id", index=True)
    contract_version_id: int = Field(foreign_key="contractversion.id", index=True)
    kind: str = "generated"        # generated | signed_upload
    file_path: str = ""
    original_filename: str = ""
    content_type: str = ""
    size_bytes: int = 0
    sha256: str = ""
    quarantine: str = "quarantined"  # quarantined | scanned (uploads); generated PDFs default scanned
    status: str = "active"         # active | archived
    uploaded_by: str = ""
    created_at: datetime = Field(default_factory=datetime.utcnow)


class SignatureEvent(SQLModel, table=True):
    """A recorded signature action. A typed name is manual tracking only — NOT a legally verified signature.
    Real e-signature is a future provider integration (honest 'Not configured' until then)."""
    id: Optional[int] = Field(default=None, primary_key=True)
    contract_id: int = Field(foreign_key="contract.id", index=True)
    contract_version_id: int = Field(foreign_key="contractversion.id", index=True)
    party_role: str = ""           # buyer | supplier | go4it
    method: str = "manual"         # manual | provider (provider = future e-sign)
    provider: str = ""             # "" | provider name (not configured this phase)
    provider_ref: str = ""
    signer_name: str = ""
    signer_email: str = ""
    verified: bool = False         # manual typed name is NEVER verified=True
    signed_at: Optional[datetime] = None
    created_at: datetime = Field(default_factory=datetime.utcnow)


# ============================================================================
# Operations (Phase 7) — Freight, Shipments, Documentation, Payments &
# Remittance. STRICTLY ADDITIVE: the accepted commercial Deal + its DEAL_STAGES
# stay the source of truth; these tables RECORD the operational execution that
# advances that journey from VERIFIED milestones (see app/operations.py's
# central projector). Go4it is NOT a licensed payment/remittance/customs/
# freight provider — every record here COORDINATES and TRACKS; it never claims a
# transfer/clearance/booking occurred without verified evidence. Money is stored
# as TEXT and handled only through Decimal (see app/pricing._d/_q). Two-way
# confidentiality holds: buyer identity/contact, provider identity/contact,
# internal costs and Go4it margin are admin-only and never reach a seller.
# ============================================================================


class OperationCase(SQLModel, table=True):
    """The operational umbrella for one piece of execution work. May be linked to a Deal, a ServiceRequest,
    both, or be a controlled standalone service case. A Deal may have MANY cases/shipments — never one-per-Deal.
    tenant_id = the seller the case concerns (owner-scoped, seller-safe projection only); owner_id = the admin."""
    id: Optional[int] = Field(default=None, primary_key=True)
    reference: str = Field(default="", index=True)     # OP-YYYYMM-####
    case_type: str = "standalone"  # deal | request | standalone
    deal_id: Optional[int] = Field(default=None, foreign_key="deal.id", index=True)
    request_id: Optional[int] = Field(default=None, foreign_key="servicerequest.id", index=True)
    tenant_id: Optional[int] = Field(default=None, foreign_key="user.id", index=True)  # seller scope; NULL=internal
    owner_id: Optional[int] = Field(default=None, foreign_key="user.id")   # admin owner
    status: str = "open"           # open | in_progress | on_hold | completed | cancelled
    priority: str = "normal"       # low | normal | high | urgent
    due_at: Optional[datetime] = None
    origin_country: str = ""
    dest_country: str = ""
    product_id: Optional[int] = Field(default=None, foreign_key="product.id")
    category: str = ""
    notes: str = ""                # internal only
    inferred: bool = False         # True = backfill-seeded baseline case, not an observed op
    created_by: Optional[int] = Field(default=None, foreign_key="user.id")
    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)


class FreightRequest(SQLModel, table=True):
    """A structured request for freight/shipping on a case. Critical physical facts (weight, CBM, hazardous,
    customs) are NEVER guessed — a missing critical field defaults NULL and raises a Work Queue action."""
    id: Optional[int] = Field(default=None, primary_key=True)
    reference: str = Field(default="", index=True)     # FR-YYYYMM-####
    operation_case_id: Optional[int] = Field(default=None, foreign_key="operationcase.id", index=True)
    deal_id: Optional[int] = Field(default=None, foreign_key="deal.id", index=True)
    request_id: Optional[int] = Field(default=None, foreign_key="servicerequest.id")
    tenant_id: Optional[int] = Field(default=None, foreign_key="user.id", index=True)
    cargo_description: str = ""
    quantity: str = "0"            # Decimal-as-text
    unit: str = ""
    gross_weight_kg: Optional[str] = None   # None = unknown (never guessed)
    net_weight_kg: Optional[str] = None
    volume_cbm: Optional[str] = None
    package_count: Optional[int] = None
    package_type: str = ""
    origin_address: str = ""
    origin_port: str = ""
    origin_country: str = ""
    dest_address: str = ""
    dest_port: str = ""
    dest_country: str = ""
    incoterm: str = ""
    mode: str = ""                 # road | sea | air | rail | multimodal
    cargo_ready_date: Optional[datetime] = None
    requested_delivery_date: Optional[datetime] = None
    temperature_reqs: str = ""
    hazardous: Optional[bool] = None        # None = unknown → must be resolved, never assumed False
    special_handling: str = ""
    insurance_required: Optional[bool] = None
    customs_required: Optional[bool] = None
    owner_id: Optional[int] = Field(default=None, foreign_key="user.id")
    status: str = "draft"          # draft | open | quoting | offer_selected | booked | cancelled
    internal_notes: str = ""
    created_by: Optional[int] = Field(default=None, foreign_key="user.id")
    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)


class FreightOffer(SQLModel, table=True):
    """A provider's offer against a FreightRequest — an immutable priced snapshot. The provider is a canonical
    Trade Network Company with a freight_provider role (never a separate provider DB). Costs + provider identity
    are ADMIN-ONLY. Expired offers are never silently used; selecting/replacing one is audited."""
    id: Optional[int] = Field(default=None, primary_key=True)
    freight_request_id: int = Field(foreign_key="freightrequest.id", index=True)
    provider_company_id: Optional[int] = Field(default=None, foreign_key="company.id")  # freight_provider role
    provider_name_cache: str = ""  # admin-only convenience label (never seller-facing)
    mode: str = ""
    route_summary: str = ""
    service_description: str = ""
    departure_estimate: Optional[datetime] = None
    lead_time_days: Optional[int] = None
    valid_until: Optional[datetime] = None
    currency: str = ""
    base_freight: str = "0"        # Decimal-as-text
    surcharges: str = "0"
    insurance_cost: str = "0"
    customs_cost: str = "0"
    total: str = "0"
    fx_snapshot: str = ""          # JSON (pricing fx snapshot) when currencies are combined
    included_services: str = ""
    excluded_services: str = ""
    source: str = "manual"         # manual | provider (provider = future integration)
    provider_reference: str = ""
    verification_status: str = "unverified"  # unverified | verified
    document_id: Optional[int] = Field(default=None, foreign_key="tradedocument.id")
    selection_status: str = "offered"  # offered | selected | rejected | replaced
    selected_by: Optional[int] = Field(default=None, foreign_key="user.id")
    selected_at: Optional[datetime] = None
    replaced_by_id: Optional[int] = Field(default=None, foreign_key="freightoffer.id")
    replacement_reason: str = ""
    created_by: Optional[int] = Field(default=None, foreign_key="user.id")
    created_at: datetime = Field(default_factory=datetime.utcnow)


class Shipment(SQLModel, table=True):
    """A canonical shipment record. A Deal may have several. Carrier/provider contacts + sensitive tracking
    references are ADMIN-ONLY / masked from sellers. current_milestone mirrors verified progress; it never
    advances on an estimate alone (delivery needs a DeliveryConfirmation, not a passed ETA)."""
    id: Optional[int] = Field(default=None, primary_key=True)
    reference: str = Field(default="", index=True)     # SH-YYYYMM-####
    operation_case_id: Optional[int] = Field(default=None, foreign_key="operationcase.id", index=True)
    deal_id: Optional[int] = Field(default=None, foreign_key="deal.id", index=True)
    freight_offer_id: Optional[int] = Field(default=None, foreign_key="freightoffer.id")
    tenant_id: Optional[int] = Field(default=None, foreign_key="user.id", index=True)
    mode: str = ""
    carrier_company_id: Optional[int] = Field(default=None, foreign_key="company.id")  # admin-only
    carrier_name_cache: str = ""   # admin-only label
    booking_reference: str = ""    # admin-only / masked for sellers
    container_reference: str = ""  # container/trailer/AWB/BOL — admin-only / masked
    origin: str = ""
    destination: str = ""
    cargo_summary: str = ""
    quantity: str = "0"
    weight_kg: str = "0"
    volume_cbm: str = "0"
    planned_pickup: Optional[datetime] = None
    actual_pickup: Optional[datetime] = None
    planned_departure: Optional[datetime] = None
    actual_departure: Optional[datetime] = None
    estimated_arrival: Optional[datetime] = None
    actual_arrival: Optional[datetime] = None
    delivery_date: Optional[datetime] = None
    current_milestone: str = "planning"  # planning|booked|export_cleared|in_transit|import_cleared|delivered
    tracking_source: str = "manual"      # manual | provider name; "Provider not configured" until integrated
    last_tracking_update: Optional[datetime] = None
    exception_state: str = "none"        # none | open (mirrors an active OperationalException)
    owner_id: Optional[int] = Field(default=None, foreign_key="user.id")
    status: str = "active"         # active | archived | cancelled
    created_by: Optional[int] = Field(default=None, foreign_key="user.id")
    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)


class ShipmentLeg(SQLModel, table=True):
    """One leg of a multimodal/multi-stop shipment. Legs are ordered by `sequence`; ordering is validated so an
    out-of-order event never moves the whole shipment backward."""
    id: Optional[int] = Field(default=None, primary_key=True)
    shipment_id: int = Field(foreign_key="shipment.id", index=True)
    sequence: int = 1
    mode: str = ""
    origin: str = ""
    destination: str = ""
    carrier_company_id: Optional[int] = Field(default=None, foreign_key="company.id")  # admin-only
    vehicle_reference: str = ""    # vehicle/vessel/flight — admin-only
    planned_departure: Optional[datetime] = None
    planned_arrival: Optional[datetime] = None
    actual_departure: Optional[datetime] = None
    actual_arrival: Optional[datetime] = None
    status: str = "planned"        # planned | in_progress | completed | skipped
    tracking_reference: str = ""   # admin-only / masked
    internal_notes: str = ""
    created_at: datetime = Field(default_factory=datetime.utcnow)


class ShipmentEvent(SQLModel, table=True):
    """A tracking event — manually recorded OR externally imported. External ingestion is IDEMPOTENT: a
    (source, external_event_id) pair is unique (partial index) so a replayed webhook/import never duplicates.
    The raw provider reference is stored privately; only seller_safe_summary may ever reach a seller. GPS/ETA/
    events are never invented."""
    id: Optional[int] = Field(default=None, primary_key=True)
    shipment_id: int = Field(foreign_key="shipment.id", index=True)
    leg_id: Optional[int] = Field(default=None, foreign_key="shipmentleg.id")
    event_type: str = ""           # booked|departed|arrived|export_cleared|import_cleared|delivered|exception|note
    event_at: Optional[datetime] = None
    recorded_at: datetime = Field(default_factory=datetime.utcnow)
    location: str = ""
    source: str = "manual"         # manual | import | webhook:<provider>
    external_event_id: str = Field(default="", index=True)   # unique per source (idempotent ingest)
    confidence: str = "recorded"   # recorded | verified (admin-verified)
    raw_reference: str = ""        # private provider payload/ref — NEVER seller-facing
    admin_note: str = ""
    seller_safe_summary: str = ""  # the ONLY field that may be surfaced to a seller
    created_by: Optional[int] = Field(default=None, foreign_key="user.id")


class DocumentRequirement(SQLModel, table=True):
    """A 'what document is needed, from whom, by when' record. Distinct from the stored file (TradeDocument).
    seller_action_required drives a sanitized seller document request; buyer_action_required stays internal."""
    id: Optional[int] = Field(default=None, primary_key=True)
    operation_case_id: Optional[int] = Field(default=None, foreign_key="operationcase.id", index=True)
    deal_id: Optional[int] = Field(default=None, foreign_key="deal.id", index=True)
    shipment_id: Optional[int] = Field(default=None, foreign_key="shipment.id")
    request_id: Optional[int] = Field(default=None, foreign_key="servicerequest.id")
    tenant_id: Optional[int] = Field(default=None, foreign_key="user.id", index=True)
    doc_type: str = ""             # commercial_invoice|packing_list|certificate_of_origin|bill_of_lading|...
    required_from: str = ""        # seller | buyer | provider | customs | admin
    due_date: Optional[datetime] = None
    status: str = "missing"        # missing | requested | received | approved | rejected
    document_id: Optional[int] = Field(default=None, foreign_key="tradedocument.id")
    approval_state: str = "pending"  # pending | approved | rejected
    rejection_reason: str = ""     # seller-safe reason when required_from == seller
    seller_action_required: bool = False
    buyer_action_required: bool = False   # internal only
    notes: str = ""
    created_at: datetime = Field(default_factory=datetime.utcnow)
    completed_at: Optional[datetime] = None


class TradeDocument(SQLModel, table=True):
    """A private stored trade document (generated or uploaded). Mirrors the hardened ProductDocument pattern:
    private OPERATION_FILES_DIR, generated on-disk name, sha256, quarantine-by-default, archive-not-delete,
    admin-download-gated. seller_safe=True only after an admin confirms it carries no buyer/provider PII —
    then it reaches a seller solely via the owner-scoped RequestDeliverable mechanism."""
    id: Optional[int] = Field(default=None, primary_key=True)
    operation_case_id: Optional[int] = Field(default=None, foreign_key="operationcase.id", index=True)
    shipment_id: Optional[int] = Field(default=None, foreign_key="shipment.id")
    requirement_id: Optional[int] = Field(default=None, foreign_key="documentrequirement.id")
    deal_id: Optional[int] = Field(default=None, foreign_key="deal.id")
    tenant_id: Optional[int] = Field(default=None, foreign_key="user.id", index=True)
    doc_type: str = ""
    kind: str = "upload"           # upload | generated
    uploaded_by_role: str = "admin"  # admin | seller (seller uploads to their own request/op)
    file_path: str = ""            # relative under OPERATION_FILES_DIR
    original_filename: str = ""
    content_type: str = ""
    size_bytes: int = 0
    sha256: str = ""
    # quarantined | admin_attested (human review, NOT a malware scan) | scanned_clean | infected. 'admin_attested'
    # is never displayed as "scanned"/"malware-free"; quarantined/infected docs are never publishable.
    quarantine: str = "quarantined"
    status: str = "active"         # active | archived
    seller_safe: bool = False      # admin-confirmed no-PII → publishable to the owning seller
    uploaded_by: Optional[int] = Field(default=None, foreign_key="user.id")
    created_at: datetime = Field(default_factory=datetime.utcnow)


class CustomsCase(SQLModel, table=True):
    """Coordination + tracking of an export/import customs interaction — NOT legal/customs advice. Clearance is
    never inferred from shipment movement alone. Broker contacts + buyer/importer info are ADMIN-ONLY."""
    id: Optional[int] = Field(default=None, primary_key=True)
    reference: str = Field(default="", index=True)     # CU-YYYYMM-####
    operation_case_id: Optional[int] = Field(default=None, foreign_key="operationcase.id", index=True)
    shipment_id: Optional[int] = Field(default=None, foreign_key="shipment.id", index=True)
    deal_id: Optional[int] = Field(default=None, foreign_key="deal.id")
    tenant_id: Optional[int] = Field(default=None, foreign_key="user.id", index=True)
    side: str = "export"           # export | import
    country: str = ""
    broker_company_id: Optional[int] = Field(default=None, foreign_key="company.id")  # customs_broker; admin-only
    required_documents: str = ""   # comma/JSON list
    declaration_reference: str = ""
    submitted_date: Optional[datetime] = None
    clearance_date: Optional[datetime] = None
    status: str = "not_started"    # not_started|documents_required|ready_to_submit|submitted|query_hold|cleared|rejected|cancelled
    hold_reason: str = ""
    duties_taxes: Optional[str] = None   # Decimal-as-text; only when explicitly provided
    duties_currency: str = ""
    owner_id: Optional[int] = Field(default=None, foreign_key="user.id")
    notes: str = ""
    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)


class DeliveryConfirmation(SQLModel, table=True):
    """Evidence that a shipment was delivered. A shipment NEVER becomes delivered just because the ETA passed —
    it needs one of these (provider event / proof-of-delivery doc / admin confirmation / controlled buyer
    confirmation). Damage/shortage/failed delivery must raise an OperationalException, not silently complete."""
    id: Optional[int] = Field(default=None, primary_key=True)
    shipment_id: int = Field(foreign_key="shipment.id", index=True)
    deal_id: Optional[int] = Field(default=None, foreign_key="deal.id")
    source: str = "admin"          # provider | pod_document | admin | buyer
    confirmed_at: Optional[datetime] = None
    recipient_role: str = ""       # buyer | agent | warehouse | other
    document_id: Optional[int] = Field(default=None, foreign_key="tradedocument.id")
    condition_notes: str = ""
    has_shortage: bool = False
    has_damage: bool = False
    failed: bool = False
    admin_verifier: Optional[int] = Field(default=None, foreign_key="user.id")
    created_at: datetime = Field(default_factory=datetime.utcnow)


class OperationalException(SQLModel, table=True):
    """A tracked operational problem. Has an internal description AND a separate seller_safe_description (the
    only text that may reach a seller, after sanitization). Integrates with the Work Queue without creating
    duplicate unresolved tasks."""
    id: Optional[int] = Field(default=None, primary_key=True)
    reference: str = Field(default="", index=True)     # EX-YYYYMM-####
    exc_type: str = ""             # missing_documents|document_rejected|booking_failed|provider_unresponsive|
    # tracking_stale|departure_delayed|customs_hold|customs_rejection|payment_overdue|payment_failed|
    # remittance_failure|cargo_damage|shortage|failed_delivery|system_failure|compliance_review
    severity: str = "medium"       # low | medium | high | critical
    operation_case_id: Optional[int] = Field(default=None, foreign_key="operationcase.id", index=True)
    shipment_id: Optional[int] = Field(default=None, foreign_key="shipment.id")
    deal_id: Optional[int] = Field(default=None, foreign_key="deal.id")
    tenant_id: Optional[int] = Field(default=None, foreign_key="user.id", index=True)
    owner_id: Optional[int] = Field(default=None, foreign_key="user.id")
    status: str = "open"           # open|investigating|waiting_seller|waiting_buyer|waiting_provider|resolved|dismissed
    due_date: Optional[datetime] = None
    internal_description: str = ""      # admin-only
    seller_safe_description: str = ""   # sanitized; the only text a seller may see
    resolution: str = ""
    created_by: Optional[int] = Field(default=None, foreign_key="user.id")
    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)
    resolved_at: Optional[datetime] = None


class PaymentMilestone(SQLModel, table=True):
    """Administrative payment tracking — NOT a payment processor. A milestone is confirmed only by an authorized
    admin WITH evidence/reference (never on an email/screenshot alone). Amounts are Decimal-as-text. Card
    numbers, banking passwords, crypto keys/seeds are NEVER stored. References shown to sellers are masked."""
    id: Optional[int] = Field(default=None, primary_key=True)
    reference: str = Field(default="", index=True)     # PM-YYYYMM-####
    deal_id: Optional[int] = Field(default=None, foreign_key="deal.id", index=True)
    operation_case_id: Optional[int] = Field(default=None, foreign_key="operationcase.id", index=True)
    tenant_id: Optional[int] = Field(default=None, foreign_key="user.id", index=True)
    milestone_type: str = ""       # buyer_deposit|buyer_balance|supplier_advance|supplier_balance|freight_payment|customs_payment|refund|other
    expected_amount: str = "0"     # Decimal-as-text
    currency: str = ""
    due_date: Optional[datetime] = None
    payer_role: str = ""           # buyer | supplier | go4it | provider
    payee_role: str = ""
    status: str = "planned"        # planned|awaiting|partially_received|received|payment_failed|refunded|cancelled|disputed
    confirmed_amount: str = "0"
    confirmed_date: Optional[datetime] = None
    reference_code: str = ""       # bank/payment reference — masked for sellers
    evidence_document_id: Optional[int] = Field(default=None, foreign_key="tradedocument.id")
    confirmed_by: Optional[int] = Field(default=None, foreign_key="user.id")
    seller_visible: bool = False   # True only for the milestone the owning seller is authorized to see
    internal_notes: str = ""       # admin-only (margin/other-party detail)
    created_by: Optional[int] = Field(default=None, foreign_key="user.id")
    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)


class RemittanceCase(SQLModel, table=True):
    """A coordination + tracking record for a remittance/Sarafi service request — NOT an executed transfer,
    unless a licensed provider integration is configured. No wallet seeds / private keys / banking passwords.
    Sensitive account identifiers are stored ENCRYPTED (via outreach.mail_encrypt) or as masked references. Uses
    the Phase-5 FX snapshot; manual FX is never presented as live. 'Provider integration not configured' until a
    real provider exists — no invented endpoints, no auto-initiated transfers."""
    id: Optional[int] = Field(default=None, primary_key=True)
    reference: str = Field(default="", index=True)     # RM-YYYYMM-####
    request_id: Optional[int] = Field(default=None, foreign_key="servicerequest.id", index=True)
    deal_id: Optional[int] = Field(default=None, foreign_key="deal.id")
    operation_case_id: Optional[int] = Field(default=None, foreign_key="operationcase.id", index=True)
    tenant_id: Optional[int] = Field(default=None, foreign_key="user.id", index=True)
    source_currency: str = ""
    dest_currency: str = ""
    source_amount: str = "0"       # Decimal-as-text
    expected_dest_amount: str = "0"
    fx_snapshot: str = ""          # JSON (pricing fx snapshot); never presented as live when manual
    route_method_category: str = ""  # bank | exchange_house | lc | third_country | other (category only)
    provider_company_id: Optional[int] = Field(default=None, foreign_key="company.id")  # remittance_provider; admin-only
    payer_role: str = ""
    payee_role: str = ""
    origin_country: str = ""
    dest_country: str = ""
    compliance_status: str = "not_reviewed"  # not_reviewed | in_review | cleared | rejected (reason internal)
    compliance_reason: str = ""    # INTERNAL unless an approved safe explanation is published
    account_ref_enc: str = ""      # ENCRYPTED sensitive account identifier (never plaintext)
    payment_references: str = ""   # masked references only
    expected_completion: Optional[datetime] = None
    actual_completion: Optional[datetime] = None
    fees: str = "0"
    status: str = "requested"      # requested|information_required|compliance_review|quoted|approved|awaiting_funds|processing|paid|confirmed|rejected|cancelled|failed
    owner_id: Optional[int] = Field(default=None, foreign_key="user.id")
    exception_reason: str = ""
    notes: str = ""                # admin-only
    created_by: Optional[int] = Field(default=None, foreign_key="user.id")
    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)


class Settlement(SQLModel, table=True):
    """An IMMUTABLE financial settlement snapshot for a Deal. A Deal reaches 'settled' only after its configured
    required financial milestones are complete. Corrections never edit this row — they append a
    SettlementAdjustment. Sellers never see Go4it margin, buyer payments, or unrelated costs."""
    id: Optional[int] = Field(default=None, primary_key=True)
    deal_id: int = Field(foreign_key="deal.id", index=True)
    revenue: str = "0"             # Decimal-as-text
    verified_costs: str = "0"
    supplier_proceeds: str = "0"
    operational_costs: str = "0"
    go4it_margin: str = "0"        # INTERNAL — never seller-facing
    currency: str = ""
    fx_snapshot: str = ""          # JSON; no cross-currency sum without explicit FX
    outstanding: str = "0"
    realized_margin: str = "0"     # INTERNAL
    settlement_date: Optional[datetime] = None
    approved_by: Optional[int] = Field(default=None, foreign_key="user.id")
    created_at: datetime = Field(default_factory=datetime.utcnow)


class SettlementAdjustment(SQLModel, table=True):
    """A controlled correction to a Settlement — appended, never destructive. Preserves full financial history."""
    id: Optional[int] = Field(default=None, primary_key=True)
    settlement_id: int = Field(foreign_key="settlement.id", index=True)
    field: str = ""                # which figure is adjusted
    delta: str = "0"               # Decimal-as-text signed adjustment
    currency: str = ""
    reason: str = ""
    approved_by: Optional[int] = Field(default=None, foreign_key="user.id")
    created_at: datetime = Field(default_factory=datetime.utcnow)


# ============================================================================
# Intelligence (Phase 8) — an ADMIN-ONLY analytics + demand + opportunity layer.
# STRICTLY ADDITIVE and READ-mostly over the operational tables: it never
# mutates a Lead/Quote/Deal, never exposes buyer identity/contact, internal
# pricing, provider details or Go4it margin, and never conflates a scraped lead
# or a negative reply with demand. DemandSignals are built ONLY from
# deterministic positive evidence (a confirmed positive reply, a buyer
# acceptance, a Deal, a verified RFQ/tender) and carry their provenance +
# observed/verified/derived/inferred state. Opportunities connect demand to
# Go4it supply with a transparent, VERSIONED score. Snapshots are immutable so
# a later source/weight change never silently rewrites a historical report.
# ============================================================================


class DemandSignal(SQLModel, table=True):
    """A single piece of REAL demand evidence. Never created from a scraped lead, an email open/delivery, a
    bounce, a negative/auto reply, generic directory membership or a stale tender. `dedup_key` (partial-unique)
    ensures one underlying event is counted once even when it surfaces in several places (Inbox + Outreach, an
    accepted quote + its Deal, a re-imported tender)."""
    id: Optional[int] = Field(default=None, primary_key=True)
    signal_type: str = ""          # positive_reply|buyer_requirement|rfq|quote_request|accepted_quote|
    # repeat_interest|deal|verified_tender|customs_trend|inbound_account_request|admin_market_observation
    product: str = ""
    category: str = ""
    hs_code: str = ""
    dest_country: str = ""         # destination market
    origin_pref: str = ""
    quantity: str = ""             # only when actually known — never invented
    unit: str = ""
    company_id: Optional[int] = Field(default=None, foreign_key="company.id")  # internal buyer ref (admin-only)
    lead_id: Optional[int] = Field(default=None, foreign_key="lead.id")
    source: str = ""               # provenance source type
    source_event: str = ""         # e.g. "quote_status_event:123" — the exact underlying record
    observed_at: Optional[datetime] = None
    expires_at: Optional[datetime] = None
    confidence: int = 0            # 0-100, from evidence tier (never from lead volume)
    # observed = a directly recorded event; verified = admin/integration confirmed; derived = calculated
    # deterministically from a recorded record (an accepted quote / a Deal); inferred = requires ASSUMPTIONS.
    # A signal built from a recorded accepted quote or Deal is DERIVED, never inferred.
    verification_state: str = "observed"  # observed | verified | derived | inferred
    strength: str = "weak"         # weak | moderate | strong (accepted quote / Deal = strong)
    evidence_ref: str = ""
    tenant_id: Optional[int] = Field(default=None, foreign_key="user.id")  # seller the demand may serve; NULL=global
    dedup_key: str = Field(default="", index=True)   # one row per underlying source event
    # commercial_event_key groups signals that are the SAME commercial demand event (an accepted quote and the
    # Deal created from it share it), so counting never double-counts one event as two signals.
    commercial_event_key: str = Field(default="", index=True)
    backfilled: bool = False       # True = seeded by the historical backfill (NOT an assumption — provenance kept)
    history_complete: bool = True  # False = derived from incomplete history
    inferred: bool = False         # RESERVED: True only for evidence that required assumptions (not backfill)
    created_by: Optional[int] = Field(default=None, foreign_key="user.id")
    created_at: datetime = Field(default_factory=datetime.utcnow)


class Opportunity(SQLModel, table=True):
    """A structured opportunity connecting demand to Go4it's supply capabilities. Carries a TRANSPARENT, versioned
    score (breakdown stored). Never auto-contacts a buyer or seller. Buyer identity is never exposed to sellers."""
    id: Optional[int] = Field(default=None, primary_key=True)
    reference: str = Field(default="", index=True)   # OPP-YYYYMM-####
    title: str = ""
    product: str = ""
    category: str = ""
    hs_code: str = ""
    dest_market: str = ""
    estimated_size: str = ""       # Decimal-as-text, ONLY when real data supports it
    size_currency: str = ""
    confidence: int = 0            # 0-100 (lowered by missing evidence)
    freshness_at: Optional[datetime] = None   # newest supporting signal
    competition: str = ""          # low|medium|high|unknown (only when supported)
    feasibility: str = "unknown"   # operational feasibility
    missing_info: str = ""         # JSON list of what's missing
    owner_id: Optional[int] = Field(default=None, foreign_key="user.id")
    tenant_id: Optional[int] = Field(default=None, foreign_key="user.id")
    status: str = "new"            # new|needs_research|needs_supply|ready_for_review|approved|monitoring|
    # pursuing|converted|rejected|expired|archived
    recommended_action: str = ""
    score: int = 0
    score_version: str = ""
    score_breakdown: str = ""      # JSON component→points (no hidden weights)
    signal_count: int = 0
    created_by: Optional[int] = Field(default=None, foreign_key="user.id")
    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)


class OpportunitySignal(SQLModel, table=True):
    """Links an Opportunity to a DemandSignal that supports it (many-to-many)."""
    id: Optional[int] = Field(default=None, primary_key=True)
    opportunity_id: int = Field(foreign_key="opportunity.id", index=True)
    demand_signal_id: int = Field(foreign_key="demandsignal.id", index=True)
    created_at: datetime = Field(default_factory=datetime.utcnow)


class OpportunityMatch(SQLModel, table=True):
    """A matched Go4it supply candidate (Product / Supplier / Seller) for an Opportunity, with an EXPLANATION and
    the missing requirements. Never invents supplier capability; never exposes buyer identity to a seller."""
    id: Optional[int] = Field(default=None, primary_key=True)
    opportunity_id: int = Field(foreign_key="opportunity.id", index=True)
    product_id: Optional[int] = Field(default=None, foreign_key="product.id")
    company_id: Optional[int] = Field(default=None, foreign_key="company.id")  # supplier/seller (admin-only)
    match_score: int = 0
    explanation: str = ""          # why this matched (reasons)
    missing_requirements: str = ""  # what's missing/unverified for a firm match
    verified: bool = False
    created_at: datetime = Field(default_factory=datetime.utcnow)


class AnalyticsSnapshot(SQLModel, table=True):
    """An IMMUTABLE computed-analytics snapshot — both a metric cache and a historical record. A later source or
    scoring-weight change never rewrites it (it carries its own metric/scoring version + source cutoff). Used to
    serve dashboards fast and to make period-over-period comparisons honest."""
    id: Optional[int] = Field(default=None, primary_key=True)
    kind: str = Field(default="", index=True)   # cache key kind, e.g. "dashboard" | "funnel" | "source_health"
    cache_key: str = Field(default="", index=True)  # tenant-scoped, NO sensitive identifiers
    metric_version: str = ""
    scoring_version: str = ""
    time_range: str = ""           # e.g. "30d" | "2026-01..2026-03"
    filters: str = ""              # JSON (country/category/product/owner) — no buyer identifiers
    source_cutoff: Optional[datetime] = None
    source_freshness: str = ""     # JSON freshness summary
    result: str = ""               # JSON payload
    tenant_id: Optional[int] = Field(default=None, foreign_key="user.id")  # NULL = global/admin
    derived: bool = True
    inferred: bool = False
    generated_at: datetime = Field(default_factory=datetime.utcnow)


class IntelAlert(SQLModel, table=True):
    """An admin-only in-app intelligence alert. Idempotent on (alert_key, condition_version): the same unchanged
    condition never spawns a duplicate; a material change (new condition_version) may create a fresh alert. No
    automatic external email/outreach is ever sent from here."""
    id: Optional[int] = Field(default=None, primary_key=True)
    alert_type: str = ""           # new_high_demand|rising_category|market_spike|requirement_no_supply|
    # match_no_outreach|seasonal_window|expiring_opportunity|stale_data|source_failure|data_anomaly
    alert_key: str = Field(default="", index=True)
    condition_version: str = ""
    severity: str = "info"         # info | warning | critical
    title: str = ""
    body: str = ""                 # admin-facing summary (no buyer PII)
    related_opportunity_id: Optional[int] = Field(default=None, foreign_key="opportunity.id")
    owner_id: Optional[int] = Field(default=None, foreign_key="user.id")  # recipient/team; NULL = all admins
    cadence: str = "custom"        # daily | weekly | monthly | seasonal | custom
    schedule_tz: str = "UTC"       # tz used only to decide the local scheduled hour; stored times stay naive UTC
    status: str = "new"            # new | reviewed | snoozed | dismissed
    snooze_until: Optional[datetime] = None
    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)


class AnalyticsReport(SQLModel, table=True):
    """A generated internal admin report (CSV/PDF) stored PRIVATELY. Records its metric/scoring version, range,
    filters, source freshness and a sha256; generation + download are audited and admin-only. No auto emailing."""
    id: Optional[int] = Field(default=None, primary_key=True)
    reference: str = Field(default="", index=True)   # RPT-YYYYMM-####
    report_type: str = ""          # weekly_exec|monthly_demand|market|category|source_quality|funnel|operations|team_performance
    title: str = ""
    time_range: str = ""
    filters: str = ""              # JSON
    params: str = ""               # JSON
    metric_version: str = ""
    scoring_version: str = ""
    source_freshness: str = ""     # JSON appendix
    file_path: str = ""            # relative under REPORT_FILES_DIR (private)
    content_type: str = ""
    sha256: str = ""
    status: str = "generated"      # generated | failed
    tenant_id: Optional[int] = Field(default=None, foreign_key="user.id")
    generated_by: Optional[int] = Field(default=None, foreign_key="user.id")
    generated_at: datetime = Field(default_factory=datetime.utcnow)


# ============================================================================
# AI Command (Phase 9) — an ADMIN-ONLY AI copilot over the Go4it data. STRICTLY
# ADDITIVE. Conversations are tenant/owner-scoped and never seller-accessible.
# Message content is ENCRYPTED at rest with a DEDICATED key (AI_DATA_ENCRYPTION_
# KEYS — never SECRET_KEY / credential keys). The AI is evidence-based (every
# material claim cites a record), permission-controlled and CANNOT send outreach
# or change critical state without an explicit, revalidated runtime approval. No
# provider credentials, banking passwords, private keys or seeds are ever stored
# here. Retrieved content (docs, replies, tool output) is untrusted evidence,
# never instructions.
# ============================================================================


class AIPromptVersion(SQLModel, table=True):
    """A VERSIONED trusted system-instruction record. Exactly one is active; changes are audited and covered by
    the evaluation suite. The content establishes role/permissions, confidentiality, evidence requirements and
    tool restrictions — retrieved content can never override it."""
    id: Optional[int] = Field(default=None, primary_key=True)
    version: str = Field(default="", index=True)   # e.g. "p1"
    checksum: str = ""             # sha256 of content
    purpose: str = ""
    content: str = ""              # the trusted system prompt (no secrets)
    active: bool = False
    change_note: str = ""
    created_by: Optional[int] = Field(default=None, foreign_key="user.id")
    created_at: datetime = Field(default_factory=datetime.utcnow)


class AIConversation(SQLModel, table=True):
    """An admin AI-copilot conversation. tenant_id = the seller it concerns (NULL = platform/global); owner_id =
    the admin who owns it. Never seller-accessible. Cross-admin access is owner-scoped where required."""
    id: Optional[int] = Field(default=None, primary_key=True)
    tenant_id: Optional[int] = Field(default=None, foreign_key="user.id", index=True)
    owner_id: Optional[int] = Field(default=None, foreign_key="user.id", index=True)
    title: str = "New conversation"
    status: str = "active"         # active | archived
    imported: bool = False         # True = backfill-seeded from a historical CommandJob (never re-executed)
    sensitivity: str = "normal"    # normal | sensitive (contains buyer/commercial detail)
    archived_at: Optional[datetime] = None
    retention_at: Optional[datetime] = None   # optional configured retention cutoff
    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)


class AIMessage(SQLModel, table=True):
    """One turn in a conversation. `content_enc` is ENCRYPTED at rest (AI_DATA_ENCRYPTION_KEYS). Secrets are
    redacted BEFORE encryption; no raw credential/key/seed/password is ever persisted, and no message content is
    written to application logs."""
    id: Optional[int] = Field(default=None, primary_key=True)
    conversation_id: int = Field(foreign_key="aiconversation.id", index=True)
    role: str = "user"             # user | assistant | tool | system
    content_enc: str = ""          # ciphertext (Fernet token); "" for empty
    status: str = "complete"       # pending | running | complete | failed | cancelled
    partial: bool = False          # True = a labelled partial response
    prompt_version: str = ""
    provider: str = ""             # "" when answered deterministically (no LLM)
    model: str = ""
    sensitivity: str = "normal"
    citation_count: int = 0
    tool_count: int = 0
    error: str = ""
    created_at: datetime = Field(default_factory=datetime.utcnow)
    completed_at: Optional[datetime] = None


class AICitation(SQLModel, table=True):
    """Evidence for a material claim in a message. A safe record reference + provenance/freshness; an authorized
    internal link only. Never a fabricated company/price/stat."""
    id: Optional[int] = Field(default=None, primary_key=True)
    message_id: int = Field(foreign_key="aimessage.id", index=True)
    conversation_id: int = Field(foreign_key="aiconversation.id")
    record_type: str = ""          # metric | quote | deal | opportunity | demand_signal | source | ...
    record_ref: str = ""           # safe reference (e.g. "OPP-202608-0001", "metric:positive_replies")
    record_id: Optional[int] = None
    record_at: Optional[datetime] = None
    source: str = ""
    freshness: str = ""            # Current | Aging | Stale | Unknown | Not configured
    provenance_class: str = ""     # observed | verified | derived | inferred
    link: str = ""                 # authorized internal link ("" when not linkable)
    created_at: datetime = Field(default_factory=datetime.utcnow)


class AIToolInvocation(SQLModel, table=True):
    """An audited record of a tool call. Summaries only — never full sensitive payloads/credentials in logs."""
    id: Optional[int] = Field(default=None, primary_key=True)
    conversation_id: int = Field(foreign_key="aiconversation.id", index=True)
    message_id: Optional[int] = Field(default=None, foreign_key="aimessage.id")
    tool_name: str = ""
    risk_level: str = "read_only"
    params_summary: str = ""       # redacted/short — no secrets
    status: str = "ok"             # ok | error | denied
    result_summary: str = ""       # short — no full private payloads
    duration_ms: int = 0
    created_at: datetime = Field(default_factory=datetime.utcnow)


class AIActionProposal(SQLModel, table=True):
    """A PROPOSED material change the AI prepared but must NOT execute without explicit admin approval. Approval
    revalidates authz/target/tenant/freshness, compares payload_hash (rejects stale), executes idempotently via
    an existing domain service, and is fully audited. The AI never replays free-form text — only this validated
    structured payload runs."""
    id: Optional[int] = Field(default=None, primary_key=True)
    conversation_id: int = Field(foreign_key="aiconversation.id", index=True)
    message_id: Optional[int] = Field(default=None, foreign_key="aimessage.id")
    action_type: str = ""          # create_work_item | assign_owner | start_research | create_draft_* | ...
    target_summary: str = ""       # human-readable target (safe)
    payload: str = ""              # validated JSON structured payload (no secrets)
    payload_hash: str = Field(default="", index=True)   # sha256 of the canonical payload (staleness guard)
    reason: str = ""
    risk_level: str = "internal_reversible"  # read_only|draft|internal_reversible|external_comm|commercial|prohibited
    requires_approval: bool = True
    status: str = "proposed"       # proposed | approved | declined | executed | expired | failed
    approval_nonce: str = ""       # server-side one-time approval token (constant-time compared)
    idempotency_key: str = Field(default="", index=True)
    expires_at: Optional[datetime] = None
    proposed_by: str = ""          # "" (deterministic) or provider/model
    approved_by: Optional[int] = Field(default=None, foreign_key="user.id")
    executed_at: Optional[datetime] = None
    result: str = ""
    tenant_id: Optional[int] = Field(default=None, foreign_key="user.id")
    created_at: datetime = Field(default_factory=datetime.utcnow)


class AIUsageRecord(SQLModel, table=True):
    """Per-response usage/cost for budgets + telemetry. Never stores full sensitive prompts."""
    id: Optional[int] = Field(default=None, primary_key=True)
    conversation_id: Optional[int] = Field(default=None, foreign_key="aiconversation.id", index=True)
    message_id: Optional[int] = Field(default=None, foreign_key="aimessage.id")
    provider: str = ""
    model: str = ""
    input_tokens: int = 0
    output_tokens: int = 0
    latency_ms: int = 0
    tool_calls: int = 0
    est_cost: str = "0"            # Decimal-as-text (USD)
    tenant_id: Optional[int] = Field(default=None, foreign_key="user.id")
    owner_id: Optional[int] = Field(default=None, foreign_key="user.id", index=True)
    success: bool = True
    cache_hit: bool = False
    created_at: datetime = Field(default_factory=datetime.utcnow)


class AutomationRule(SQLModel, table=True):
    """A DETERMINISTIC safe-automation rule. AI may RECOMMEND rules (as proposals) but never secretly creates
    them. Actions are limited to internal, reversible outputs — automation NEVER sends email, starts campaigns,
    issues quotes/contracts, advances Deals, moves funds or publishes without existing safe approval."""
    id: Optional[int] = Field(default=None, primary_key=True)
    tenant_id: Optional[int] = Field(default=None, foreign_key="user.id", index=True)
    owner_id: Optional[int] = Field(default=None, foreign_key="user.id")
    name: str = ""
    trigger_type: str = ""         # work_queue_overdue | source_stale | new_opportunity | demand_no_supply | ...
    conditions: str = ""           # JSON
    action_type: str = ""          # create_work_item | create_alert | draft_report | draft_summary | assign_review
    action_params: str = ""        # JSON
    enabled: bool = True
    cadence: str = "event"         # event | daily | weekly | monthly
    schedule_tz: str = "UTC"
    condition_version: str = ""
    max_frequency_hours: int = 24  # never fire more often than this per condition instance
    last_run: Optional[datetime] = None
    next_run: Optional[datetime] = None
    failure_count: int = 0
    created_by: Optional[int] = Field(default=None, foreign_key="user.id")
    updated_by: Optional[int] = Field(default=None, foreign_key="user.id")
    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)


class AutomationRun(SQLModel, table=True):
    """A single (idempotent, bounded) automation-rule execution. dry_run previews without side effects."""
    id: Optional[int] = Field(default=None, primary_key=True)
    rule_id: int = Field(foreign_key="automationrule.id", index=True)
    status: str = "ok"             # ok | failed | skipped | dry_run
    condition_version: str = ""
    output_summary: str = ""
    related_workitem_id: Optional[int] = Field(default=None, foreign_key="workitem.id")
    error: str = ""
    started_at: datetime = Field(default_factory=datetime.utcnow)
    finished_at: Optional[datetime] = None


class AIEvaluationResult(SQLModel, table=True):
    """A durable evaluation-suite result (deterministic; no live provider). Prompt/model changes re-run the
    suite; a regression raises a Work Queue item."""
    id: Optional[int] = Field(default=None, primary_key=True)
    suite_version: str = ""
    scenario: str = Field(default="", index=True)
    passed: bool = True
    detail: str = ""
    prompt_version: str = ""
    model: str = ""
    created_at: datetime = Field(default_factory=datetime.utcnow)
