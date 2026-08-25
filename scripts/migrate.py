"""Lightweight idempotent migrations for the SQLite DB.

    ./.venv/bin/python scripts/migrate.py     (or: make db-migrate)

SQLModel's create_all() creates missing TABLES but never ALTERs existing ones, so new columns on
an existing table need this. Each entry is (table, column, sqlite_type_with_default); adding a
column that already exists is skipped. Safe to run repeatedly. Run after pulling changes that add
columns, before starting the app.
"""
import os
import sqlite3

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///data.db")


def _db_path():
    """The on-disk SQLite file this migration should ALTER, derived from DATABASE_URL so it matches the
    file the app actually serves (in Docker that's /app/var/go4it.db, not <repo>/data.db). Returns None
    for a non-SQLite URL (Postgres — schema handled by create_all) or an in-memory DB."""
    try:
        from sqlalchemy.engine import make_url
        url = make_url(DATABASE_URL)
    except Exception:  # noqa: BLE001 — fall back to the legacy repo path
        return os.path.join(BASE, "data.db")
    if url.get_backend_name() != "sqlite":
        return None
    path = url.database
    if not path or path == ":memory:":
        return None
    return path if os.path.isabs(path) else os.path.join(BASE, path)


MIGRATIONS = [
    ("lead", "next_action_at", "TIMESTAMP"),
    ("lead", "next_action_note", "VARCHAR DEFAULT ''"),
    ("quote", "share_token", "VARCHAR DEFAULT ''"),
    ("lead", "buyer_replied_at", "TIMESTAMP"),
    ("outreach", "direction", "VARCHAR DEFAULT 'out'"),
    ("outreach", "from_addr", "VARCHAR DEFAULT ''"),
    ("outreach", "message_id", "VARCHAR DEFAULT ''"),
    ("lead", "accepted_at", "TIMESTAMP"),
    ("quote", "accepted_at", "TIMESTAMP"),
    ("quote", "buyer_response", "VARCHAR DEFAULT ''"),
    ("ratecard", "dest_country", "VARCHAR DEFAULT ''"),
    ("quote", "owner_id", "INTEGER"),          # tenant scope; backfilled from lead.owner_id below
    ("servicerequest", "result_file_path", "VARCHAR DEFAULT ''"),   # Phase 3 file delivery
    ("servicerequest", "result_url", "VARCHAR DEFAULT ''"),
    ("lead", "active", "BOOLEAN DEFAULT 1"),   # reversible "unlist" — existing buyers stay listed
    # confidential managed-outreach pipeline
    ("lead", "managed", "BOOLEAN DEFAULT 0"),
    ("lead", "seller_id", "INTEGER"),
    ("lead", "request_id", "INTEGER"),
    ("lead", "pipeline_stage", "VARCHAR DEFAULT 'identified'"),
    ("lead", "anon_ref", "VARCHAR DEFAULT ''"),
    ("lead", "assigned_admin_id", "INTEGER"),
    ("lead", "buyer_category", "VARCHAR DEFAULT ''"),
    ("lead", "company_size_band", "VARCHAR DEFAULT ''"),
    ("lead", "fit_score", "FLOAT DEFAULT 0"),
    ("lead", "seller_action_required", "BOOLEAN DEFAULT 0"),
    ("requestdeliverable", "seller_safe", "BOOLEAN DEFAULT 0"),
    ("mailaccount", "admin_owned", "BOOLEAN DEFAULT 0"),
    # these live on the NEW tables (create_all makes them with the columns); only needed where the table
    # already existed from an earlier boot — the loop skips them if the table isn't there yet.
    ("stageevent", "inferred", "BOOLEAN DEFAULT 0"),
    ("sellerupdate", "status", "VARCHAR DEFAULT 'open'"),
    ("sellerupdate", "resolved_at", "TIMESTAMP"),
    ("sellerupdate", "resolved_by", "VARCHAR DEFAULT ''"),
    ("auditlog", "tenant_id", "INTEGER"),
    # Trade Network (Phase 2) — additive links on existing tables (new tables handled by create_all)
    ("lead", "company_id", "INTEGER"),
    ("lead", "engagement_class", "VARCHAR DEFAULT ''"),
    ("lead", "reply_outcome", "VARCHAR DEFAULT ''"),
    ("supplier", "company_id", "INTEGER"),
    # Requests + Work Queue (Phase 3) — additive columns on the existing servicerequest table.
    # Legacy status/request_type are UNCHANGED; these enrich the admin surface only. The new
    # workitem / requeststatusevent TABLES are made (with all their columns/indexes) by create_all.
    ("servicerequest", "direction", "VARCHAR DEFAULT 'sell'"),
    ("servicerequest", "workflow_status", "VARCHAR DEFAULT ''"),
    ("servicerequest", "priority", "VARCHAR DEFAULT 'normal'"),
    ("servicerequest", "assigned_admin_id", "INTEGER"),
    ("servicerequest", "due_at", "TIMESTAMP"),
    ("servicerequest", "last_activity_at", "TIMESTAMP"),
    ("servicerequest", "next_action_note", "VARCHAR DEFAULT ''"),
    ("servicerequest", "action_required_admin", "BOOLEAN DEFAULT 0"),
    ("servicerequest", "action_required_requester", "BOOLEAN DEFAULT 0"),
    ("servicerequest", "on_behalf_company_id", "INTEGER"),
    ("servicerequest", "admin_last_read_at", "TIMESTAMP"),
    ("servicerequest", "requester_last_read_at", "TIMESTAMP"),
    # durable-disposition column on the workitem table (only needed on DBs where workitem already existed
    # from an earlier boot; fresh DBs get it from create_all — the loop skips absent tables).
    ("workitem", "condition_version", "VARCHAR DEFAULT ''"),
    # Outreach, Campaigns & Email (Phase 4) — additive columns on the existing outreach + mailaccount tables.
    # The new Phase-4 tables (campaign/campaignstep/campaignrecipient/emailtemplate/suppression/bouncerecord/
    # outreachcontrol) are made by create_all; the loop below skips their columns until the table exists.
    ("outreach", "campaign_id", "INTEGER"),
    ("outreach", "campaign_recipient_id", "INTEGER"),
    ("outreach", "campaign_version", "INTEGER DEFAULT 0"),
    ("outreach", "campaign_step", "INTEGER DEFAULT 0"),
    ("outreach", "in_reply_to", "VARCHAR DEFAULT ''"),
    ("mailaccount", "daily_limit", "INTEGER DEFAULT 200"),
    ("mailaccount", "sent_today", "INTEGER DEFAULT 0"),
    ("mailaccount", "sent_today_date", "VARCHAR DEFAULT ''"),
    ("mailaccount", "paused", "BOOLEAN DEFAULT 0"),
    ("mailaccount", "imap_host", "VARCHAR DEFAULT ''"),
    ("mailaccount", "imap_port", "INTEGER DEFAULT 993"),
    ("mailaccount", "imap_user", "VARCHAR DEFAULT ''"),
    ("mailaccount", "imap_password_enc", "VARCHAR DEFAULT ''"),
    ("mailaccount", "last_inbound_at", "TIMESTAMP"),
    ("mailaccount", "last_outbound_at", "TIMESTAMP"),
    ("mailaccount", "last_send_error", "VARCHAR DEFAULT ''"),
    ("mailaccount", "spf_status", "VARCHAR DEFAULT ''"),
    ("mailaccount", "dkim_status", "VARCHAR DEFAULT ''"),
    ("mailaccount", "dmarc_status", "VARCHAR DEFAULT ''"),
    ("mailaccount", "updated_at", "TIMESTAMP"),
    ("mailaccount", "cred_enc_version", "INTEGER DEFAULT 1"),   # Phase-4 gate: credential encryption version
    # Phase-4 gate: durable RFC Message-ID on the crash-safe send record (only needed where campaignsend
    # already existed from an earlier boot; fresh DBs get it from create_all — the loop skips absent tables).
    ("campaignsend", "rfc_message_id", "VARCHAR DEFAULT ''"),
    # Products, Catalogs, Suppliers & Pricing (Phase 5) — additive columns on the existing product/supplier/
    # fxrate tables. The new Phase-5 TABLES (productcategory/productcategoryalias/productvariant/
    # productsupplier/costrate/productpriceversion/productdocument/cataloggenerationjob) are made by
    # create_all + scripts/migrate_gate_p5.py; the loop below skips their columns until the table exists.
    ("product", "sku", "VARCHAR DEFAULT ''"),
    ("product", "short_description", "VARCHAR DEFAULT ''"),
    ("product", "category_id", "INTEGER"),
    ("product", "subcategory", "VARCHAR DEFAULT ''"),
    ("product", "brand", "VARCHAR DEFAULT ''"),
    ("product", "grade", "VARCHAR DEFAULT ''"),
    ("product", "origin_country", "VARCHAR DEFAULT ''"),
    ("product", "origin_city", "VARCHAR DEFAULT ''"),
    ("product", "producer", "VARCHAR DEFAULT ''"),
    ("product", "units_per_package", "FLOAT DEFAULT 0"),
    ("product", "production_capacity", "VARCHAR DEFAULT ''"),
    ("product", "lead_time_days", "INTEGER DEFAULT 0"),
    ("product", "shelf_life", "VARCHAR DEFAULT ''"),
    ("product", "storage_requirements", "VARCHAR DEFAULT ''"),
    ("product", "certifications", "VARCHAR DEFAULT ''"),
    ("product", "incoterms", "VARCHAR DEFAULT ''"),
    ("product", "status", "VARCHAR DEFAULT 'active'"),
    ("product", "completeness_score", "INTEGER DEFAULT 0"),
    ("product", "verification_status", "VARCHAR DEFAULT 'unverified'"),
    ("product", "verified_at", "TIMESTAMP"),
    ("product", "verified_by", "VARCHAR DEFAULT ''"),
    ("product", "internal_notes", "VARCHAR DEFAULT ''"),
    ("product", "created_at", "TIMESTAMP"),
    ("supplier", "reliability_rated", "BOOLEAN DEFAULT 0"),
    ("workitem", "related_product_id", "INTEGER"),
    ("productdocument", "quarantine", "VARCHAR DEFAULT 'quarantined'"),   # Phase-5 hardening
    ("fxrate", "source", "VARCHAR DEFAULT ''"),
    ("fxrate", "kind", "VARCHAR DEFAULT 'manual'"),
    ("fxrate", "retrieved_at", "TIMESTAMP"),
    ("fxrate", "expires_at", "TIMESTAMP"),
    ("fxrate", "verified_by", "VARCHAR DEFAULT ''"),
    ("fxrate", "active", "BOOLEAN DEFAULT 1"),
    # Commercial: Quotes/Contracts/Deals (Phase 6) — additive columns on existing quote/deal/workitem.
    # New Phase-6 tables are made by create_all + scripts/migrate_gate_p6.py.
    ("quote", "current_version_id", "INTEGER"),
    ("quote", "viewed_at", "TIMESTAMP"),
    ("deal", "quote_version_id", "INTEGER"),
    ("deal", "ready_for_ops", "BOOLEAN DEFAULT 0"),
    ("workitem", "related_contract_id", "INTEGER"),
    ("quoteaccesstoken", "consumed_at", "TIMESTAMP"),   # portal hardening: single-use link consumption
    # Operations (Phase 7) — additive WorkItem linkage columns to the new operational entities. New Phase-7
    # tables are made by create_all + scripts/migrate_gate_p7.py.
    ("workitem", "related_operation_case_id", "INTEGER"),
    ("workitem", "related_shipment_id", "INTEGER"),
    ("workitem", "related_payment_id", "INTEGER"),
    ("workitem", "related_exception_id", "INTEGER"),
    # Intelligence (Phase 8) — additive WorkItem linkage columns. New Phase-8 tables are made by create_all +
    # scripts/migrate_gate_p8.py.
    ("workitem", "related_opportunity_id", "INTEGER"),
    ("workitem", "related_alert_id", "INTEGER"),
    # Phase 8 correction: separate the SAME-commercial-event grouping + backfilled provenance from the reserved
    # 'inferred' (assumption) flag on demand signals.
    ("demandsignal", "commercial_event_key", "VARCHAR DEFAULT ''"),
    ("demandsignal", "backfilled", "BOOLEAN DEFAULT 0"),
    ("demandsignal", "history_complete", "BOOLEAN DEFAULT 1"),
    # AI Command (Phase 9) — additive WorkItem linkage columns. New Phase-9 tables are made by create_all +
    # scripts/migrate_gate_p9.py.
    ("workitem", "related_conversation_id", "INTEGER"),
    ("workitem", "related_proposal_id", "INTEGER"),
    ("workitem", "related_automation_id", "INTEGER"),
]


