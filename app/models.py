"""The three tables that run go4it: Demand, Offer, Match."""
from datetime import datetime
from typing import Optional

from sqlmodel import SQLModel, Field


class Demand(SQLModel, table=True):
    """Something a buyer wants."""
    id: Optional[int] = Field(default=None, primary_key=True)
    product: str
    category: str = ""
    spec: str = ""
    quantity: float = 0
    unit: str = ""
    target_price: float = 0        # buyer's max acceptable price (per unit)
    currency: str = "USD"
    location: str = ""
    contact: str = ""              # who to talk to (name / phone / channel)
    notes: str = ""
    status: str = "open"           # open | closed
    created_at: datetime = Field(default_factory=datetime.utcnow)


class Offer(SQLModel, table=True):
    """Something a seller has available."""
    id: Optional[int] = Field(default=None, primary_key=True)
    product: str
    category: str = ""
    spec: str = ""
    quantity: float = 0
    unit: str = ""
    price: float = 0               # seller's asking price (per unit)
    currency: str = "USD"
    location: str = ""
    contact: str = ""
    notes: str = ""
    status: str = "open"           # open | closed
    created_at: datetime = Field(default_factory=datetime.utcnow)


class Match(SQLModel, table=True):
    """A scored pairing of a demand and an offer — the thing your team acts on."""
    id: Optional[int] = Field(default=None, primary_key=True)
    demand_id: int = Field(foreign_key="demand.id")
    offer_id: int = Field(foreign_key="offer.id")
    score: float = 0
    reasons: str = ""              # human-readable "why this matched"
    status: str = "new"           # new | contacted | won | lost
    created_at: datetime = Field(default_factory=datetime.utcnow)
