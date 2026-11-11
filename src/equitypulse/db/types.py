"""Custom column types."""

import datetime as dt
from typing import Any

from sqlalchemy import DateTime, Dialect
from sqlalchemy.types import TypeDecorator


class UTCDateTime(TypeDecorator[dt.datetime]):
    """Timezone-aware UTC timestamps on every backend.

    SQLite has no timezone type and silently drops tzinfo, and a naive datetime is ambiguous
    (IST or UTC?). So naive values are refused on write, aware values are stored as UTC, and
    every value read back is aware UTC.
    """

    impl = DateTime(timezone=True)
    cache_ok = True

    def process_bind_param(self, value: dt.datetime | None, dialect: Dialect) -> Any:
        if value is None:
            return None
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError(f"naive datetime {value!r}: timestamps must be timezone-aware")
        value = value.astimezone(dt.UTC)
        return value.replace(tzinfo=None) if dialect.name == "sqlite" else value

    def process_result_value(self, value: dt.datetime | None, dialect: Dialect) -> Any:
        if value is None:
            return None
        if value.tzinfo is None:
            return value.replace(tzinfo=dt.UTC)
        return value.astimezone(dt.UTC)


def utcnow() -> dt.datetime:
    return dt.datetime.now(dt.UTC)
