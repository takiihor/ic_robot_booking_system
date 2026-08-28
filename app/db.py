"""Database engine, session handling and schema creation."""
from __future__ import annotations

import logging
from collections.abc import Iterator

from sqlalchemy import create_engine, event
from sqlalchemy.orm import Session, sessionmaker

from app.config import DATABASE_URL, DB_PATH

log = logging.getLogger(__name__)

if DATABASE_URL.startswith("sqlite:///") and not DATABASE_URL.endswith(":memory:"):
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)

engine = create_engine(
    DATABASE_URL,
    future=True,
    connect_args={"check_same_thread": False} if DATABASE_URL.startswith("sqlite") else {},
)


@event.listens_for(engine, "connect")
def _sqlite_pragmas(dbapi_connection, _record):
    """Enforce foreign keys and use WAL so reads don't block the UI."""
    if not DATABASE_URL.startswith("sqlite"):
        return
    cursor = dbapi_connection.cursor()
    cursor.execute("PRAGMA foreign_keys=ON")
    cursor.execute("PRAGMA journal_mode=WAL")
    cursor.close()


SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False, future=True)


def get_db() -> Iterator[Session]:
    """FastAPI dependency yielding a request-scoped session."""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def init_db() -> None:
    from app.models import Base  # imported here so models register their tables

    Base.metadata.create_all(engine)
    log.info("Database ready at %s", DATABASE_URL)
