"""Products/Pricing (Phase 5) — additive, idempotent, CONSERVATIVE backfill.

    ./.venv/bin/python scripts/backfill_products.py --dry-run   # show what it would create; change nothing
    ./.venv/bin/python scripts/backfill_products.py             # apply (transactional)
    ./.venv/bin/python scripts/backfill_products.py --rollback  # PRE-GO-LIVE revert (inferred rows only)
    ./.venv/bin/python scripts/backfill_products.py --recover   # POST-GO-LIVE safe cleanup (untouched only)

RUN ORDER (prod): backup_db.py -> migrate.py -> migrate_gate_p5.py -> (prior backfills) -> THIS.

What it does — strictly additive, conservative, no invented data:
  * Creates a ProductCategory (inferred=True) for each distinct non-empty free-text Product.category, adds the
    text as an alias, and links matching products (sets category_id). The free-text `category` is PRESERVED.
  * Links ProductSupplier (inferred=True) from the legacy Product.supplier_id when the Supplier already has a
    canonical company_id (deterministic). Suppliers with no company link are counted, never guessed.
  * Does NOT invent HS codes, verification, reliability, or prices, and never marks placeholder prices verified
    or activates expired rates. Operational counts (products/suppliers/quotes/deals/companies) never change.
"""
import os
import sys

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)

from sqlmodel import Session, func, select   # noqa: E402

from app import category_service as CATS   # noqa: E402
from app.db import engine, init_db   # noqa: E402
from app.models import (Company, Deal, Product, ProductCategory, ProductCategoryAlias, ProductSupplier,
                        Quote, Supplier)   # noqa: E402

_OPERATIONAL = ("products", "suppliers", "quotes", "deals", "companies")


def _counts(s) -> dict:
    c = lambda m: s.exec(select(func.count()).select_from(m)).one()   # noqa: E731
    return {"products": c(Product), "suppliers": c(Supplier), "quotes": c(Quote), "deals": c(Deal),
            "companies": c(Company), "categories": c(ProductCategory),
            "product_suppliers": c(ProductSupplier)}


def _apply(s) -> dict:
    res = {"categories_created": 0, "aliases_created": 0, "products_categorized": 0,
           "product_suppliers_linked": 0, "suppliers_no_company": 0}
    # 1) free-text categories → inferred ProductCategory + alias, link uncategorized products
    sources = {(p.category or "").strip() for p in s.exec(select(Product)).all() if (p.category or "").strip()}
    for name in sorted(sources):
        existing = CATS.resolve_category(s, name)
        cat = existing or CATS.get_or_create_category(s, name, inferred=True)
        if not existing:
            res["categories_created"] += 1
            if CATS.add_alias(s, name, cat.id):
                res["aliases_created"] += 1
        for p in s.exec(select(Product).where(Product.category == name,
                                              Product.category_id == None)).all():   # noqa: E711
            p.category_id = cat.id
            s.add(p)
            res["products_categorized"] += 1
    # 2) legacy supplier links → canonical ProductSupplier (only when the supplier has a company)
    for p in s.exec(select(Product).where(Product.supplier_id != None)).all():   # noqa: E711
        sup = s.get(Supplier, p.supplier_id)
        if not sup or not sup.company_id:
            res["suppliers_no_company"] += 1
            continue
        exists = s.exec(select(ProductSupplier).where(
            ProductSupplier.product_id == p.id, ProductSupplier.company_id == sup.company_id)).first()
        if exists:
            continue
        s.add(ProductSupplier(product_id=p.id, company_id=sup.company_id, is_primary=True, inferred=True))
        res["product_suppliers_linked"] += 1
    return res


def migrate(dry=False):
    init_db()
    with Session(engine) as s:
        pre = _counts(s)
        print("PRE :", pre)
        if dry:
            sp = s.begin_nested()
            res = _apply(s)
            post = _counts(s)
            sp.rollback()
            print(f"[dry-run] would create: {res}")
            print("POST:", post, "(rolled back)")
            return
        try:
            res = _apply(s)
            post = _counts(s)
            for k in _OPERATIONAL:
                if pre[k] != post[k]:
                    raise RuntimeError(f"operational count changed for {k}: {pre[k]} -> {post[k]}")
            s.commit()
            print(f"OK — created {res}")
            print("POST:", post)
        except Exception:
            s.rollback()
            print("ERROR — rolled back, no partial backfill applied")
            raise


def rollback():
    """PRE-GO-LIVE: unset category_id on products pointing at an inferred category, delete inferred categories +
    their aliases + inferred product-supplier links. The free-text `category` field is never touched."""
    init_db()
    with Session(engine) as s:
        inferred = s.exec(select(ProductCategory).where(ProductCategory.inferred == True)).all()  # noqa: E712
        ids = {c.id for c in inferred}
        n_unset = 0
        for p in s.exec(select(Product).where(Product.category_id.in_(ids or [-1]))).all():
            p.category_id = None; s.add(p); n_unset += 1
        aliases = [a for a in s.exec(select(ProductCategoryAlias)).all() if a.category_id in ids]
        ps = s.exec(select(ProductSupplier).where(ProductSupplier.inferred == True)).all()  # noqa: E712
        for row in aliases + ps + inferred:
            s.delete(row)
        s.commit()
        print(f"PRE-GO-LIVE rollback: unset {n_unset} product category_id(s), deleted {len(inferred)} inferred "
              f"category(ies), {len(aliases)} alias(es), {len(ps)} inferred product-supplier link(s)")


def recover():
    """POST-GO-LIVE: remove only inferred categories that have NO products and were not merged (never touched by
    an admin). Preserves every real admin change, price version and approved catalog."""
    init_db()
    with Session(engine) as s:
        removed = 0
        for c in s.exec(select(ProductCategory).where(ProductCategory.inferred == True)).all():  # noqa: E712
            n = s.exec(select(func.count()).select_from(Product).where(Product.category_id == c.id)).one()
            if not n and c.merged_into_id is None and c.status == "active":
                # drop its aliases too, then the category
                for a in [x for x in s.exec(select(ProductCategoryAlias)).all() if x.category_id == c.id]:
                    s.delete(a)
                s.delete(c); removed += 1
        s.commit()
        print(f"POST-GO-LIVE recovery: removed {removed} untouched inferred category(ies); all admin changes, "
              f"price versions and approved catalogs preserved")


if __name__ == "__main__":
    if "--rollback" in sys.argv:
        rollback()
    elif "--recover" in sys.argv:
        recover()
    else:
        migrate(dry="--dry-run" in sys.argv)
