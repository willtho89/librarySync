from datetime import datetime, timedelta, timezone
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Path, Query, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from sqlalchemy import delete, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from librarysync.api.deps import get_admin_api_key, get_db
from librarysync.connectors.metadata.base import MediaCandidate, MetadataProvider
from librarysync.core.metadata_enrichment import apply_refresh_candidate
from librarysync.core.metadata_providers import load_random_provider
from librarysync.core.watchlist import normalize_media_ids
from librarysync.db.models import (
    EpisodeItem,
    MediaItem,
    OutboxJob,
    ScheduledJob,
    StremioCustomCatalogItem,
    User,
    WatchedItem,
    WatchEvent,
    WatchlistItem,
    WatchlistSourceItem,
)
from librarysync.jobs.merge_history import merge_history_for_user
from librarysync.jobs.metadata_backfill import (
    METADATA_BACKFILL_FORCE_JOB,
    METADATA_BACKFILL_JOB,
)
from librarysync.jobs.metadata_cache import METADATA_CACHE_JOB
from librarysync.jobs.watchlist_refresh import WATCHLIST_REFRESH_JOB

router = APIRouter(prefix="/api/admin", tags=["admin"])

IMPORT_EVENT_PROVIDERS = {
    "aiostreams",
    "anilist",
    "letterboxd",
    "simkl",
    "stremio",
    "trakt",
}
MEDIA_EXTERNAL_ID_FIELDS = (
    "imdb_id",
    "tmdb_id",
    "tvdb_id",
    "tvmaze_id",
    "kitsu_id",
    "myanimelist_id",
    "anilist_id",
)
EPISODE_EXTERNAL_ID_FIELDS = ("imdb_id", "tmdb_id", "tvdb_id", "tvmaze_id")
MEDIA_EXTERNAL_ID_FIELDS_WITH_TYPE = {
    "tmdb_id",
    "tvdb_id",
    "tvmaze_id",
    "kitsu_id",
    "myanimelist_id",
    "anilist_id",
}
WATCHLIST_STATUS_RANK = {
    "removed": 0,
    "hidden": 1,
    "added": 2,
    "not_released": 2,
    "active": 3,
    "in_progress": 3,
    "waiting": 4,
    "watched": 5,
}
ACTIVE_OUTBOX_STATUSES = {"pending", "failed_retryable", "in_progress"}
PROVIDER_WATCHLIST_JOB_TYPES = {"push_watchlist", "remove_watchlist"}


class MediaItemExternalIdsUpdateIn(BaseModel):
    imdb_id: str | None = None
    tmdb_id: str | None = None
    tvdb_id: str | None = None
    tvmaze_id: str | None = None
    kitsu_id: str | None = None
    myanimelist_id: str | None = None
    anilist_id: str | None = None


def _admin_refresh_scope(media_item: MediaItem) -> str | None:
    if media_item.media_type == "movie":
        return "movie"
    if media_item.media_type in {"tv", "anime"}:
        return media_item.media_type
    return None


def _select_refresh_candidate(
    candidates: list[MediaCandidate],
    scope: str,
) -> MediaCandidate | None:
    valid = [candidate for candidate in candidates if candidate.provider_id]
    if not valid:
        return None
    for candidate in valid:
        if candidate.media_type == scope:
            return candidate
    return valid[0]


def _external_ids_for_refresh(provider: MetadataProvider, media_item: MediaItem) -> list[str]:
    if media_item.imdb_id:
        return [media_item.imdb_id.lower()]
    if provider.provider == "tvdb" and media_item.tmdb_id:
        return [media_item.tmdb_id]
    return []


async def _fetch_refresh_candidate(
    provider: MetadataProvider,
    media_item: MediaItem,
) -> MediaCandidate | None:
    scope = _admin_refresh_scope(media_item)
    if scope is None or not provider.supports_scope(scope):
        return None

    provider_field = {
        "imdb": "imdb_id",
        "tmdb": "tmdb_id",
        "tvdb": "tvdb_id",
        "tvmaze": "tvmaze_id",
        "kitsu": "kitsu_id",
        "myanimelist": "myanimelist_id",
        "anilist": "anilist_id",
    }.get(provider.provider)
    provider_id = getattr(media_item, provider_field, None) if provider_field else None
    if provider_id and provider.capabilities.supports_details:
        return await provider.get_details(provider_id, scope)

    if not provider.capabilities.supports_external_id:
        return None
    for external_id in _external_ids_for_refresh(provider, media_item):
        candidates = await provider.find_by_external_id(external_id, scope)
        candidate = _select_refresh_candidate(candidates, scope)
        if candidate:
            return candidate
    return None


