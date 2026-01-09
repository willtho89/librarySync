from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Iterable

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from librarysync.config import settings
from librarysync.connectors.services.aiostreams_proxy import has_required_aiostreams_fields
from librarysync.connectors.services.anilist import has_required_anilist_fields
from librarysync.connectors.services.letterboxd import has_required_letterboxd_fields
from librarysync.connectors.services.simkl import has_required_simkl_fields
from librarysync.connectors.services.stremio import has_required_stremio_fields
from librarysync.connectors.services.trakt import has_required_trakt_fields
from librarysync.core.import_schedule import parse_datetime
from librarysync.core.integrations import load_integration_with_secrets
from librarysync.db.models import Integration

IMPORT_ALL_PROVIDER = "system"
IMPORT_ALL_PRIORITY = (
    "trakt",
    "letterboxd",
    "simkl",
    "anilist",
    "stremio",
    "aiostreams",
)
DEFAULT_IMPORT_QUEUE_ORDER = (
    "trakt",
    "letterboxd",
    "simkl",
    "anilist",
    "stremio",
    "aiostreams",
)

IMPORT_ALL_STATUS_PENDING = "pending"
IMPORT_ALL_STATUS_IN_PROGRESS = "in_progress"
IMPORT_ALL_STATUS_COMPLETED = "completed"
IMPORT_ALL_STATUS_FAILED = "failed"

IMPORT_ALL_STATUS_KEY = "import_all_status"
IMPORT_ALL_QUEUE_KEY = "import_all_queue"
IMPORT_ALL_INDEX_KEY = "import_all_index"
IMPORT_QUEUE_ORDER_KEY = "import_queue_order"
IMPORT_ALL_REQUESTED_KEY = "import_all_requested_at"
IMPORT_ALL_STARTED_KEY = "import_all_started_at"
IMPORT_ALL_COMPLETED_KEY = "import_all_completed_at"
IMPORT_ALL_ERROR_KEY = "import_all_error"

IMPORT_ALL_ACTIVE_STATUSES = {
    IMPORT_ALL_STATUS_PENDING,
    IMPORT_ALL_STATUS_IN_PROGRESS,
}


@dataclass(frozen=True)
class ImportAllState:
    status: str | None
    queue: list[str]
    index: int
    requested_at: datetime | None
    started_at: datetime | None
    completed_at: datetime | None
    error: str | None


def parse_import_all_state(config: dict | None) -> ImportAllState:
    config = config or {}
    status_raw = config.get(IMPORT_ALL_STATUS_KEY)
    status = str(status_raw) if status_raw is not None else None
    queue = _normalize_queue(config.get(IMPORT_ALL_QUEUE_KEY))
    index = _coerce_int(config.get(IMPORT_ALL_INDEX_KEY)) or 0
    return ImportAllState(
        status=status,
        queue=queue,
        index=index,
        requested_at=parse_datetime(config.get(IMPORT_ALL_REQUESTED_KEY)),
        started_at=parse_datetime(config.get(IMPORT_ALL_STARTED_KEY)),
        completed_at=parse_datetime(config.get(IMPORT_ALL_COMPLETED_KEY)),
        error=_coerce_str(config.get(IMPORT_ALL_ERROR_KEY)),
    )


def build_import_all_config(
    config: dict | None,
    queue: Iterable[str],
    requested_at: datetime,
) -> dict:
    updated = dict(config or {})
    updated[IMPORT_ALL_STATUS_KEY] = IMPORT_ALL_STATUS_PENDING
    updated[IMPORT_ALL_QUEUE_KEY] = list(queue)
    updated[IMPORT_ALL_INDEX_KEY] = 0
    updated[IMPORT_ALL_REQUESTED_KEY] = requested_at.isoformat()
    updated[IMPORT_ALL_STARTED_KEY] = None
    updated[IMPORT_ALL_COMPLETED_KEY] = None
    updated[IMPORT_ALL_ERROR_KEY] = None
    return updated


def mark_import_all_started(config: dict | None, started_at: datetime) -> dict:
    updated = dict(config or {})
    updated[IMPORT_ALL_STATUS_KEY] = IMPORT_ALL_STATUS_IN_PROGRESS
    updated[IMPORT_ALL_STARTED_KEY] = started_at.isoformat()
    return updated


def mark_import_all_completed(config: dict | None, completed_at: datetime) -> dict:
    updated = dict(config or {})
    updated[IMPORT_ALL_STATUS_KEY] = IMPORT_ALL_STATUS_COMPLETED
    updated[IMPORT_ALL_COMPLETED_KEY] = completed_at.isoformat()
    return updated


def mark_import_all_failed(config: dict | None, failed_at: datetime, error: str) -> dict:
    updated = dict(config or {})
    updated[IMPORT_ALL_STATUS_KEY] = IMPORT_ALL_STATUS_FAILED
    updated[IMPORT_ALL_COMPLETED_KEY] = failed_at.isoformat()
    updated[IMPORT_ALL_ERROR_KEY] = error
    return updated


