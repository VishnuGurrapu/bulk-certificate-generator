from datetime import datetime, timezone
from typing import Any, Optional

from sqlalchemy import DateTime
from sqlalchemy.engine import Dialect
from sqlalchemy.types import TypeDecorator


class UTCDateTime(TypeDecorator):
    """A DateTime type that safely stores and retrieves timezone-aware UTC datetimes.

    In databases without native timezone storage (such as SQLite), standard DateTime
    columns return naive datetimes on deserialization. UTCDateTime guarantees that
    retrieved values always possess tzinfo=timezone.utc.
    """

    impl = DateTime(timezone=True)
    cache_ok = True

    def process_bind_param(
        self, value: Optional[datetime], dialect: Dialect
    ) -> Optional[datetime]:
        if value is not None:
            if not isinstance(value, datetime):
                raise TypeError(f"Expected datetime object, got {type(value).__name__}")
            if value.tzinfo is None:
                return value.replace(tzinfo=timezone.utc)
            return value.astimezone(timezone.utc)
        return None

    def process_result_value(
        self, value: Optional[datetime], dialect: Dialect
    ) -> Optional[datetime]:
        if value is not None:
            if value.tzinfo is None:
                return value.replace(tzinfo=timezone.utc)
            return value.astimezone(timezone.utc)
        return None
