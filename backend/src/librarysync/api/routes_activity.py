from datetime import datetime, timezone

from fastapi import APIRouter, Depends, Query
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from librarysync.api.deps import get_current_user, get_db
from librarysync.core.import_all import IMPORT_ALL_PROVIDER, parse_import_all_state
from librarysync.core.import_history import parse_import_history
from librarysync.core.import_control import (
    MERGE_COMPLETED_AT_KEY,
    MERGE_ERROR_KEY,
    MERGE_REQUIRED_AT_KEY,
    next_quick_import_at,
    parse_quick_import_state,
)
from librarysync.core.import_schedule import parse_datetime
from librarysync.db.models import (
    EpisodeItem,
    Integration,
    MediaItem,
    MetadataLookupRequest,
    OutboxJob,
    ScheduledJob,
    SyncAttempt,
    User,
    WatchedItem,
    WatchEvent,
)

router = APIRouter(
    prefix="/api",
    tags=["activity"],
    dependencies=[Depends(get_current_user)],
)


def _build_item_payload(
    media: MediaItem | None,
    episode: EpisodeItem | None,
    show: MediaItem | None,
) -> dict | None:
    if not media and not episode and not show:
        return None
    if episode:
        show_item = show or media
        return {
            "title": show_item.title if show_item else None,
            "year": show_item.year if show_item else None,
            "media_type": "tv",
            "season_number": episode.season_number,
            "episode_number": episode.episode_number,
            "episode_title": episode.title,
        }
    if media:
        return {
            "title": media.title,
            "year": media.year,
            "media_type": media.media_type,
            "season_number": None,
            "episode_number": None,
            "episode_title": None,
        }
    return None


def _coerce_str(value: object) -> str | None:
    if value is None:
        return None
    if isinstance(value, str):
        cleaned = value.strip()
        return cleaned if cleaned else None
    return str(value)


def _coerce_int(value: object) -> int | None:
    if value is None:
        return None
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


def _build_item_from_payload(payload: dict) -> dict | None:
    title = _coerce_str(payload.get("title"))
    year = _coerce_int(payload.get("year"))
    media_type = _coerce_str(payload.get("media_type"))
    season_number = _coerce_int(payload.get("season_number"))
    episode_number = _coerce_int(payload.get("episode_number"))
    episode_title = _coerce_str(payload.get("episode_title"))
    if not any([title, year, media_type, season_number, episode_number, episode_title]):
        return None
    return {
        "title": title,
        "year": year,
        "media_type": media_type,
        "season_number": season_number,
        "episode_number": episode_number,
        "episode_title": episode_title,
    }


def _event_source(event_type: str) -> str | None:
    if event_type.endswith("_imported"):
        return event_type.split("_")[0]
    if event_type.startswith("manual_"):
        return "manual"
    return None


async def _load_watched_map(
    db: AsyncSession, user_id: str, watched_ids: list[str]
) -> dict[str, dict]:
    if not watched_ids:
        return {}
    show_item = aliased(MediaItem)
    result = await db.execute(
        select(WatchedItem, MediaItem, EpisodeItem, show_item)
        .outerjoin(MediaItem, WatchedItem.media_item_id == MediaItem.id)
        .outerjoin(EpisodeItem, WatchedItem.episode_item_id == EpisodeItem.id)
        .outerjoin(show_item, EpisodeItem.show_media_item_id == show_item.id)
        .where(WatchedItem.user_id == user_id, WatchedItem.id.in_(watched_ids))
    )
    mapping: dict[str, dict] = {}
    for watched, media, episode, show in result.all():
        mapping[watched.id] = {
            "source": watched.source,
            "item": _build_item_payload(media, episode, show),
        }
    return mapping


