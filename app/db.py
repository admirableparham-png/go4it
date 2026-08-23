"""Database engine + session helpers."""
from sqlalchemy import event
from sqlmodel import SQLModel, Session, create_engine

from .config import DATABASE_URL

_is_sqlite = DATABASE_URL.startswith("sqlite")

# check_same_thread lets FastAPI's threadpool share the connection; timeout is the
# busy-wait before giving up on a locked database.
connect_args = {"check_same_thread": False, "timeout": 30} if _is_sqlite else {}
engine = create_engine(DATABASE_URL, echo=False, connect_args=connect_args)


if _is_sqlite:
    @event.listens_for(engine, "connect")
    def _set_sqlite_pragmas(dbapi_conn, _record):
        """WAL lets readers run alongside a writer; a long busy_timeout plus WAL
        virtually eliminates 'database is locked' under this app's short writes."""
        cur = dbapi_conn.cursor()
        cur.execute("PRAGMA journal_mode=WAL;")
        cur.execute("PRAGMA busy_timeout=30000;")
        cur.execute("PRAGMA synchronous=NORMAL;")
        cur.close()


def init_db() -> None:
    """Create all tables if they don't exist yet, and ensure the DB-level uniqueness guards."""
    SQLModel.metadata.create_all(engine)
    _ensure_anon_ref_unique()
    _ensure_trade_network_indexes()
    _ensure_workitem_indexes()


def _ensure_trade_network_indexes() -> None:
    """Trade Network (Phase 2) DB-level guards, created on every startup (idempotent, never blocks boot):
    unique company-role, unique dedup-candidate pair, and a PARTIAL unique provenance index (only where a
    source_ref exists — so blank-ref provenance rows never collide but a re-ingest is a no-op)."""
    if not _is_sqlite:
        return
    try:
        with engine.connect() as conn:
            from sqlalchemy import text
            conn.execute(text("CREATE UNIQUE INDEX IF NOT EXISTS uq_companyrole_company_role "
                              "ON companyrole(company_id, role)"))
            conn.execute(text("CREATE UNIQUE INDEX IF NOT EXISTS uq_dupcand_pair "
                              "ON duplicatecandidate(left_id, right_id)"))
            conn.execute(text("CREATE UNIQUE INDEX IF NOT EXISTS uq_provenance_dedup "
                              "ON provenance(entity_type, entity_id, source_type, source_ref) "
                              "WHERE source_ref != ''"))
            conn.commit()
    except Exception:  # noqa: BLE001 — never block startup on the guard
        pass


def _ensure_workitem_indexes() -> None:
    """Requests + Work Queue (Phase 3) DB-level guard: a PARTIAL-unique index on the automatic-item
    idempotency_key over OPEN statuses only — so re-running work-item synchronization can never create a
    duplicate OPEN task, while completed/dismissed history and blank-key manual items never collide. Runs on
    every startup (fresh + migrated DBs); idempotent; never blocks boot."""
    if not _is_sqlite:
        return
    try:
        with engine.connect() as conn:
            from sqlalchemy import text
            conn.execute(text("CREATE UNIQUE INDEX IF NOT EXISTS uq_workitem_idem_open "
                              "ON workitem(idempotency_key) "
                              "WHERE idempotency_key != '' AND status IN ('open','in_progress','waiting')"))
            conn.commit()
    except Exception:  # noqa: BLE001 — never block startup on the guard
        pass


def _ensure_anon_ref_unique() -> None:
    """A DB-level UNIQUE constraint on (request_id, anon_ref) via a PARTIAL unique index (only where a ref
    is actually assigned) — so blank anon_ref rows never collide, but two buyers in one request can never
    share a reference. Runs on every startup (fresh + existing DBs); idempotent."""
    if not _is_sqlite:
        return
    try:
        with engine.connect() as conn:
            from sqlalchemy import text
            conn.execute(text(
                "CREATE UNIQUE INDEX IF NOT EXISTS uq_lead_req_anonref "
                "ON lead(request_id, anon_ref) WHERE anon_ref != ''"))
            conn.commit()
    except Exception:  # noqa: BLE001 — never block startup on the guard (migrate.py also creates it)
        pass
