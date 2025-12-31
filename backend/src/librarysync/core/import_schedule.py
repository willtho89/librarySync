from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

IMPORT_INTERVAL_KEY = "import_interval_seconds"
IMPORT_LAST_RUN_KEY = "import_last_run_at"
IMPORT_REQUESTED_KEY = "import_requested_at"
DEFAULT_IMPORT_INTERVAL_SECONDS = 24 * 60 * 60


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


def should_run_import(
    config: dict | None,
    now: datetime,
    default_interval_seconds: int | None = DEFAULT_IMPORT_INTERVAL_SECONDS,
) -> bool:
    config = config or {}
    interval = normalize_interval_seconds(config.get(IMPORT_INTERVAL_KEY))
    if interval is None:
        interval = default_interval_seconds
    requested_at = parse_datetime(config.get(IMPORT_REQUESTED_KEY))
    last_run = parse_datetime(config.get(IMPORT_LAST_RUN_KEY))
    if requested_at and (last_run is None or requested_at > last_run):
        return True
    if not interval:
        return False
    if last_run is None:
        return True
    return now - last_run >= timedelta(seconds=interval)


def compute_next_import_at(
    config: dict | None,
    now: datetime,
    default_interval_seconds: int | None = DEFAULT_IMPORT_INTERVAL_SECONDS,
) -> datetime | None:
    config = config or {}
    interval = normalize_interval_seconds(config.get(IMPORT_INTERVAL_KEY))
    if interval is None:
        interval = default_interval_seconds
    requested_at = parse_datetime(config.get(IMPORT_REQUESTED_KEY))
    last_run = parse_datetime(config.get(IMPORT_LAST_RUN_KEY))
    if requested_at and (last_run is None or requested_at > last_run):
        return requested_at
    if not interval:
        return None
    if last_run is None:
        return now
    return last_run + timedelta(seconds=interval)


def record_import_run(config: dict | None, now: datetime) -> dict:
    updated = dict(config or {})
    updated[IMPORT_LAST_RUN_KEY] = now.isoformat()
    updated.pop(IMPORT_REQUESTED_KEY, None)
    return updated


def set_import_requested(config: dict | None, now: datetime) -> dict:
    updated = dict(config or {})
    updated[IMPORT_REQUESTED_KEY] = now.isoformat()
    return updated


def set_import_interval(config: dict | None, interval_seconds: int | None) -> dict:
    updated = dict(config or {})
    updated[IMPORT_INTERVAL_KEY] = interval_seconds or 0
    return updated
