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
    """Create all tables if they don't exist yet."""
    SQLModel.metadata.create_all(engine)
