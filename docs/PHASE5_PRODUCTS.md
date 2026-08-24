# Phase 5 — Products, Catalogs, Suppliers & Pricing

Admin-only workspace under the existing **Products** header (no sidebar): **Catalog · Categories · Suppliers ·
Pricing & Rates · Catalog Studio**. Everything additive; existing quotes/deals, the Trade Network, Research,
Work Queue and Phase-4 systems are unchanged.

## Data model (additive)
- **Product** (extended): `sku`, `short_description`, `category_id`(FK), `subcategory`, `brand`, `grade`,
  `origin_country`, `origin_city`, `producer`, `units_per_package`, `production_capacity`, `lead_time_days`,
  `shelf_life`, `storage_requirements`, `certifications`, `incoterms`, `status`, `completeness_score`,
  `verification_status`, `verified_at/by`, `internal_notes` (admin-only), `created_at`. Legacy fields
  (`category` free text, `exw_price`, `supplier_id`, `active`) are preserved + kept in sync.
- **Supplier** (extended): `reliability_rated` — the bare 1-5 `reliability` is shown only as an explicit manual
  admin rating; unrated suppliers show **"Not yet rated"** (no invented default stars).
- **FxRate** (extended): `source`, `kind` (manual|live), `retrieved_at`, `expires_at`, `verified_by`,
  `active` — honest staleness (Live/verified · Manually entered · Stale · Not available). The existing quote
  FX path only reads `rate` and is unchanged.
- **New tables:** `ProductCategory` (hierarchical, mergeable, aliasable), `ProductCategoryAlias`,
  `ProductVariant`, `ProductSupplier` (product↔canonical Company, unique per pair), `CostRate` (structured
  time-bounded cost components), `ProductPriceVersion` (immutable landed-price calculations),
  `ProductDocument` (private files), `CatalogGenerationJob` (Catalog Studio). `WorkItem.related_product_id`
  added. Legacy `RateCard`/`CostParam` are untouched.

## Indexes
`ix_product_{sku,category_id,origin_country,supplier_id}`; partial-unique `uq_productsupplier_pc`,
`uq_productcategory_active`, `uq_prodcatalias_norm` (via `db._ensure_product_indexes`); plus the gate indexes
`ix_productpriceversion_product`, `ix_cataloggenerationjob_product`.

## Pricing (app/pricing.py — separate from quoting.py)
Decimal-exact landed-price calculator over `CostRate` + `FxRate`, Incoterm-specific components for
**EXW/FCA/FOB/CFR/CIF/CPT/CIP/DAP/DDP** (DDP → `needs_review` unless all destination costs are present).
Reports **margin%** (of price) and **markup%** (of cost) distinctly. Expired rates and missing duties/taxes are
surfaced ("expired" / "Not included" / "Required"), never guessed. Results are frozen into **immutable**
`ProductPriceVersion`s (draft→needs_review→approved→expired→archived; duplicate-to-revise). A read-only
`prefill_for_quote()` exists for Phase 6 but is **not** wired into `create_quote`.

## Catalog Studio (app/catalog_studio.py)
Provider abstraction: **BuiltinProvider** — a real, self-hosted HTML→PDF via **Playwright/Chromium**
(installed; degrades to an error → work item if no browser in prod); **HiggsProvider** — an honest
**"Not configured"** stub (no invented endpoints; env-gated; would send only approved fields + store a job id;
tested with mocks). Only APPROVED product fields + images are used; contact is always Go4it. PDFs are stored
privately (`product_files/`), admin-download-gated, **never auto-emailed**; an approved catalog may be
published seller-safe. A generation failure opens a `catalog_generation_failed` work item without blocking.

## Documents (private storage)
`ProductDocument` under `PRODUCT_FILES_DIR` (outside `/static`), validated via the tested
`attachments._secure_validate` (extension/MIME/size/dangerous/traversal), safe generated filename, original
name as metadata, admin-only upload, admin-or-seller-safe download, **archive not delete**, audited. This is
the proven `REQUEST_FILES_DIR` mechanism — NOT the disabled email-attachment path.

## Migration (run order)
```
backup_db.py → migrate.py → migrate_gate_p5.py [--dry-run] → backfill_products.py [--dry-run]
```
`migrate_gate_p5.py` explicitly + idempotently creates the 8 tables + constraints/indexes, runs
`PRAGMA integrity_check`, asserts operational counts (products/suppliers/quotes/deals/companies) invariant, and
verifies the indexes. `backfill_products.py` is **conservative**: creates inferred `ProductCategory` rows from
distinct free-text `Product.category` (+ aliases), links products, links `ProductSupplier` from
`Product.supplier_id`→`Supplier.company_id` (only when a company link exists), and **invents nothing** (no HS,
verification, reliability, prices, or activated expired rates). Idempotent.

## Rollback / recovery
- **Pre-go-live rollback:** `backfill_products.py --rollback` (unsets inferred `category_id`, deletes inferred
  categories/aliases/product-supplier links; PRESERVES the free-text `category` + all products), or restore
  the dated backup. Gate schema is additive (nothing to drop).
- **Post-go-live recovery:** `backfill_products.py --recover` removes only untouched inferred categories (no
  products, not merged); preserves every real admin change, price version and approved catalog.

## Hardening (pre-Phase 6)
- **Seller-safe access is tenant/owner-scoped.** `ProductDocument`/`CatalogGenerationJob` direct downloads are
  **admin-only**; `seller_safe=True` alone never grants a seller access. A seller sees a file ONLY after an
  admin **publishes it to a specific request** (`_publish_to_request` → a seller-safe `RequestDeliverable`),
  accessed via the existing **owner-scoped** `request_deliverable_file` (Seller A cannot read Seller B's
  files). Uploads default **private + quarantined**.
- **Document quarantine.** MIME/extension validation is not malware scanning, so every upload is
  `quarantine='quarantined'` and admin-only. Publication is limited to **Go4it-generated catalog PDFs** and
  **validated safe raster images** (`product_image`/`packaging_image`, `image/png|jpeg`); other docs need an
  explicit admin **scan-clear** (the malware-scan integration point) before they can be published.
- **PDF sandbox.** Every product field is HTML-escaped; the Playwright context runs with **JavaScript
  disabled** and **offline** with a `route("**/*")` guard that **blocks all network egress** (only inline
  `data:`/`about:` load — no `file://`, no remote URLs, no injected fetches); hard execution timeout +
  constrained launch flags. The HTML is fully self-contained.
- **Production Chromium.** `playwright==1.60.0` pinned; prod must `playwright install chromium` at that
  version and run `scripts/pdf_smoke.py` **inside the exact production container** (real PDF + fonts + embedded
  image + A4 + writable/private storage) before trusting Catalog Studio there.
- **Supplier navigation.** Products → Suppliers is a **product-focused projection**; the canonical
  supplier/company record is managed in the **Trade Network** (`/companies`) and is neither replaced nor
  duplicated (each supplier row links to its canonical company).

## Deferred / production-config
- **Higgs** — deferred pending real credentials/API docs (interface + "Not configured" + mocked tests shipped).
- **PDF in prod** — self-hosted Playwright works locally; prod containers need Chromium installed or it
  degrades to an error + a work item.
- **Quote prefill (Phase 6)** — `prefill_for_quote()` helper only; quotes/deals unchanged.
- **Live FX feed** — manual entry only (no unapproved external source).
