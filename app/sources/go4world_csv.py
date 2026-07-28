"""go4worldbusiness buy-lead CSV adapter (the TOS-clean 'official export' path).

You export buyer leads from your paid go4worldbusiness account and drop the CSV
into the inbox folder; this adapter parses them into RawLeads. Column names are
matched tolerantly because export formats vary. When the exact paid-tier format
is known, only the alias map below needs adjusting (or swap in an API adapter).
"""
import csv
import glob
import hashlib
import io
import os
import shutil

from .base import LeadSource, RawLead

_ALIASES = {
    "external_id": ["id", "lead id", "inquiry id", "reference", "ref", "lead_id"],
    "product": ["product", "item", "commodity", "requirement", "buying lead", "title", "subject"],
    "category": ["category", "cat"],
    "spec": ["specification", "specs", "spec", "details", "description"],
    "quantity": ["quantity", "qty", "volume"],
    "unit": ["unit", "uom"],
    "target_price": ["target price", "budget", "price", "target_price"],
    "currency": ["currency", "curr", "ccy"],
    "dest_country": ["country", "destination", "importing country", "buyer country", "dest_country"],
    "dest_city": ["city", "dest_city"],
    "buyer_company": ["company", "company name", "buyer", "importer", "buyer_company"],
    "contact_name": ["contact", "name", "contact person", "contact_name"],
    "email": ["email", "e-mail", "mail"],
    "phone": ["phone", "mobile", "tel", "telephone", "whatsapp"],
}
_NUMERIC = {"quantity", "target_price"}
# full country name -> ISO-ish code we store; matching corridor also accepts names.
_COUNTRY = {"georgia": "GE", "turkey": "TR", "türkiye": "TR", "turkiye": "TR"}


def _header_map(fieldnames):
    present = {name: (name or "").strip().lower() for name in fieldnames}
    mapping = {}
    for field, aliases in _ALIASES.items():
        for actual, low in present.items():
            if low in aliases:
                mapping[field] = actual
                break
    return mapping


def _num(value):
    try:
        return float(value) if value not in ("", None) else 0.0
    except ValueError:
        return 0.0


def parse_text(text: str):
    """Pure parse: CSV text -> list[RawLead]. Testable without the filesystem."""
    reader = csv.DictReader(io.StringIO(text))
    if not reader.fieldnames:
        return []
    hmap = _header_map(reader.fieldnames)
    leads = []
    for row in reader:
        get = lambda f: (row.get(hmap[f], "") or "").strip() if f in hmap else ""
        product = get("product")
        if not product:
            continue
        country = get("dest_country")
        raw = RawLead(
            external_id=get("external_id"),
            product=product,
            category=get("category"),
            spec=get("spec"),
            quantity=_num(get("quantity")),
            unit=get("unit"),
            target_price=_num(get("target_price")),
            currency=get("currency") or "USD",
            dest_country=_COUNTRY.get(country.lower(), country.upper()),
            dest_city=get("dest_city"),
            buyer_company=get("buyer_company"),
            contact_name=get("contact_name"),
            email=get("email"),
            phone=get("phone"),
            raw=dict(row),
        )
        # A stable id makes re-imports idempotent even without an id column.
        if not raw.external_id:
            basis = "|".join([raw.product, raw.buyer_company, raw.email, raw.dest_country,
                              str(raw.quantity)]).lower()
            raw.external_id = "h:" + hashlib.sha1(basis.encode()).hexdigest()[:16]
        leads.append(raw)
    return leads


class Go4WorldCsvSource(LeadSource):
    name = "go4world"

    def __init__(self, folder: str):
        self.folder = folder

    def fetch(self):
        leads = []
        processed = os.path.join(self.folder, "processed")
        os.makedirs(processed, exist_ok=True)
        for path in sorted(glob.glob(os.path.join(self.folder, "*.csv"))):
            try:
                with open(path, "r", encoding="utf-8-sig", errors="replace") as f:
                    leads.extend(parse_text(f.read()))
                shutil.move(path, os.path.join(processed, os.path.basename(path)))
            except Exception:
                # Leave the file in place so a transient error can be retried.
                continue
        return leads
