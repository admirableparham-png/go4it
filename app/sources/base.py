"""Pluggable lead-source contract.

A LeadSource knows how to `fetch()` a batch of RawLeads from somewhere
(a CSV export, an API, an email mailbox). Normalization + dedup + persistence
happen in app/ingest.py, so a new source only has to produce RawLeads.
"""
from dataclasses import dataclass, field


@dataclass
class RawLead:
    """A source-agnostic buyer request, before it becomes a Lead row."""
    external_id: str = ""
    product: str = ""
    category: str = ""
    spec: str = ""
    quantity: float = 0.0
    unit: str = ""
    target_price: float = 0.0
    currency: str = "USD"
    dest_country: str = ""
    dest_city: str = ""
    buyer_company: str = ""
    contact_name: str = ""
    email: str = ""
    phone: str = ""
    raw: dict = field(default_factory=dict)


class LeadSource:
    """Base class for all lead sources."""
    name = "base"

    def fetch(self):
        """Return a list[RawLead]. Implemented by subclasses."""
        raise NotImplementedError
