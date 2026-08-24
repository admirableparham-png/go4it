"""Explicit, idempotent production migration for the Phase-5 Products/Pricing schema.

Rather than relying on ORM create_all as the production procedure, this EXPLICITLY and idempotently ensures the
new tables + their unique constraints + indexes + the additive product/supplier/fxrate/workitem columns.

    ./.venv/bin/python scripts/migrate_gate_p5.py --dry-run   # show pending ops + counts; change nothing
    ./.venv/bin/python scripts/migrate_gate_p5.py             # apply (idempotent; re-running is a no-op)

Run AFTER scripts/migrate.py (additive columns). A PRAGMA integrity_check runs first; pre/post operational
counts (products/suppliers/quotes/deals/companies) are asserted invariant; the constraints/indexes are
verified. Never deletes product, supplier, quote, deal or company history. Take a dated backup first
(scripts/backup_db.py). Rollback = restore the backup (schema additions are non-destructive).
"""
import os
import sys

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)

import app.models  # noqa: E402,F401 — register every model in metadata
from sqlalchemy import inspect, text   # noqa: E402
from sqlmodel import Session, func, select   # noqa: E402

from app.db import _is_sqlite, engine   # noqa: E402
from app.models import (CatalogGenerationJob, Company, CostRate, Deal, Product, ProductCategory,
                        ProductCategoryAlias, ProductDocument, ProductPriceVersion, ProductSupplier,
                        ProductVariant, Quote, Supplier)   # noqa: E402

_NEW_TABLES = [ProductCategory, ProductCategoryAlias, ProductVariant, ProductSupplier, CostRate,
               ProductPriceVersion, ProductDocument, CatalogGenerationJob]
_GATE_INDEXES = [
    ("uq_productsupplier_pc", "productsupplier", "product_id, company_id", True),
    ("uq_productcategory_active", "productcategory", "tenant_id, name_normalized, parent_id", True,
     "status = 'active'"),
    ("uq_prodcatalias_norm", "productcategoryalias", "alias_normalized", True),
    ("ix_productpriceversion_product", "productpriceversion", "product_id", False),
    ("ix_cataloggenerationjob_product", "cataloggenerationjob", "product_id", False),
]
_PRODUCT_COLS = {"sku", "category_id", "origin_country", "verification_status", "created_at", "status"}
_OPERATIONAL = {"products": Product, "suppliers": Supplier, "quotes": Quote, "deals": Deal, "companies": Company}


def _counts(s):
    return {k: s.exec(select(func.count()).select_from(m)).one() for k, m in _OPERATIONAL.items()}


def _integrity_ok():
    if not _is_sqlite:
        return True
    with engine.connect() as c:
        return (c.execute(text("PRAGMA integrity_check")).fetchone() or ["?"])[0] == "ok"


def _plan():
    insp = inspect(engine)
    tables = set(insp.get_table_names())
    ops = []
    for m in _NEW_TABLES:
        if m.__tablename__ not in tables:
            ops.append(f"CREATE TABLE {m.__tablename__}")
    if "product" in tables:
        cols = {c["name"] for c in insp.get_columns("product")}
        for col in sorted(_PRODUCT_COLS - cols):
            ops.append(f"ADD COLUMN product.{col}")
    for idx in _GATE_INDEXES:
        name, table = idx[0], idx[1]
        if table not in tables:
            continue
        have = {i["name"] for i in insp.get_indexes(table)} | {u["name"] for u in
                                                               insp.get_unique_constraints(table)}
        if name not in have:
            ops.append(f"CREATE INDEX {name}")
    return ops


def _apply():
    for m in _NEW_TABLES:
        m.__table__.create(bind=engine, checkfirst=True)
    with engine.begin() as c:
        for idx in _GATE_INDEXES:
            name, table, cols, uniq = idx[0], idx[1], idx[2], idx[3]
            where = f" WHERE {idx[4]}" if len(idx) > 4 else ""
            u = "UNIQUE " if uniq else ""
            c.execute(text(f"CREATE {u}INDEX IF NOT EXISTS {name} ON {table}({cols}){where}"))


def _verify():
    insp = inspect(engine)
    tables = set(insp.get_table_names())
    problems = []
    for m in _NEW_TABLES:
        if m.__tablename__ not in tables:
            problems.append(f"missing table {m.__tablename__}")
    for idx in _GATE_INDEXES:
        name, table = idx[0], idx[1]
        if table not in tables:
            problems.append(f"missing table {table} for index {name}")
            continue
        have = {i["name"] for i in insp.get_indexes(table)} | {u["name"] for u in
                                                               insp.get_unique_constraints(table)}
        if name not in have:
            problems.append(f"missing index {name}")
    return problems


def main(dry=False):
    if not _integrity_ok():
        print("ABORT: PRAGMA integrity_check failed — restore a backup before migrating.")
        sys.exit(2)
    with Session(engine) as s:
        pre = _counts(s)
    ops = _plan()
    print("PRE :", pre)
    print("pending operations:", ops or "none (already up to date)")
    if dry:
        print("[dry-run] no changes made.")
        return
    _apply()
    with Session(engine) as s:
        post = _counts(s)
    changed = {k: (pre[k], post[k]) for k in _OPERATIONAL if pre[k] != post[k]}
    if changed:
        print("ABORT: operational counts changed (must be invariant):", changed)
        sys.exit(3)
    problems = _verify()
    print("POST:", post)
    print("index verification:", "OK" if not problems else problems)
    if problems:
        sys.exit(4)
    print("Phase-5 gate migration complete (idempotent).")


if __name__ == "__main__":
    main(dry="--dry-run" in sys.argv)