def _merge_media_item_fields(target: MediaItem, source: MediaItem) -> None:
    for field in MEDIA_EXTERNAL_ID_FIELDS:
        if not getattr(target, field) and getattr(source, field):
            setattr(target, field, getattr(source, field))
    if source.year is not None and target.year is None:
        target.year = source.year
    if source.poster_url and not target.poster_url:
        target.poster_url = source.poster_url
    if source.release_date and not target.release_date:
        target.release_date = source.release_date
    if source.first_air_date and not target.first_air_date:
        target.first_air_date = source.first_air_date
    if source.last_air_date and not target.last_air_date:
        target.last_air_date = source.last_air_date
    if source.runtime_in_seconds is not None and target.runtime_in_seconds is None:
        target.runtime_in_seconds = source.runtime_in_seconds
    if source.genres and not target.genres:
        target.genres = list(source.genres)
    if source.overview and not target.overview:
        target.overview = source.overview
    if source.title and (not target.title or len(source.title) > len(target.title)):
        target.title = source.title
    if isinstance(target.raw, dict) and isinstance(source.raw, dict):
        for key, value in source.raw.items():
            target.raw.setdefault(key, value)
    elif target.raw is None and isinstance(source.raw, dict):
        target.raw = dict(source.raw)


def _merge_episode_item_fields(target: EpisodeItem, source: EpisodeItem) -> None:
    for field in EPISODE_EXTERNAL_ID_FIELDS:
        if not getattr(target, field) and getattr(source, field):
            setattr(target, field, getattr(source, field))
    if source.title and not target.title:
        target.title = source.title
    if source.air_date and not target.air_date:
        target.air_date = source.air_date
    if isinstance(target.raw, dict) and isinstance(source.raw, dict):
        for key, value in source.raw.items():
            target.raw.setdefault(key, value)
    elif target.raw is None and isinstance(source.raw, dict):
        target.raw = dict(source.raw)


def _merge_watchlist_item_fields(target: WatchlistItem, source: WatchlistItem) -> None:
    target_rank = WATCHLIST_STATUS_RANK.get(target.status or "", -1)
    source_rank = WATCHLIST_STATUS_RANK.get(source.status or "", -1)
    if source_rank > target_rank:
        target.status = source.status
    if target.source == "manual" and source.source:
        target.source = source.source
    if source.created_at and (not target.created_at or source.created_at < target.created_at):
        target.created_at = source.created_at
    if source.updated_at and (not target.updated_at or source.updated_at > target.updated_at):
        target.updated_at = source.updated_at


def _merge_watchlist_source_item_fields(
    target: WatchlistSourceItem,
    source: WatchlistSourceItem,
    target_media_item_id: str,
) -> None:
    target.media_item_id = target_media_item_id
    if source.external_item_id and not target.external_item_id:
        target.external_item_id = source.external_item_id
    if source.added_at and (not target.added_at or source.added_at < target.added_at):
        target.added_at = source.added_at
    if source.last_seen_at and (
        not target.last_seen_at or source.last_seen_at > target.last_seen_at
    ):
        target.last_seen_at = source.last_seen_at


def _merge_catalog_item_fields(
    target: StremioCustomCatalogItem,
    source: StremioCustomCatalogItem,
) -> None:
    if source.position < target.position:
        target.position = source.position
    if source.created_at and source.created_at < target.created_at:
        target.created_at = source.created_at


def _register_episode_external_ids(
    index: dict[str, dict[str, EpisodeItem]],
    episode: EpisodeItem,
) -> None:
    for field in EPISODE_EXTERNAL_ID_FIELDS:
        value = getattr(episode, field, None)
        if value:
            index.setdefault(field, {})[value] = episode


def _find_target_episode_match(
    target_by_key: dict[tuple[int, int], EpisodeItem],
    target_by_external_id: dict[str, dict[str, EpisodeItem]],
    source_episode: EpisodeItem,
) -> EpisodeItem | None:
    episode_key = (source_episode.season_number, source_episode.episode_number)
    target_episode = target_by_key.get(episode_key)
    if target_episode:
        return target_episode

    matches: dict[str, EpisodeItem] = {}
    for field in EPISODE_EXTERNAL_ID_FIELDS:
        value = getattr(source_episode, field, None)
        if not value:
            continue
        matched = target_by_external_id.get(field, {}).get(value)
        if matched:
            matches[matched.id] = matched
    if len(matches) > 1:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Conflicting episode items found for the same external IDs",
        )
    return next(iter(matches.values()), None)


