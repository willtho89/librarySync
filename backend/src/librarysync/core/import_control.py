from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from librarysync.core.import_all import (
    IMPORT_ALL_PROVIDER,
    import_all_active,
)
from librarysync.core.import_schedule import normalize_interval_seconds, parse_datetime
from librarysync.core.import_state import coerce_int, coerce_str, normalize_queue
from librarysync.db.models import Integration

QUICK_IMPORT_STATUS_PENDING = "pending"
QUICK_IMPORT_STATUS_IN_PROGRESS = "in_progress"
QUICK_IMPORT_STATUS_COMPLETED = "completed"
QUICK_IMPORT_STATUS_FAILED = "failed"

QUICK_IMPORT_STATUS_KEY = "quick_import_status"
QUICK_IMPORT_QUEUE_KEY = "quick_import_queue"
QUICK_IMPORT_INDEX_KEY = "quick_import_index"
QUICK_IMPORT_REQUESTED_KEY = "quick_import_requested_at"
QUICK_IMPORT_STARTED_KEY = "quick_import_started_at"
QUICK_IMPORT_COMPLETED_KEY = "quick_import_completed_at"
QUICK_IMPORT_ERROR_KEY = "quick_import_error"
QUICK_IMPORT_INTERVAL_KEY = "quick_import_interval_seconds"
QUICK_IMPORT_LAST_RUN_KEY = "quick_import_last_run_at"

QUICK_IMPORT_ACTIVE_STATUSES = {
    QUICK_IMPORT_STATUS_PENDING,
    QUICK_IMPORT_STATUS_IN_PROGRESS,
}

MERGE_REQUIRED_AT_KEY = "merge_required_at"
MERGE_COMPLETED_AT_KEY = "merge_completed_at"
MERGE_ERROR_KEY = "merge_error"


@dataclass(frozen=True)
class QuickImportState:
    status: str | None
    queue: list[str]
    index: int
    requested_at: datetime | None
    started_at: datetime | None
    completed_at: datetime | None
    error: str | None
    interval_seconds: int | None
    last_run_at: datetime | None


def parse_quick_import_state(config: dict | None) -> QuickImportState:
    config = config or {}
    status_raw = config.get(QUICK_IMPORT_STATUS_KEY)
    status = str(status_raw) if status_raw is not None else None
    queue = normalize_queue(config.get(QUICK_IMPORT_QUEUE_KEY))
    index = coerce_int(config.get(QUICK_IMPORT_INDEX_KEY)) or 0
    interval_seconds = normalize_interval_seconds(config.get(QUICK_IMPORT_INTERVAL_KEY))
    return QuickImportState(
        status=status,
        queue=queue,
        index=index,
        requested_at=parse_datetime(config.get(QUICK_IMPORT_REQUESTED_KEY)),
        started_at=parse_datetime(config.get(QUICK_IMPORT_STARTED_KEY)),
        completed_at=parse_datetime(config.get(QUICK_IMPORT_COMPLETED_KEY)),
        error=coerce_str(config.get(QUICK_IMPORT_ERROR_KEY)),
        interval_seconds=interval_seconds,
        last_run_at=parse_datetime(config.get(QUICK_IMPORT_LAST_RUN_KEY)),
    )


def quick_import_active(config: dict | None) -> bool:
    state = parse_quick_import_state(config)
    return bool(state.status in QUICK_IMPORT_ACTIVE_STATUSES)


def should_run_quick_import(config: dict | None, now: datetime) -> bool:
    state = parse_quick_import_state(config)
    if state.status in QUICK_IMPORT_ACTIVE_STATUSES:
        return True
    if state.requested_at and (state.last_run_at is None or state.requested_at > state.last_run_at):
        return True
    interval = state.interval_seconds
    if not interval:
        return False
    if state.last_run_at is None:
        return True
    return now - state.last_run_at >= timedelta(seconds=interval)


def next_quick_import_at(config: dict | None, now: datetime) -> datetime | None:
    state = parse_quick_import_state(config)
    if state.status in QUICK_IMPORT_ACTIVE_STATUSES:
        return state.started_at or state.requested_at or now
    if state.requested_at and (state.last_run_at is None or state.requested_at > state.last_run_at):
        return state.requested_at
    interval = state.interval_seconds
    if not interval:
        return None
    if state.last_run_at is None:
        return now
    return state.last_run_at + timedelta(seconds=interval)