def run():
    db = _db_path()
    if db is None:
        print("non-SQLite (or in-memory) DATABASE_URL — additive migrations handled by create_all; skipping")
        return
    if not os.path.exists(db):
        print(f"no DB yet at {db} — create_all will include new columns on first run")
        return
    con = sqlite3.connect(db)
    cur = con.cursor()
    # Tables that don't exist yet are created (with all their columns) by create_all()/init_db on app boot —
    # skip their column migrations here so a fresh DB doesn't hit "no such table" ALTERs.
    existing_tables = {r[0] for r in cur.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    applied = 0
    for table, column, decl in MIGRATIONS:
        if table not in existing_tables:
            continue
        cols = {r[1] for r in cur.execute(f"PRAGMA table_info({table})")}
        if column not in cols:
            cur.execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")
            print(f"+ {table}.{column}")
            applied += 1
    # Backfill quote.owner_id from the owning lead (tenant scope) for any rows still NULL — so pre-existing
    # quotes are correctly isolated the moment scoping goes live. Idempotent (only touches NULLs).
    qcols = {r[1] for r in cur.execute("PRAGMA table_info(quote)")}
    if "owner_id" in qcols:
        cur.execute("UPDATE quote SET owner_id = (SELECT owner_id FROM lead WHERE lead.id = quote.lead_id) "
                    "WHERE owner_id IS NULL")
        if cur.rowcount:
            print(f"~ backfilled quote.owner_id for {cur.rowcount} row(s)")

    # Indexes the models declare (Field(index=True)) that ALTER TABLE ADD COLUMN doesn't create.
    # create_all() makes them on fresh DBs; migrated DBs need them here to match (else full scans).
    tables = {r[0] for r in cur.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    for idx, table, column in [("ix_quote_share_token", "quote", "share_token"),
                               ("ix_outreach_message_id", "outreach", "message_id"),
                               ("ix_quote_owner_id", "quote", "owner_id"),
                               ("ix_lead_seller_id", "lead", "seller_id"),
                               ("ix_lead_request_id", "lead", "request_id"),
                               ("ix_lead_anon_ref", "lead", "anon_ref"),
                               ("ix_auditlog_tenant_id", "auditlog", "tenant_id"),
                               ("ix_lead_company_id", "lead", "company_id"),
                               ("ix_supplier_company_id", "supplier", "company_id"),
                               ("ix_servicerequest_assigned_admin_id", "servicerequest", "assigned_admin_id"),
                               ("ix_outreach_campaign_id", "outreach", "campaign_id"),
                               ("ix_product_sku", "product", "sku"),
                               ("ix_product_category_id", "product", "category_id"),
                               ("ix_product_origin_country", "product", "origin_country"),
                               ("ix_product_supplier_id", "product", "supplier_id")]:
        if table in tables:
            cur.execute(f"CREATE INDEX IF NOT EXISTS {idx} ON {table}({column})")
    # DB-level anon_ref uniqueness: a PARTIAL unique index so blank refs never collide but two buyers in one
    # request can never share a reference.
    if "lead" in tables and "anon_ref" in {r[1] for r in cur.execute("PRAGMA table_info(lead)")}:
        cur.execute("CREATE UNIQUE INDEX IF NOT EXISTS uq_lead_req_anonref "
                    "ON lead(request_id, anon_ref) WHERE anon_ref != ''")
    con.commit()
    con.close()
    print(f"migrations applied: {applied}")


if __name__ == "__main__":
    run()