async def _load_media_item_for_update(
    db: AsyncSession,
    media_item_id: str,
) -> MediaItem:
    result = await db.execute(select(MediaItem).where(MediaItem.id == media_item_id))
    media_item = result.scalars().first()
    if not media_item:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Media item not found",
        )
    return media_item


async def _find_conflicting_media_item(
    db: AsyncSession,
    target: MediaItem,
    field: str,
    value: str,
) -> MediaItem | None:
    column = getattr(MediaItem, field)
    query = select(MediaItem).where(column == value, MediaItem.id != target.id)
    if field in MEDIA_EXTERNAL_ID_FIELDS_WITH_TYPE:
        query = query.where(MediaItem.media_type == target.media_type)
    result = await db.execute(query)
    return result.scalars().first()


async def _merge_episode_items(
    db: AsyncSession,
    target: MediaItem,
    source: MediaItem,
) -> None:
    result = await db.execute(
        select(EpisodeItem).where(EpisodeItem.show_media_item_id.in_([target.id, source.id]))
    )
    episodes = result.scalars().all()
    target_by_key = {
        (episode.season_number, episode.episode_number): episode
        for episode in episodes
        if episode.show_media_item_id == target.id
    }
    target_by_external_id: dict[str, dict[str, EpisodeItem]] = {}
    for target_episode in target_by_key.values():
        _register_episode_external_ids(target_by_external_id, target_episode)
    source_episodes = [episode for episode in episodes if episode.show_media_item_id == source.id]

    for source_episode in source_episodes:
        key = (source_episode.season_number, source_episode.episode_number)
        target_episode = _find_target_episode_match(
            target_by_key,
            target_by_external_id,
            source_episode,
        )
        if not target_episode:
            source_episode.show_media_item_id = target.id
            target_by_key[key] = source_episode
            _register_episode_external_ids(target_by_external_id, source_episode)
            continue
        _merge_episode_item_fields(target_episode, source_episode)
        _register_episode_external_ids(target_by_external_id, target_episode)
        await db.execute(
            update(WatchedItem)
            .where(WatchedItem.episode_item_id == source_episode.id)
            .values(episode_item_id=target_episode.id)
        )
        await db.execute(
            update(WatchEvent)
            .where(WatchEvent.episode_item_id == source_episode.id)
            .values(episode_item_id=target_episode.id)
        )
        await db.delete(source_episode)


async def _merge_watchlist_items(
    db: AsyncSession,
    target: MediaItem,
    source: MediaItem,
) -> dict[str, str]:
    result = await db.execute(
        select(WatchlistItem).where(WatchlistItem.media_item_id.in_([target.id, source.id]))
    )
    items = result.scalars().all()
    target_by_user = {
        item.user_id: item for item in items if item.media_item_id == target.id
    }
    source_items = [item for item in items if item.media_item_id == source.id]
    watchlist_item_id_map: dict[str, str] = {}

    for source_item in source_items:
        target_item = target_by_user.get(source_item.user_id)
        if not target_item:
            source_item.media_item_id = target.id
            target_by_user[source_item.user_id] = source_item
            watchlist_item_id_map[source_item.id] = source_item.id
            continue
        _merge_watchlist_item_fields(target_item, source_item)
        watchlist_item_id_map[source_item.id] = target_item.id
        source_rows_result = await db.execute(
            select(WatchlistSourceItem).where(
                WatchlistSourceItem.watchlist_item_id.in_([target_item.id, source_item.id])
            )
        )
        source_rows = source_rows_result.scalars().all()
        target_sources = {
            row.source_id: row for row in source_rows if row.watchlist_item_id == target_item.id
        }
        duplicate_sources = [
            row for row in source_rows if row.watchlist_item_id == source_item.id
        ]
        for source_row in duplicate_sources:
            existing = target_sources.get(source_row.source_id)
            if existing:
                _merge_watchlist_source_item_fields(existing, source_row, target.id)
                await db.delete(source_row)
                continue
            source_row.watchlist_item_id = target_item.id
            source_row.media_item_id = target.id
            target_sources[source_row.source_id] = source_row
        await db.delete(source_item)
    return watchlist_item_id_map


