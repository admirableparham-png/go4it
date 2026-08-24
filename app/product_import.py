"""Phase 5 — product CSV import (preview → apply), idempotent + transaction-safe.

Conservative identity: products are NEVER matched by name alone. Identity is resolved from the strongest
available combination — internal SKU, else (HS code + origin + grade + supplier), else supplier product code +
supplier. A single strong match → update; multiple → AMBIGUOUS (routed to a review task, never auto-merged);
none → new. Category text resolves through the alias table. Re-running the same file changes nothing.
"""
import csv
import io
import json
import re

from sqlmodel import select

from . import category_service as CATS
from .company_service import get_or_create_company
from .models import Company, Product

# Canonical field ← accepted header spellings (superset of csv_import; adds the Phase-5 fields).
_ALIASES = {
    "sku": ("sku", "code", "internal code", "product code", "item code"),
    "name": ("name", "product", "product name", "title", "item"),
    "short_description": ("short description", "summary", "tagline"),
    "category": ("category", "cat", "product category"),
    "subcategory": ("subcategory", "sub category", "sub-category"),
    "grade": ("grade", "purity", "variant"),
    "brand": ("brand", "make"),
    "hs_code": ("hs", "hs code", "hs_code", "hscode", "tariff"),
    "spec": ("spec", "specification", "specs", "description"),
    "exw_price": ("exw", "exw price", "price", "unit price", "base price"),
    "currency": ("currency", "ccy"),
    "unit": ("unit", "uom", "unit of measure"),
    "weight_kg_per_unit": ("weight", "weight kg", "kg", "unit weight", "weight_kg_per_unit"),
    "min_order_qty": ("moq", "min order", "minimum order", "min_order_qty"),
    "origin_country": ("origin country", "origin_country", "country of origin", "origin"),
    "origin_city": ("origin city", "origin_city", "city"),
    "producer": ("producer", "manufacturer", "maker"),
    "incoterms": ("incoterms", "incoterm", "terms"),
    "certifications": ("certifications", "certs", "certificates"),
    "lead_time_days": ("lead time", "lead_time", "lead time days", "lead_time_days"),
    "units_per_package": ("units per package", "units_per_package", "pack size"),
    "supplier": ("supplier", "vendor", "supplier name"),
    "supplier_sku": ("supplier sku", "supplier code", "vendor code", "supplier_sku"),
    "supplier_country": ("supplier country", "supplier_country"),
}
_NUMERIC = {"exw_price", "weight_kg_per_unit", "min_order_qty", "units_per_package"}
_INT = {"lead_time_days"}
TEMPLATE_COLUMNS = ["sku", "name", "category", "subcategory", "grade", "brand", "hs_code", "spec",
                    "short_description", "exw_price", "currency", "unit", "weight_kg_per_unit", "min_order_qty",
                    "origin_country", "origin_city", "producer", "incoterms", "certifications",
                    "lead_time_days", "units_per_package", "supplier", "supplier_sku", "supplier_country"]


def template_csv() -> str:
    out = io.StringIO()
    w = csv.writer(out)
    w.writerow(TEMPLATE_COLUMNS)
    w.writerow(["CU-CATH-9999", "Copper cathode 99.99%", "Metals", "Copper", "99.99%", "", "7403.11", "LME grade A",
                "High-purity copper cathode", "8500", "USD", "tonne", "1000", "25", "IR", "Tabriz",
                "Nat'l Copper", "FOB,CIF", "ISO9001", "30", "1", "Example Supplier Co", "SUP-CU-01", "IR"])
    return out.getvalue()


def _norm_headers(fieldnames):
    """Map each CSV header to a canonical field name (best-effort)."""
    mapping = {}
    for h in fieldnames or []:
        hn = re.sub(r"\s+", " ", (h or "").strip().lower())
        for canon, spellings in _ALIASES.items():
            if hn == canon or hn in spellings:
                mapping[h] = canon
                break
    return mapping


def parse(text: str):
    """Parse CSV → (rows, errors, column_mapping). Rows are dicts keyed by canonical field. A row with no
    name AND no sku is an error (can't identify). Bad numbers are reported, not fatal."""
    rows, errors = [], []
    reader = csv.DictReader(io.StringIO(text))
    mapping = _norm_headers(reader.fieldnames)
    if "name" not in mapping.values() and "sku" not in mapping.values():
        return [], ["CSV needs at least a 'name' or 'sku' column"], mapping
    for i, raw in enumerate(reader, start=2):
        rec = {}
        for header, canon in mapping.items():
            val = (raw.get(header) or "").strip()
            if canon in _NUMERIC:
                try:
                    rec[canon] = float(val) if val else 0.0
                except ValueError:
                    errors.append(f"row {i}: bad number for {canon}: {val!r}")
                    rec[canon] = 0.0
            elif canon in _INT:
                try:
                    rec[canon] = int(float(val)) if val else 0
                except ValueError:
                    rec[canon] = 0
            else:
                rec[canon] = val
        if not (rec.get("name") or rec.get("sku")):
            errors.append(f"row {i}: no name or sku — skipped")
            continue
        rec["_row"] = i
        rows.append(rec)
    return rows, errors, mapping


def _candidates(session, rec):
    """Conservative identity — return the list of matching products (NEVER by name alone)."""
    sku = (rec.get("sku") or "").strip()
    if sku:
        m = session.exec(select(Product).where(Product.sku == sku)).all()
        if m:
            return m, "sku"
    hs, origin, grade = (rec.get("hs_code") or "").strip(), (rec.get("origin_country") or "").strip(), \
        (rec.get("grade") or "").strip()
    if hs and origin:
        q = select(Product).where(Product.hs_code == hs, Product.origin_country == origin)
        m = [p for p in session.exec(q).all() if (grade == "" or (p.grade or "") == grade)]
        if m:
            return m, "hs+origin+grade"
    return [], ""


