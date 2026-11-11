"""Engine and session factory.

SQLite needs two settings on *every* connection (they are per-connection, not stored in the
file): foreign keys are off by default, and WAL lets the nightly job write while the UI
reads. A busy timeout avoids spurious "database is locked" errors under that overlap.
"""

from functools import lru_cache
from typing import Any

from sqlalchemy import Engine, create_engine, event
from sqlalchemy.orm import Session, sessionmaker

from equitypulse.config import get_settings


def _sqlite_on_connect(dbapi_connection: Any, _record: Any) -> None:
    cursor = dbapi_connection.cursor()
    try:
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA busy_timeout=5000")
    finally:
        cursor.close()


def make_engine(url: str, **kwargs: Any) -> Engine:
    """Engine for an explicit URL (tests, Alembic). Extra kwargs go to `create_engine`."""
    engine = create_engine(url, **kwargs)
    if engine.dialect.name == "sqlite":
        event.listen(engine, "connect", _sqlite_on_connect)
    return engine


@lru_cache(maxsize=1)
def get_engine() -> Engine:
    """Process-wide engine for the configured DATABASE_URL."""
    return make_engine(get_settings().database_url.get_secret_value())


def session_factory(engine: Engine | None = None) -> sessionmaker[Session]:
    # expire_on_commit=False: objects stay readable after commit, e.g. for audit summaries.
    return sessionmaker(engine or get_engine(), expire_on_commit=False)