async def _merge_catalog_items(
    db: AsyncSession,
    target: MediaItem,
    source: MediaItem,
) -> None:
    result = await db.execute(
        select(StremioCustomCatalogItem).where(
            StremioCustomCatalogItem.media_item_id.in_([target.id, source.id])
        )
    )
    items = result.scalars().all()
    target_by_catalog = {
        item.catalog_id: item for item in items if item.media_item_id == target.id
    }
    source_items = [item for item in items if item.media_item_id == source.id]
    for source_item in source_items:
        target_item = target_by_catalog.get(source_item.catalog_id)
        if target_item:
            _merge_catalog_item_fields(target_item, source_item)
            await db.delete(source_item)
            continue
        source_item.media_item_id = target.id
        target_by_catalog[source_item.catalog_id] = source_item


async def _repoint_active_watchlist_jobs(
    db: AsyncSession,
    target_media_item_id: str,
    source_media_item_id: str,
) -> None:
    result = await db.execute(
        select(OutboxJob).where(
            OutboxJob.job_type == "watchlist_update",
            OutboxJob.status.in_(ACTIVE_OUTBOX_STATUSES),
        )
    )
    jobs = result.scalars().all()
    jobs_by_target_key = {
        job.dedupe_key: job
        for job in jobs
        if isinstance(job.payload, dict)
        and job.payload.get("media_item_id") == target_media_item_id
        and job.dedupe_key
    }
    for job in jobs:
        if not isinstance(job.payload, dict):
            continue
        if job.payload.get("media_item_id") != source_media_item_id:
            continue
        target_key = f"{job.user_id}:internal:watchlist_update:{target_media_item_id}"
        existing = jobs_by_target_key.get(target_key)
        if existing and existing.id != job.id:
            await db.delete(job)
            continue
        payload = dict(job.payload)
        payload["media_item_id"] = target_media_item_id
        job.payload = payload
        job.dedupe_key = target_key
        jobs_by_target_key[target_key] = job


async def _repoint_active_provider_watchlist_jobs(
    db: AsyncSession,
    watchlist_item_id_map: dict[str, str],
    target_media_item_id: str,
) -> None:
    if not watchlist_item_id_map:
        return

    result = await db.execute(
        select(OutboxJob).where(
            OutboxJob.job_type.in_(PROVIDER_WATCHLIST_JOB_TYPES),
            OutboxJob.status.in_(ACTIVE_OUTBOX_STATUSES),
        )
    )
    jobs = result.scalars().all()
    target_item_ids = set(watchlist_item_id_map.values())
    jobs_by_target_key = {
        f"{job.user_id}:{job.target_provider}:{job.job_type}:{payload_watchlist_item_id}": job
        for job in jobs
        if isinstance(job.payload, dict)
        and (payload_watchlist_item_id := job.payload.get("watchlist_item_id")) in target_item_ids
    }

    for job in jobs:
        if not isinstance(job.payload, dict):
            continue
        payload_watchlist_item_id = job.payload.get("watchlist_item_id")
        if payload_watchlist_item_id not in watchlist_item_id_map:
            continue
        target_watchlist_item_id = watchlist_item_id_map[payload_watchlist_item_id]
        target_key = (
            f"{job.user_id}:{job.target_provider}:{job.job_type}:{target_watchlist_item_id}"
        )
        existing = jobs_by_target_key.get(target_key)
        if existing and existing.id != job.id:
            await db.delete(job)
            continue
        payload = dict(job.payload)
        payload["watchlist_item_id"] = target_watchlist_item_id
        payload["media_item_id"] = target_media_item_id
        job.payload = payload
        job.dedupe_key = target_key
        jobs_by_target_key[target_key] = job


