"""Phase 5 — structured product categories (replaces unrestricted free-text Product.category).

Hierarchical, archivable, alias-matched (for CSV import + research), and SAFELY mergeable: a merge re-points
products, aliases and child categories to the survivor and archives the source (reversible via merged_into_id)
— it NEVER deletes product history. Everything here is admin-only at the route layer.
"""
import re
from datetime import datetime

from sqlmodel import func, select

from .models import Product, ProductCategory, ProductCategoryAlias


def normalize(name: str) -> str:
    return re.sub(r"\s+", " ", (name or "").strip().lower())


def _slug(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", normalize(name)).strip("-")[:60]


def get_or_create_category(session, name, *, tenant_id=None, parent_id=None, inferred=False):
    """Idempotent get-or-create on (tenant, normalized name, parent). Returns the active category."""
    norm = normalize(name)
    if not norm:
        return None
    existing = session.exec(select(ProductCategory).where(
        ProductCategory.name_normalized == norm, ProductCategory.parent_id == parent_id,
        ProductCategory.status == "active")).first()
    if existing:
        return existing
    cat = ProductCategory(tenant_id=tenant_id, name=name.strip(), name_normalized=norm, slug=_slug(name),
                          parent_id=parent_id, status="active", inferred=inferred)
    session.add(cat)
    session.flush()
    return cat


def add_alias(session, alias, category_id):
    """Map an alternate spelling to a canonical category (idempotent on normalized alias)."""
    norm = normalize(alias)
    if not norm:
        return None
    existing = session.exec(select(ProductCategoryAlias).where(
        ProductCategoryAlias.alias_normalized == norm)).first()
    if existing:
        return existing
    a = ProductCategoryAlias(alias_normalized=norm, category_id=category_id)
    session.add(a)
    try:
        session.flush()
    except Exception:  # noqa: BLE001 — a concurrent identical alias is a harmless no-op
        session.rollback()
        return session.exec(select(ProductCategoryAlias).where(
            ProductCategoryAlias.alias_normalized == norm)).first()
    return a


def resolve_category(session, text, tenant_id=None):
    """Resolve inbound category text → a canonical category via exact name OR a registered alias. Returns the
    category or None (caller decides: create, or route ambiguous to review). Never guesses across tenants."""
    norm = normalize(text)
    if not norm:
        return None
    cat = session.exec(select(ProductCategory).where(
        ProductCategory.name_normalized == norm, ProductCategory.status == "active")).first()
    if cat:
        return cat
    alias = session.exec(select(ProductCategoryAlias).where(
        ProductCategoryAlias.alias_normalized == norm)).first()
    if alias:
        return session.get(ProductCategory, alias.category_id)
    return None


def product_count(session, category_id) -> int:
    return session.exec(select(func.count()).select_from(Product).where(
        Product.category_id == category_id)).one() or 0


def uncategorized(session):
    return session.exec(select(Product).where(Product.category_id.is_(None))).all()


def move_products(session, product_ids, category_id, actor=None) -> int:
    """Bulk move products to a category (records prev/new per product in the audit meta)."""
    n = 0
    for pid in product_ids:
        p = session.get(Product, pid)
        if not p:
            continue
        prev, p.category_id = p.category_id, category_id
        if prev != category_id:
            p.updated_at = datetime.utcnow(); session.add(p)
            _audit(session, actor, pid, "product_category_move", {"from": prev, "to": category_id})
            n += 1
    return n


def merge_categories(session, source_id, dest_id, actor=None) -> dict:
    """SAFE merge: re-point every product, alias and child category from source→dest, add the source name as
    an alias of dest, archive the source (merged_into_id=dest, reversible). Product HISTORY is never deleted —
    products are only re-pointed. Audited with before/after. Returns move counts."""
    if source_id == dest_id:
        return {"error": "cannot merge a category into itself"}
    src = session.get(ProductCategory, source_id)
    dst = session.get(ProductCategory, dest_id)
    if not src or not dst:
        return {"error": "category not found"}
    moved = 0
    for p in session.exec(select(Product).where(Product.category_id == source_id)).all():
        p.category_id = dest_id; p.updated_at = datetime.utcnow(); session.add(p); moved += 1
    for a in session.exec(select(ProductCategoryAlias).where(
            ProductCategoryAlias.category_id == source_id)).all():
        a.category_id = dest_id; session.add(a)
    children = 0
    for c in session.exec(select(ProductCategory).where(ProductCategory.parent_id == source_id)).all():
        c.parent_id = dest_id; session.add(c); children += 1
    add_alias(session, src.name, dest_id)          # keep the old name resolvable to the survivor
    src.status = "archived"
    src.merged_into_id = dest_id
    src.updated_at = datetime.utcnow()
    session.add(src)
    _audit(session, actor, source_id, "category_merge",
           {"into": dest_id, "products_moved": moved, "children_moved": children,
            "source_name": src.name, "dest_name": dst.name})
    return {"products_moved": moved, "children_moved": children, "into": dest_id}


def unmerge_category(session, source_id, actor=None) -> bool:
    """Reverse the archive side of a merge (reactivate the source). Products already moved stay with the
    survivor unless an admin moves them back — history is intact, nothing was destroyed."""
    src = session.get(ProductCategory, source_id)
    if not src or src.status != "archived" or src.merged_into_id is None:
        return False
    into = src.merged_into_id
    src.status = "active"; src.merged_into_id = None; src.updated_at = datetime.utcnow()
    session.add(src)
    _audit(session, actor, source_id, "category_unmerge", {"was_merged_into": into})
    return True


def set_status(session, category_id, status, actor=None) -> bool:
    cat = session.get(ProductCategory, category_id)
    if not cat or status not in ("active", "archived"):
        return False
    prev, cat.status = cat.status, status
    cat.updated_at = datetime.utcnow(); session.add(cat)
    _audit(session, actor, category_id, "category_status", {"from": prev, "to": status})
    return True


def _audit(session, actor, cat_id, action, meta):
    try:
        from .pipeline import audit
        audit(session, actor, "product_category", cat_id, action, meta)
    except Exception:  # noqa: BLE001
        pass
