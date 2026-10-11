import sqlite3
from collections.abc import Generator
from contextlib import contextmanager
from typing import Any

from sqlalchemy import create_engine, event
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from app.core.config import get_settings
from app.db.base import Base

settings = get_settings()


def _get_connect_args(database_url: str) -> dict[str, Any]:
    """Build driver-specific connection arguments."""
    connect_args: dict[str, Any] = {}
    if database_url.startswith("sqlite"):
        # check_same_thread=False allows FastAPI threads to use SQLite safely
        connect_args["check_same_thread"] = False
        connect_args["timeout"] = settings.sqlite_busy_timeout_seconds
    return connect_args


engine: Engine = create_engine(
    settings.database_url,
    connect_args=_get_connect_args(settings.database_url),
    echo=settings.debug,
)


@event.listens_for(Engine, "connect")
def _set_sqlite_pragmas(dbapi_connection: Any, connection_record: Any) -> None:
    """Enforce foreign keys and enable WAL mode on SQLite connections."""
    if isinstance(dbapi_connection, sqlite3.Connection):
        cursor = dbapi_connection.cursor()
        # Enforce foreign key constraints
        cursor.execute("PRAGMA foreign_keys=ON;")

        # Enable WAL mode for disk databases (in-memory SQLite does not support WAL)
        cursor.execute("PRAGMA database_list;")
        databases = cursor.fetchall()
        is_memory = any(
            row[2] == "" or row[2] == ":memory:" or "memory" in row[2].lower()
            for row in databases
        )
        if not is_memory:
            cursor.execute("PRAGMA journal_mode=WAL;")

        cursor.close()


SessionLocal = sessionmaker(
    autocommit=False,
    autoflush=False,
    bind=engine,
    class_=Session,
)


def get_db() -> Generator[Session, None, None]:
    """FastAPI route dependency yielding a scoped database session.

    Ensures the session is always closed when the request lifecycle ends.
    """
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


@contextmanager
def get_db_context(session_factory: sessionmaker[Session] = SessionLocal) -> Generator[Session, None, None]:
    """Context manager yielding a standalone database session.

    Suitable for background worker tasks and testing outside of HTTP requests.
    Guarantees session closure on exit.
    """
    db = session_factory()
    try:
        yield db
    finally:
        db.close()


def init_db(target_engine: Engine | None = None) -> None:
    """Create all registered database tables in the target database.

    Explicitly imports app.models to ensure model metadata registration.
    Also handles SQLite schema backward compatibility for newly added columns.
    """
    import app.models  # noqa: F401
    from sqlalchemy import inspect, text

    bind_engine = target_engine or engine
    Base.metadata.create_all(bind=bind_engine)

    # Ensure backward compatibility for existing SQLite databases
    inspector = inspect(bind_engine)
    if "generation_jobs" in inspector.get_table_names():
        columns = {col["name"] for col in inspector.get_columns("generation_jobs")}
        if "issue_date" not in columns:
            with bind_engine.connect() as conn:
                conn.execute(text("ALTER TABLE generation_jobs ADD COLUMN issue_date DATE;"))
                conn.commit()