async def _merge_media_items(
    db: AsyncSession,
    target: MediaItem,
    source: MediaItem,
) -> None:
    if source.id == target.id:
        return
    if source.media_type != target.media_type:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Conflicting media item has a different media type",
        )

    _merge_media_item_fields(target, source)
    await _merge_episode_items(db, target, source)
    watchlist_item_id_map = await _merge_watchlist_items(db, target, source)
    await _repoint_active_provider_watchlist_jobs(db, watchlist_item_id_map, target.id)
    await _merge_catalog_items(db, target, source)
    await _repoint_active_watchlist_jobs(db, target.id, source.id)

    await db.execute(
        update(WatchedItem)
        .where(WatchedItem.media_item_id == source.id)
        .values(media_item_id=target.id)
    )
    await db.execute(
        update(WatchEvent)
        .where(WatchEvent.media_item_id == source.id)
        .values(media_item_id=target.id)
    )
    await db.execute(
        update(WatchlistSourceItem)
        .where(WatchlistSourceItem.media_item_id == source.id)
        .values(media_item_id=target.id)
    )

    await db.delete(source)
    await db.flush()


async def _refresh_media_item_metadata(
    db: AsyncSession,
    media_item: MediaItem,
) -> dict[str, Any]:
    provider_order = ("imdb", "tmdb", "tvdb", "tvmaze", "kitsu", "myanimelist", "anilist")
    attempted: list[str] = []
    refreshed_from: list[str] = []
    errors: list[str] = []
    refreshed = False

    for provider_name in provider_order:
        provider = await load_random_provider(db, provider_name)
        if not provider:
            continue
        attempted.append(provider_name)
        try:
            candidate = await _fetch_refresh_candidate(provider, media_item)
        except Exception:
            errors.append(provider_name)
            continue
        if not candidate:
            continue
        await apply_refresh_candidate(db, media_item, candidate, overwrite=not refreshed)
        refreshed = True
        refreshed_from.append(provider_name)

    if refreshed:
        media_item.metadata_refreshed_at = datetime.now(timezone.utc)

    return {
        "refreshed": refreshed,
        "providers": refreshed_from,
        "attempted": attempted,
        "errors": errors,
    }


@router.post(
    "/reset-outbox-jobs",
    summary="Reset stuck outbox jobs",
    description=(
        "Reset outbox jobs that have been in 'in_progress' status for longer "
        "than the specified timeout."
    ),
)
async def reset_outbox_jobs(
    timeout_minutes: int = Query(10, description="Timeout in minutes for stuck jobs", ge=1),
    db: AsyncSession = Depends(get_db),
    _: str = Depends(get_admin_api_key),
) -> JSONResponse:
    cutoff_time = datetime.now(timezone.utc) - timedelta(minutes=timeout_minutes)

    # Query for stuck jobs
    query = select(OutboxJob).where(
        OutboxJob.status == "in_progress",
        OutboxJob.updated_at < cutoff_time,
    )
    result = await db.execute(query)
    stuck_jobs = result.scalars().all()

    if not stuck_jobs:
        return JSONResponse({"message": "No stuck jobs found", "reset_count": 0})

    # Reset the jobs
    job_ids = [job.id for job in stuck_jobs]
    await db.execute(
        update(OutboxJob)
        .where(OutboxJob.id.in_(job_ids))
        .values(
            status="pending",
            run_after=datetime.now(timezone.utc),
            attempts=0,
            last_error=None,
        )
    )
    await db.commit()

    return JSONResponse(
        {
            "message": f"Reset {len(job_ids)} stuck jobs",
            "reset_count": len(job_ids),
            "job_ids": job_ids,
        }
    )


@router.delete(
    "/purge-jobs",
    summary="Purge outbox jobs",
    description=(
        "Delete outbox jobs matching the specified criteria. "
        "Use with caution - this is irreversible."
    ),
)
async def purge_jobs(
    status: str | None = Query(None, description="Filter by job status"),
    target_provider: str | None = Query(None, description="Filter by target provider"),
    older_than_hours: int | None = Query(
        None, description="Only delete jobs older than this many hours", ge=1
    ),
    limit: int = Query(1000, description="Maximum number of jobs to delete", ge=1, le=10000),
    dry_run: bool = Query(False, description="If true, return count without deleting"),
    db: AsyncSession = Depends(get_db),
    _: str = Depends(get_admin_api_key),
) -> JSONResponse:
    # Build the base query
    query = select(OutboxJob)

    # Apply filters
    conditions = []
    if status:
        conditions.append(OutboxJob.status == status)
    if target_provider:
        conditions.append(OutboxJob.target_provider == target_provider)
    if older_than_hours:
        cutoff_time = datetime.now(timezone.utc) - timedelta(hours=older_than_hours)
        conditions.append(OutboxJob.created_at < cutoff_time)

    if conditions:
        query = query.where(*conditions)

    # Apply limit
    query = query.limit(limit)

    # Get matching jobs
    result = await db.execute(query)
    matching_jobs = result.scalars().all()
    job_ids = [job.id for job in matching_jobs]

    if not job_ids:
        return JSONResponse(
            {
                "message": "No matching jobs found",
                "purge_count": 0,
                "job_ids": [],
            }
        )

    if dry_run:
        return JSONResponse(
            {
                "message": f"Dry run: would delete {len(job_ids)} jobs",
                "purge_count": 0,
                "job_ids": job_ids,
                "filters_applied": {
                    "status": status,
                    "target_provider": target_provider,
                    "older_than_hours": older_than_hours,
                    "limit": limit,
                },
            }
        )

    # Delete the jobs
    await db.execute(delete(OutboxJob).where(OutboxJob.id.in_(job_ids)))
    await db.commit()

    return JSONResponse(
        {
            "message": f"Successfully purged {len(job_ids)} jobs",
            "purge_count": len(job_ids),
            "job_ids": job_ids,
            "filters_applied": {
                "status": status,
                "target_provider": target_provider,
                "older_than_hours": older_than_hours,
                "limit": limit,
            },
        }
    )


