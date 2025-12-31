from datetime import datetime, timezone

from fastapi import APIRouter, Depends, Query
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from librarysync.api.deps import get_current_user, get_db
from librarysync.core.import_schedule import (
    DEFAULT_IMPORT_INTERVAL_SECONDS,
    IMPORT_INTERVAL_KEY,
    IMPORT_LAST_RUN_KEY,
    IMPORT_REQUESTED_KEY,
    compute_next_import_at,
    normalize_interval_seconds,
    parse_datetime,
)
from librarysync.db.models import (
    EpisodeItem,
    Integration,
    MediaItem,
    MetadataLookupRequest,
    OutboxJob,
    ScheduledJob,
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
    show_item = aliased(MediaItem)
    result = await db.execute(
        select(WatchEvent, MediaItem, EpisodeItem, show_item)
        .outerjoin(MediaItem, WatchEvent.media_item_id == MediaItem.id)
        .outerjoin(EpisodeItem, WatchEvent.episode_item_id == EpisodeItem.id)
        .outerjoin(show_item, EpisodeItem.show_media_item_id == show_item.id)
        .where(WatchEvent.user_id == current_user.id)
        .order_by(WatchEvent.occurred_at.desc())
        .limit(limit)
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

    integrations_result = await db.execute(
        select(Integration).where(Integration.user_id == current_user.id)
    )
    integrations: list[dict] = []
    for integration in integrations_result.scalars().all():
        config = integration.config or {}
        interval_seconds = normalize_interval_seconds(config.get(IMPORT_INTERVAL_KEY))
        last_run = parse_datetime(config.get(IMPORT_LAST_RUN_KEY))
        requested_at = parse_datetime(config.get(IMPORT_REQUESTED_KEY))
        next_import_at = integration.next_import_at
        if next_import_at is None and (
            interval_seconds is not None or last_run or requested_at
        ):
            next_import_at = compute_next_import_at(
                config, now, DEFAULT_IMPORT_INTERVAL_SECONDS
            )
        integrations.append(
            {
                "provider": integration.provider,
                "status": integration.status,
                "next_import_at": next_import_at,
                "last_import_at": last_run,
                "requested_at": requested_at,
                "interval_seconds": interval_seconds,
                "lease_until": integration.import_lease_until,
                "lease_owner": integration.import_lease_owner,
            }
        )

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
        "import_schedules": integrations,
        "scheduled_jobs": scheduled_jobs,
        "outbox": {
            "counts": outbox_counts,
            "next_run_at": next_outbox_run,
        },
        "metadata": {
            "counts": metadata_counts,
        },
    }