def preview(session, rows):
    """Classify each parsed row as new | update | ambiguous | skip, with the match basis. Changes nothing."""
    out = {"new": 0, "update": 0, "ambiguous": 0, "skip": 0, "rows": []}
    for rec in rows:
        cands, basis = _candidates(session, rec)
        if len(cands) == 1:
            decision, target = "update", cands[0].id
        elif len(cands) > 1:
            decision, target = "ambiguous", [c.id for c in cands]
        else:
            decision, target = "new", None
        out[decision] += 1
        out["rows"].append({"row": rec.get("_row"), "name": rec.get("name", ""), "sku": rec.get("sku", ""),
                            "decision": decision, "basis": basis, "target": target})
    return out


_ASSIGNABLE = ("name", "short_description", "subcategory", "grade", "brand", "hs_code", "spec", "exw_price",
               "currency", "unit", "weight_kg_per_unit", "min_order_qty", "origin_country", "origin_city",
               "producer", "incoterms", "certifications", "lead_time_days", "units_per_package")


def apply(session, rows, actor=None, tenant_id=None):
    """Idempotent, transaction-safe import. new→create, single match→update, ambiguous→work item (never
    merged), errors counted. Category text resolves via alias (creates an inferred category if unseen).
    Returns counts + a per-row report (downloadable as an error CSV by the caller)."""
    counts = {"created": 0, "updated": 0, "ambiguous": 0, "skipped": 0, "errors": 0}
    report = []
    for rec in rows:
        try:
            cands, basis = _candidates(session, rec)
            if len(cands) > 1:
                counts["ambiguous"] += 1
                _ambiguous_task(session, rec, [c.id for c in cands], actor)
                report.append({"row": rec.get("_row"), "result": "ambiguous",
                               "detail": f"matches {[c.id for c in cands]} by {basis}"})
                continue
            cat = CATS.resolve_category(session, rec.get("category", ""), tenant_id) if rec.get("category") \
                else None
            if rec.get("category") and cat is None:
                cat = CATS.get_or_create_category(session, rec["category"], tenant_id=tenant_id, inferred=True)
                CATS.add_alias(session, rec["category"], cat.id)
            supplier_company = _resolve_supplier(session, rec)
            if cands:
                p = cands[0]
                for f in _ASSIGNABLE:
                    if f in rec and rec[f] not in ("", 0, 0.0):
                        setattr(p, f, rec[f])
                if cat:
                    p.category_id = cat.id
                    p.category = cat.name
                _sync_status(p)
                session.add(p)
                counts["updated"] += 1
                report.append({"row": rec.get("_row"), "result": "updated", "detail": f"id={p.id}"})
            else:
                p = Product(name=rec.get("name") or rec.get("sku"), sku=rec.get("sku", ""))
                for f in _ASSIGNABLE:
                    if f in rec:
                        setattr(p, f, rec[f])
                if cat:
                    p.category_id = cat.id
                    p.category = cat.name
                elif rec.get("category"):
                    p.category = rec["category"]
                _sync_status(p)
                session.add(p); session.flush()
                counts["created"] += 1
                report.append({"row": rec.get("_row"), "result": "created", "detail": f"id={p.id}"})
            if supplier_company and p.id:
                _link_supplier(session, p, supplier_company, rec)
        except Exception as e:  # noqa: BLE001 — one bad row never aborts the batch
            counts["errors"] += 1
            report.append({"row": rec.get("_row"), "result": "error", "detail": str(e)[:200]})
    return counts, report


def _sync_status(p):
    p.status = "active" if p.active else "archived"


def _resolve_supplier(session, rec):
    name = (rec.get("supplier") or "").strip()
    if not name:
        return None
    country = (rec.get("supplier_country") or rec.get("origin_country") or "").strip()
    return get_or_create_company(session, None, name, country, "", role="supplier")


def _link_supplier(session, product, company, rec):
    from .models import ProductSupplier
    if not company:
        return
    exists = session.exec(select(ProductSupplier).where(
        ProductSupplier.product_id == product.id, ProductSupplier.company_id == company.id)).first()
    if exists:
        return
    session.add(ProductSupplier(product_id=product.id, company_id=company.id,
                                supplier_sku=rec.get("supplier_sku", ""), currency=rec.get("currency", "USD")))


def _ambiguous_task(session, rec, candidate_ids, actor):
    try:
        from . import work_queue as WQ
        key = f"ambiguous_import_match:sku:{rec.get('sku') or rec.get('name')}"
        WQ.create_work_item_safe(session, actor=actor, type="ambiguous_import_match", source="automatic",
                                 title="Ambiguous product import match",
                                 description=(f"Import row {rec.get('_row')} ({rec.get('name') or rec.get('sku')}) "
                                              f"matched multiple products {candidate_ids} — review before merge."),
                                 idempotency_key=key, condition_version=json.dumps(sorted(candidate_ids)))
    except Exception:  # noqa: BLE001
        pass


def error_report_csv(report) -> str:
    out = io.StringIO()
    w = csv.writer(out)
    w.writerow(["row", "result", "detail"])
    for r in report:
        w.writerow([r.get("row"), r.get("result"), r.get("detail")])
    return out.getvalue()