@router.get(
    "/activity/events",
    summary="List recent watch events",
    description="Return recent manual and imported watch events.",
)
async def events(
    limit: int = Query(50, ge=1, le=200),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    system_result = await db.execute(
        select(Integration).where(
            Integration.user_id == current_user.id,
            Integration.provider == IMPORT_ALL_PROVIDER,
        )
    )
    system_integration = system_result.scalars().first()
    import_history = parse_import_history(
        system_integration.config if system_integration else None
    )
    watch_limit = min(limit + len(import_history), 200)

    show_item = aliased(MediaItem)
    result = await db.execute(
        select(WatchEvent, MediaItem, EpisodeItem, show_item)
        .outerjoin(MediaItem, WatchEvent.media_item_id == MediaItem.id)
        .outerjoin(EpisodeItem, WatchEvent.episode_item_id == EpisodeItem.id)
        .outerjoin(show_item, EpisodeItem.show_media_item_id == show_item.id)
        .where(WatchEvent.user_id == current_user.id)
        .order_by(WatchEvent.occurred_at.desc())
        .limit(watch_limit)
    )
    events_out: list[dict] = []
    for event, media, episode, show in result.all():
        events_out.append(
            {
                "id": event.id,
                "event_type": event.event_type,
                "source_provider": _event_source(event.event_type),
                "occurred_at": event.occurred_at,
                "created_at": event.created_at,
                "item": _build_item_payload(media, episode, show),
                "raw": event.raw,
            }
        )

    now = datetime.now(timezone.utc)
    for entry in import_history:
        occurred_at = (
            entry.get("completed_at")
            or entry.get("started_at")
            or entry.get("requested_at")
            or now
        )
        merge_payload = {
            "required_at": entry.get("merge_required_at"),
            "completed_at": entry.get("merge_completed_at"),
            "error": entry.get("merge_error"),
        }
        events_out.append(
            {
                "id": f"import:{entry['id']}",
                "event_type": entry.get("event_type"),
                "event_category": "import",
                "source_provider": None,
                "occurred_at": occurred_at,
                "created_at": occurred_at,
                "item": None,
                "raw": {
                    "status": entry.get("status"),
                    "requested_at": entry.get("requested_at"),
                    "started_at": entry.get("started_at"),
                    "completed_at": entry.get("completed_at"),
                    "error": entry.get("error"),
                    "queue": entry.get("queue"),
                    "merge_required_at": entry.get("merge_required_at"),
                    "merge_completed_at": entry.get("merge_completed_at"),
                    "merge_error": entry.get("merge_error"),
                },
                "import_status": entry.get("status"),
                "import_error": entry.get("error"),
                "import_queue": entry.get("queue"),
                "import_merge": merge_payload,
            }
        )

    events_out.sort(
        key=lambda event: event.get("occurred_at") or event.get("created_at") or now,
        reverse=True,
    )
    events_out = events_out[:limit]
    return {"events": events_out}


@router.get(
    "/activity/sessions",
    summary="List active sessions",
    description="Return active playback sessions if available.",
)
async def sessions() -> dict:
    return {"sessions": []}


@router.get(
    "/outbox",
    summary="List outbox jobs",
    description="Return outbox delivery jobs for the current user.",
)
async def outbox(
    status: str | None = Query(None, description="Filter by job status"),
    limit: int = Query(50, ge=1, le=200),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    query = select(OutboxJob).where(OutboxJob.user_id == current_user.id)
    if status:
        query = query.where(OutboxJob.status == status)
    query = query.order_by(OutboxJob.created_at.desc()).limit(limit)
    result = await db.execute(query)
    jobs = result.scalars().all()

    attempt_map: dict[str, list[dict]] = {}
    job_ids = [job.id for job in jobs]
    if job_ids:
        attempts_result = await db.execute(
            select(SyncAttempt)
            .where(SyncAttempt.job_id.in_(job_ids))
            .order_by(SyncAttempt.attempted_at.desc())
        )
        for attempt in attempts_result.scalars().all():
            bucket = attempt_map.setdefault(attempt.job_id, [])
            if len(bucket) >= 5:
                continue
            bucket.append(
                {
                    "status": attempt.status,
                    "attempted_at": attempt.attempted_at,
                    "response_code": attempt.response_code,
                    "error": attempt.error,
                }
            )

    watched_ids = []
    for job in jobs:
        payload = job.payload if isinstance(job.payload, dict) else {}
        watched_id = _coerce_str(payload.get("watched_item_id"))
        if watched_id:
            watched_ids.append(watched_id)
    watched_map = await _load_watched_map(db, current_user.id, watched_ids)

    items: list[dict] = []
    for job in jobs:
        payload = job.payload if isinstance(job.payload, dict) else {}
        watched_id = _coerce_str(payload.get("watched_item_id"))
        watched_entry = watched_map.get(watched_id) if watched_id else None
        item = watched_entry["item"] if watched_entry else None
        if not item:
            item = _build_item_from_payload(payload)
        source = watched_entry["source"] if watched_entry else None
        if not source:
            source = _coerce_str(payload.get("source"))
        items.append(
            {
                "id": job.id,
                "target_provider": job.target_provider,
                "job_type": job.job_type,
                "status": job.status,
                "run_after": job.run_after,
                "attempts": job.attempts,
                "last_error": job.last_error,
                "created_at": job.created_at,
                "updated_at": job.updated_at,
                "source_provider": source,
                "watched_item_id": watched_id,
                "item": item,
                "payload": payload,
                "sync_attempts": attempt_map.get(job.id, []),
            }
        )
    return {"jobs": items}


@router.get(
    "/status",
    summary="Get sync status",
    description="Return schedule and queue status for the current user.",
)
async def status(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    now = datetime.now(timezone.utc)

    system_result = await db.execute(
        select(Integration).where(
            Integration.user_id == current_user.id,
            Integration.provider == IMPORT_ALL_PROVIDER,
        )
    )
    system_integration = system_result.scalars().first()
    system_config = system_integration.config if system_integration else {}
    quick_state = parse_quick_import_state(system_config)
    import_all_state = parse_import_all_state(system_config)
    merge_required_at = parse_datetime(system_config.get(MERGE_REQUIRED_AT_KEY))
    merge_completed_at = parse_datetime(system_config.get(MERGE_COMPLETED_AT_KEY))
    merge_error = system_config.get(MERGE_ERROR_KEY)

    scheduled_result = await db.execute(select(ScheduledJob))
    scheduled_jobs = [
        {
            "name": job.name,
            "next_run_at": job.next_run_at,
            "last_run_at": job.last_run_at,
            "lease_until": job.lease_until,
            "lease_owner": job.lease_owner,
        }
        for job in scheduled_result.scalars().all()
        if job.name != "merge_history"
    ]

    outbox_counts: dict[str, int] = {}
    outbox_result = await db.execute(
        select(OutboxJob.status, func.count())
        .where(OutboxJob.user_id == current_user.id)
        .group_by(OutboxJob.status)
    )
    for status, count in outbox_result.all():
        outbox_counts[str(status)] = int(count)

    pending_ready = await db.execute(
        select(func.count())
        .where(
            OutboxJob.user_id == current_user.id,
            OutboxJob.status.in_(("pending", "failed_retryable")),
            OutboxJob.run_after.is_(None),
        )
    )
    next_outbox_run: datetime | None = None
    if (pending_ready.scalar() or 0) > 0:
        next_outbox_run = now
    else:
        next_outbox_result = await db.execute(
            select(func.min(OutboxJob.run_after)).where(
                OutboxJob.user_id == current_user.id,
                OutboxJob.status.in_(("pending", "failed_retryable")),
                OutboxJob.run_after.is_not(None),
            )
        )
        next_outbox_run = next_outbox_result.scalar()

    metadata_counts: dict[str, int] = {}
    metadata_result = await db.execute(
        select(MetadataLookupRequest.status, func.count())
        .where(MetadataLookupRequest.user_id == current_user.id)
        .group_by(MetadataLookupRequest.status)
    )
    for status, count in metadata_result.all():
        metadata_counts[str(status)] = int(count)

    return {
        "server_time": now,
        "imports": {
            "quick": {
                "status": quick_state.status,
                "interval_seconds": quick_state.interval_seconds,
                "next_run_at": next_quick_import_at(system_config, now),
                "last_run_at": quick_state.last_run_at,
                "requested_at": quick_state.requested_at,
                "started_at": quick_state.started_at,
                "completed_at": quick_state.completed_at,
                "error": quick_state.error,
                "queue": quick_state.queue,
                "index": quick_state.index,
            },
            "import_all": {
                "status": import_all_state.status,
                "requested_at": import_all_state.requested_at,
                "started_at": import_all_state.started_at,
                "completed_at": import_all_state.completed_at,
                "error": import_all_state.error,
                "queue": import_all_state.queue,
                "index": import_all_state.index,
            },
            "merge": {
                "required_at": merge_required_at,
                "completed_at": merge_completed_at,
                "error": merge_error,
            },
        },
        "scheduled_jobs": scheduled_jobs,
        "outbox": {
            "counts": outbox_counts,
            "next_run_at": next_outbox_run,
        },
        "metadata": {
            "counts": metadata_counts,
        },
    }
