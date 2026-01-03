from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import uuid4

from librarysync.core.import_schedule import parse_datetime

IMPORT_HISTORY_KEY = "import_history"
IMPORT_HISTORY_MAX = 50


def build_import_history_entry(
    *,
    event_type: str,
    status: str | None,
    requested_at: datetime | None,
    started_at: datetime | None,
    completed_at: datetime | None,
    error: str | None,
    queue: list[str] | None,
    merge_required_at: datetime | None = None,
    merge_completed_at: datetime | None = None,
    merge_error: str | None = None,
) -> dict:
    return {
        "id": str(uuid4()),
        "event_type": event_type,
        "status": status,
        "requested_at": requested_at.isoformat() if requested_at else None,
        "started_at": started_at.isoformat() if started_at else None,
        "completed_at": completed_at.isoformat() if completed_at else None,
        "error": error,
        "queue": list(queue or []),
        "merge_required_at": merge_required_at.isoformat() if merge_required_at else None,
        "merge_completed_at": merge_completed_at.isoformat() if merge_completed_at else None,
        "merge_error": merge_error,
    }


def append_import_history(config: dict | None, entry: dict) -> dict:
    updated = dict(config or {})
    history = updated.get(IMPORT_HISTORY_KEY)
    if not isinstance(history, list):
        history = []
    history.append(entry)
    if len(history) > IMPORT_HISTORY_MAX:
        history = history[-IMPORT_HISTORY_MAX:]
    updated[IMPORT_HISTORY_KEY] = history
    return updated


def parse_import_history(config: dict | None) -> list[dict]:
    config = config or {}
    history = config.get(IMPORT_HISTORY_KEY)
    if not isinstance(history, list):
        return []
    entries: list[dict] = []
    for raw in history:
        if not isinstance(raw, dict):
            continue
        event_type = _coerce_str(raw.get("event_type"))
        if not event_type:
            continue
        entry_id = _coerce_str(raw.get("id")) or str(uuid4())
        entries.append(
            {
                "id": entry_id,
                "event_type": event_type,
                "status": _coerce_str(raw.get("status")),
                "requested_at": parse_datetime(raw.get("requested_at")),
                "started_at": parse_datetime(raw.get("started_at")),
                "completed_at": parse_datetime(raw.get("completed_at")),
                "error": _coerce_str(raw.get("error")),
                "queue": _normalize_queue(raw.get("queue")),
                "merge_required_at": parse_datetime(raw.get("merge_required_at")),
                "merge_completed_at": parse_datetime(raw.get("merge_completed_at")),
                "merge_error": _coerce_str(raw.get("merge_error")),
            }
        )
    return entries


def _normalize_queue(value: Any) -> list[str]:
    if isinstance(value, list):
        return [str(entry) for entry in value if entry]
    return []


def _coerce_str(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    cleaned = value.strip()
    return cleaned or None
