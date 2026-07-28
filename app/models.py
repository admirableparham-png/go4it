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
    source: str = "manual"         # manual | csv | go4world | ...
    external_id: str = ""          # id at the source, for dedup
    content_hash: str = Field(default="", index=True)       # dedup identical leads

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

    # workflow (the 5-stage CRM pipeline is formalized in Phase 3)
    status: str = "new"            # new | quoted | negotiating | won | lost
    assigned_to: str = ""
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
    rate_per_truck: float = 0
    rate_per_tonne: float = 0
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
    version: int = 1
    created_by: str = ""
    approved_by: str = ""
    created_at: datetime = Field(default_factory=datetime.utcnow)
