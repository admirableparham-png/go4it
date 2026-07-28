"""Database engine + session helpers."""
from sqlmodel import SQLModel, Session, create_engine

from .config import DATABASE_URL

# check_same_thread is a SQLite-only setting so FastAPI's threads can share it.
connect_args = {"check_same_thread": False} if DATABASE_URL.startswith("sqlite") else {}
engine = create_engine(DATABASE_URL, echo=False, connect_args=connect_args)


def init_db() -> None:
    """Create all tables if they don't exist yet."""
    SQLModel.metadata.create_all(engine)
