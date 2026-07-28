"""Parse a product-catalog CSV into normalized rows.

Tolerant of column-name variations (Excel exports, different spellings) so the
team can upload the sheet they already keep. Returns (rows, errors); each row is
a dict keyed by the canonical Product field names.
"""
import csv
import io

# canonical field -> accepted header names (compared lowercased/stripped)
_ALIASES = {
    "name": ["name", "product", "product name", "title", "item"],
    "category": ["category", "cat"],
    "spec": ["spec", "specification", "specs", "grade"],
    "hs_code": ["hs_code", "hs", "hs code", "hscode", "hs-code"],
    "exw_price": ["exw_price", "exw", "exw price", "price", "unit price", "unitprice"],
    "currency": ["currency", "curr", "ccy"],
    "unit": ["unit", "uom", "units"],
    "weight_kg_per_unit": ["weight_kg_per_unit", "weight", "weight kg", "weight_kg", "kg"],
    "cbm_per_unit": ["cbm_per_unit", "cbm", "volume", "m3"],
    "packaging": ["packaging", "package", "pack"],
    "min_order_qty": ["min_order_qty", "moq", "min order", "minimum order", "min_qty", "min qty"],
    "origin_region": ["origin_region", "origin", "region"],
    "supplier": ["supplier", "supplier name", "vendor", "manufacturer"],
}
_NUMERIC = {"exw_price", "weight_kg_per_unit", "cbm_per_unit", "min_order_qty"}


def _header_map(fieldnames):
    """Map canonical field -> the actual header present in the file."""
    present = {name: (name or "").strip().lower() for name in fieldnames}
    mapping = {}
    for field, aliases in _ALIASES.items():
        for actual, low in present.items():
            if low in aliases:
                mapping[field] = actual
                break
    return mapping


def parse_products(text: str):
    """Parse CSV text. Returns (rows, errors)."""
    reader = csv.DictReader(io.StringIO(text))
    if not reader.fieldnames:
        return [], ["The file is empty or has no header row."]

    hmap = _header_map(reader.fieldnames)
    if "name" not in hmap:
        return [], [
            "CSV needs a product-name column (e.g. 'name' or 'product'). "
            "Found: " + ", ".join(reader.fieldnames)
        ]

    rows, errors = [], []
    for line_no, raw in enumerate(reader, start=2):  # row 1 is the header
        rec = {}
        for field, col in hmap.items():
            value = (raw.get(col) or "").strip()
            if field in _NUMERIC:
                try:
                    rec[field] = float(value) if value else 0.0
                except ValueError:
                    errors.append(f"row {line_no}: '{col}'='{value}' is not a number (used 0)")
                    rec[field] = 0.0
            else:
                rec[field] = value
        if not rec.get("name"):
            errors.append(f"row {line_no}: missing product name — skipped")
            continue
        rows.append(rec)
    return rows, errors