@router.post(
    "/metadata-backfill",
    summary="Schedule metadata backfill",
    description="Schedule a metadata backfill run for the worker to execute.",
)
async def schedule_metadata_backfill(
    force: bool = Query(False, description="If true, force episode refresh regardless of delta"),
    db: AsyncSession = Depends(get_db),
    _: str = Depends(get_admin_api_key),
) -> JSONResponse:
    now = datetime.now(timezone.utc)
    job_name = METADATA_BACKFILL_FORCE_JOB if force else METADATA_BACKFILL_JOB
    result = await db.execute(select(ScheduledJob).where(ScheduledJob.name == job_name))
    job = result.scalars().first()
    if not job:
        job = ScheduledJob(name=job_name, next_run_at=now)
        db.add(job)
    else:
        job.next_run_at = now
        job.lease_until = None
        job.lease_owner = None
        job.updated_at = now
    await db.commit()
    return JSONResponse(
        {
            "message": "Metadata backfill scheduled",
            "force": force,
            "next_run_at": job.next_run_at.isoformat() if job.next_run_at else None,
        }
    )


@router.post(
    "/watchlist-refresh",
    summary="Schedule watchlist refresh",
    description="Schedule a watchlist refresh run for the worker to execute.",
)
async def schedule_watchlist_refresh(
    db: AsyncSession = Depends(get_db),
    _: str = Depends(get_admin_api_key),
) -> JSONResponse:
    now = datetime.now(timezone.utc)
    result = await db.execute(
        select(ScheduledJob).where(ScheduledJob.name == WATCHLIST_REFRESH_JOB)
    )
    job = result.scalars().first()
    if not job:
        job = ScheduledJob(name=WATCHLIST_REFRESH_JOB, next_run_at=now)
        db.add(job)
    else:
        job.next_run_at = now
        job.lease_until = None
        job.lease_owner = None
        job.updated_at = now
    await db.commit()
    return JSONResponse(
        {
            "message": "Watchlist refresh scheduled",
            "next_run_at": job.next_run_at.isoformat() if job.next_run_at else None,
        }
    )


@router.post(
    "/metadata-cache",
    summary="Schedule metadata cache refresh",
    description="Schedule the metadata cache refresh job to run immediately.",
)
async def schedule_metadata_cache(
    db: AsyncSession = Depends(get_db),
    _: str = Depends(get_admin_api_key),
) -> JSONResponse:
    now = datetime.now(timezone.utc)
    result = await db.execute(select(ScheduledJob).where(ScheduledJob.name == METADATA_CACHE_JOB))
    job = result.scalars().first()
    if not job:
        job = ScheduledJob(name=METADATA_CACHE_JOB, next_run_at=now)
        db.add(job)
    else:
        job.next_run_at = now
        job.lease_until = None
        job.lease_owner = None
        job.updated_at = now
    await db.commit()
    return JSONResponse(
        {
            "message": "Metadata cache refresh scheduled",
            "next_run_at": job.next_run_at.isoformat() if job.next_run_at else None,
        }
    )