def import_all_active(config: dict | None) -> bool:
    state = parse_import_all_state(config)
    return bool(state.status in IMPORT_ALL_ACTIVE_STATUSES)


async def get_or_create_system_integration(
    db: AsyncSession, user_id: str
) -> Integration:
    result = await db.execute(
        select(Integration).where(
            Integration.user_id == user_id,
            Integration.provider == IMPORT_ALL_PROVIDER,
        )
    )
    integration = result.scalars().first()
    if integration:
        return integration
    integration = Integration(
        user_id=user_id,
        provider=IMPORT_ALL_PROVIDER,
        status="system",
        config={},
    )
    db.add(integration)
    await db.flush()
    return integration


async def load_import_queue_preferences(db: AsyncSession, user_id: str) -> list[str]:
    result = await db.execute(
        select(Integration.config).where(
            Integration.user_id == user_id,
            Integration.provider == IMPORT_ALL_PROVIDER,
        )
    )
    config = result.scalar_one_or_none()
    return get_import_queue_order(config)


async def load_import_ready_providers(db: AsyncSession, user_id: str) -> list[str]:
    queue: list[str] = []
    for provider in IMPORT_ALL_PRIORITY:
        integration, secret_data = await load_integration_with_secrets(
            db, user_id, provider
        )
        if not integration or not secret_data:
            continue
        if provider == "letterboxd":
            if not has_required_letterboxd_fields(secret_data):
                continue
        elif provider == "trakt":
            if not settings.trakt_client_id or not settings.trakt_client_secret:
                continue
            if not has_required_trakt_fields(secret_data):
                continue
        elif provider == "simkl":
            if not settings.simkl_client_id or not settings.simkl_client_secret:
                continue
            if not has_required_simkl_fields(secret_data):
                continue
        elif provider == "stremio":
            if not has_required_stremio_fields(secret_data):
                continue
        elif provider == "anilist":
            if not settings.anilist_client_id or not settings.anilist_client_secret:
                continue
            if not has_required_anilist_fields(secret_data):
                continue
        elif provider == "aiostreams":
            if not has_required_aiostreams_fields(secret_data):
                continue
        else:
            continue
        queue.append(provider)
    return queue


async def build_import_all_queue(db: AsyncSession, user_id: str) -> list[str]:
    available = await load_import_ready_providers(db, user_id)
    preferred = await load_import_queue_preferences(db, user_id)
    if not preferred:
        preferred = list(DEFAULT_IMPORT_QUEUE_ORDER)
    return apply_import_queue_order(available, preferred)


async def load_active_import_all_users(db: AsyncSession) -> set[str]:
    result = await db.execute(
        select(Integration.user_id, Integration.config).where(
            Integration.provider == IMPORT_ALL_PROVIDER
        )
    )
    active: set[str] = set()
    for user_id, config in result.all():
        if import_all_active(config):
            active.add(str(user_id))
    return active


def _normalize_queue(value: object) -> list[str]:
    if isinstance(value, list):
        return [str(entry) for entry in value if entry]
    return []


def normalize_import_queue_order(value: object) -> list[str]:
    if not isinstance(value, list):
        return []
    cleaned: list[str] = []
    for entry in value:
        if entry is None:
            continue
        normalized = str(entry).strip().lower()
        if not normalized or normalized in cleaned:
            continue
        if normalized not in IMPORT_ALL_PRIORITY:
            continue
        cleaned.append(normalized)
    return cleaned


def get_import_queue_order(config: dict | None) -> list[str]:
    config = config or {}
    return normalize_import_queue_order(config.get(IMPORT_QUEUE_ORDER_KEY))


def set_import_queue_order(config: dict | None, order: Iterable[str]) -> dict:
    updated = dict(config or {})
    normalized = normalize_import_queue_order(list(order))
    if normalized:
        updated[IMPORT_QUEUE_ORDER_KEY] = normalized
    else:
        updated.pop(IMPORT_QUEUE_ORDER_KEY, None)
    return updated


def apply_import_queue_order(
    available: Iterable[str],
    preferred: Iterable[str],
) -> list[str]:
    available_list = [str(entry).strip().lower() for entry in available if entry]
    available_set = set(available_list)
    ordered: list[str] = []
    for entry in preferred:
        normalized = str(entry).strip().lower()
        if normalized and normalized in available_set and normalized not in ordered:
            ordered.append(normalized)
    for provider in available_list:
        if provider not in ordered:
            ordered.append(provider)
    return ordered


def _coerce_int(value: Any) -> int | None:
    if isinstance(value, int):
        return value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    if isinstance(value, str):
        cleaned = value.strip()
        if cleaned.isdigit():
            try:
                return int(cleaned)
            except ValueError:
                return None
    return None


def _coerce_str(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    cleaned = value.strip()
    return cleaned or None
