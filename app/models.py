"""go4it data model (Phase 1).

Asymmetric brokerage pipeline: external **buyer Leads** are matched against our
internal **Product** catalog (sourced from **Suppliers**). The Lead is the spine.

Money is plain float for now; it becomes Decimal in Phase 2 when quotes go to
buyers. Alembic migrations are introduced at the start of Phase 2, before any of
the real catalog data entered here is altered.
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
    """A scored pairing of a buyer Lead and a catalog Product — the thing the
    team acts on. One row per (lead, product)."""
    __table_args__ = (UniqueConstraint("lead_id", "product_id", name="uq_match_lead_product"),)

    id: Optional[int] = Field(default=None, primary_key=True)
    lead_id: int = Field(foreign_key="lead.id", index=True)
    product_id: int = Field(foreign_key="product.id", index=True)
    score: float = 0
    reasons: str = ""
    is_dismissed: bool = False
    created_at: datetime = Field(default_factory=datetime.utcnow)