@router.delete(
    "/import-history",
    summary="Reset import history",
    description="Delete import history events for a provider to allow re-importing.",
)
async def reset_import_history(
    provider: str = Query(..., description="Import provider (e.g., aiostreams)"),
    user_id: str | None = Query(
        None,
        description="Optional user id to scope the reset. Omit to reset all users.",
    ),
    include_blacklisted: bool = Query(
        True,
        description="Also delete provider_blacklisted events.",
    ),
    dry_run: bool = Query(False, description="If true, return count without deleting"),
    db: AsyncSession = Depends(get_db),
    _: str = Depends(get_admin_api_key),
) -> JSONResponse:
    normalized = provider.strip().lower()
    if normalized not in IMPORT_EVENT_PROVIDERS:
        return JSONResponse(
            {
                "message": "Unsupported provider",
                "deleted": 0,
                "provider": normalized,
            },
            status_code=400,
        )
    event_types = [f"{normalized}_imported"]
    if include_blacklisted:
        event_types.append(f"{normalized}_blacklisted")
    conditions = [WatchEvent.event_type.in_(event_types)]
    if user_id:
        conditions.append(WatchEvent.user_id == user_id)

    count_result = await db.execute(select(func.count()).select_from(WatchEvent).where(*conditions))
    count = int(count_result.scalar_one() or 0)
    if dry_run:
        return JSONResponse(
            {
                "message": "Dry run: would delete import history",
                "deleted": 0,
                "provider": normalized,
                "user_id": user_id,
                "event_types": event_types,
                "match_count": count,
            }
        )

    if count:
        await db.execute(delete(WatchEvent).where(*conditions))
        await db.commit()

    return JSONResponse(
        {
            "message": "Import history reset",
            "deleted": count,
            "provider": normalized,
            "user_id": user_id,
            "event_types": event_types,
        }
    )


@router.post(
    "/media-items/{media_item_id}/external-ids",
    summary="Update media item external IDs",
    description=(
        "Update external IDs for a media item. If another media item already owns one "
        "of the submitted IDs, its references are merged into the requested media item "
        "before the IDs are applied and metadata is refreshed."
    ),
)
async def update_media_item_external_ids(
    payload: MediaItemExternalIdsUpdateIn,
    media_item_id: str = Path(..., description="Media item ID to update"),
    db: AsyncSession = Depends(get_db),
    _: str = Depends(get_admin_api_key),
) -> JSONResponse:
    normalized_ids = normalize_media_ids(payload.model_dump(exclude_none=True))
    if not normalized_ids:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Provide at least one external ID",
        )

    merged_media_item_ids: list[str] = []
    try:
        media_item = await _load_media_item_for_update(db, media_item_id)

        for field, value in normalized_ids.items():
            conflict = await _find_conflicting_media_item(db, media_item, field, value)
            if conflict and conflict.id not in merged_media_item_ids:
                await _merge_media_items(db, media_item, conflict)
                merged_media_item_ids.append(conflict.id)
            setattr(media_item, field, value)

        await db.flush()
        refresh_result = await _refresh_media_item_metadata(db, media_item)
        await db.commit()
    except HTTPException:
        await db.rollback()
        raise
    except Exception:
        await db.rollback()
        raise

    return JSONResponse(
        {
            "message": "Media item external IDs updated",
            "media_item_id": media_item.id,
            "updated_ids": {field: getattr(media_item, field) for field in normalized_ids},
            "merged_media_item_ids": merged_media_item_ids,
            "metadata_refresh": refresh_result,
        }
    )


@router.post(
    "/merge-history",
    summary="Merge duplicate history entries",
    description="Merge duplicate watched history entries for users to fix pagination issues.",
)
async def merge_history(
    user_id: str | None = Query(
        None, description="User ID to merge for. Omit to merge for all users."
    ),
    db: AsyncSession = Depends(get_db),
    _: str = Depends(get_admin_api_key),
) -> JSONResponse:
    if user_id:
        users = [user_id]
    else:
        result = await db.execute(select(User.id))
        users = [row[0] for row in result.all()]

    total_merged = 0
    user_results = []
    for uid in users:
        try:
            merged = await merge_history_for_user(db, uid)
            total_merged += merged
            user_results.append({"user_id": uid, "merged_count": merged})
        except Exception as e:
            await db.rollback()
            user_results.append({"user_id": uid, "error": str(e)})

    return JSONResponse(
        {
            "message": "History merge completed",
            "total_merged": total_merged,
            "user_results": user_results,
        }
    )
