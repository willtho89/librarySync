from __future__ import annotations

from datetime import datetime, timezone
from typing import Any


def normalize_interval_seconds(value: Any) -> int | None:
    if value is None:
        return None
    try:
        interval = int(value)
    except (TypeError, ValueError):
        return None
    if interval < 0:
        return None
    return interval


def parse_datetime(value: Any) -> datetime | None:
    if not value:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