def build_quick_import_config(
    config: dict | None,
    queue: list[str],
    requested_at: datetime,
) -> dict:
    updated = dict(config or {})
    updated[QUICK_IMPORT_STATUS_KEY] = QUICK_IMPORT_STATUS_PENDING
    updated[QUICK_IMPORT_QUEUE_KEY] = list(queue)
    updated[QUICK_IMPORT_INDEX_KEY] = 0
    updated[QUICK_IMPORT_REQUESTED_KEY] = requested_at.isoformat()
    updated[QUICK_IMPORT_STARTED_KEY] = None
    updated[QUICK_IMPORT_COMPLETED_KEY] = None
    updated[QUICK_IMPORT_ERROR_KEY] = None
    return updated


def mark_quick_import_started(config: dict | None, started_at: datetime) -> dict:
    updated = dict(config or {})
    updated[QUICK_IMPORT_STATUS_KEY] = QUICK_IMPORT_STATUS_IN_PROGRESS
    updated[QUICK_IMPORT_STARTED_KEY] = started_at.isoformat()
    return updated


def mark_quick_import_completed(config: dict | None, completed_at: datetime) -> dict:
    updated = dict(config or {})
    updated[QUICK_IMPORT_STATUS_KEY] = QUICK_IMPORT_STATUS_COMPLETED
    updated[QUICK_IMPORT_COMPLETED_KEY] = completed_at.isoformat()
    updated[QUICK_IMPORT_LAST_RUN_KEY] = completed_at.isoformat()
    updated[QUICK_IMPORT_ERROR_KEY] = None
    updated.pop(QUICK_IMPORT_REQUESTED_KEY, None)
    return updated


def mark_quick_import_failed(config: dict | None, failed_at: datetime, error: str) -> dict:
    updated = dict(config or {})
    updated[QUICK_IMPORT_STATUS_KEY] = QUICK_IMPORT_STATUS_FAILED
    updated[QUICK_IMPORT_COMPLETED_KEY] = failed_at.isoformat()
    updated[QUICK_IMPORT_ERROR_KEY] = error
    return updated


def set_quick_import_interval(config: dict | None, interval_seconds: int | None) -> dict:
    updated = dict(config or {})
    updated[QUICK_IMPORT_INTERVAL_KEY] = interval_seconds or 0
    return updated


def mark_merge_required(config: dict | None, required_at: datetime) -> dict:
    updated = dict(config or {})
    updated[MERGE_REQUIRED_AT_KEY] = required_at.isoformat()
    return updated


def mark_merge_completed(config: dict | None, completed_at: datetime) -> dict:
    updated = dict(config or {})
    updated[MERGE_COMPLETED_AT_KEY] = completed_at.isoformat()
    updated[MERGE_ERROR_KEY] = None
    return updated


def mark_merge_failed(config: dict | None, failed_at: datetime, error: str) -> dict:
    updated = dict(config or {})
    updated[MERGE_COMPLETED_AT_KEY] = failed_at.isoformat()
    updated[MERGE_ERROR_KEY] = error
    return updated


def merge_pending(config: dict | None) -> bool:
    config = config or {}
    required_at = parse_datetime(config.get(MERGE_REQUIRED_AT_KEY))
    completed_at = parse_datetime(config.get(MERGE_COMPLETED_AT_KEY))
    error = coerce_str(config.get(MERGE_ERROR_KEY))
    if not required_at:
        return False
    if not completed_at:
        return True
    if completed_at < required_at:
        return True
    return error is not None


async def load_blocked_outbox_users(db: AsyncSession) -> set[str]:
    result = await db.execute(
        select(Integration.user_id, Integration.config).where(
            Integration.provider == IMPORT_ALL_PROVIDER
        )
    )
    blocked: set[str] = set()
    for user_id, config in result.all():
        if import_all_active(config) or quick_import_active(config) or merge_pending(config):
            blocked.add(str(user_id))
    return blocked
