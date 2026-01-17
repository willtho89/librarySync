from datetime import datetime, timedelta, timezone
from typing import Any

from fastapi import APIRouter, Depends, Query
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from sqlalchemy import delete, func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from librarysync.api.deps import get_admin_api_key, get_db
from librarysync.core.watch_pipeline import _merge_media_fields
from librarysync.db.models import (
    EpisodeItem,
    MediaItem,
    OutboxJob,
    ScheduledJob,
    User,
    WatchedItem,
    WatchEvent,
    WatchlistItem,
    WatchlistSourceItem,
    WatchSync,
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


class MediaIdUpdate(BaseModel):
    id: str = Field(..., description="Media item ID to update")
    imdb: str | None = Field(None, description="IMDb ID to set (or null to clear)")
    tmdb: str | None = Field(None, description="TMDB ID to set (or null to clear)")
    tvdb: str | None = Field(None, description="TVDB ID to set (or null to clear)")
    tvmaze: str | None = Field(None, description="TVMaze ID to set (or null to clear)")
    kitsu: str | None = Field(None, description="Kitsu ID to set (or null to clear)")
    myanimelist: str | None = Field(None, description="MyAnimeList ID to set (or null to clear)")
    anilist: str | None = Field(None, description="AniList ID to set (or null to clear)")


class MediaUpdateRequest(BaseModel):
    updates: list[MediaIdUpdate] = Field(
        max_length=100, description="List of media items to update"
    )
    dry_run: bool = Field(False, description="If true, preview changes without committing")


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


@router.post(
    "/media/update-external-ids",
    summary="Update external IDs for media items",
    description=(
        "Update external IDs for one or more media items. If updating an ID causes "
        "a conflict (another media item already has that ID), the items will be merged: "
        "all dependent objects will be migrated to the target item and the duplicate "
        "will be deleted. Use dry_run=true to preview changes."
    ),
)
async def update_media_external_ids(
    request: MediaUpdateRequest,
    db: AsyncSession = Depends(get_db),
    _: str = Depends(get_admin_api_key),
) -> JSONResponse:
    # Load all target media items upfront
    ids = [u.id for u in request.updates]
    result = await db.execute(select(MediaItem).where(MediaItem.id.in_(ids)))
    target_items = result.scalars().all()
    target_map = {item.id: item for item in target_items}

    results = []
    total_updated = 0
    total_merged = 0
    total_unchanged = 0
    total_errors = 0

    for update_item in request.updates:
        target_media = target_map.get(update_item.id)
        if not target_media:
            results.append(
                {
                    "id": update_item.id,
                    "status": "error",
                    "message": "Media item not found",
                }
            )
            total_errors += 1
            continue
        try:
            result = await _process_media_id_update(
                db, update_item, target_media, dry_run=request.dry_run
            )
            results.append(result)

            if result["status"] == "updated":
                total_updated += 1
            elif result["status"] == "merged":
                total_merged += 1
            elif result["status"] == "unchanged":
                total_unchanged += 1
            elif result["status"] == "error":
                total_errors += 1

        except Exception as e:
            await db.rollback()
            results.append(
                {
                    "id": update_item.id,
                    "status": "error",
                    "message": str(e)[:500],
                }
            )
            total_errors += 1

    return JSONResponse(
        {
            "results": results,
            "total_updated": total_updated,
            "total_merged": total_merged,
            "total_unchanged": total_unchanged,
            "total_errors": total_errors,
        }
    )


async def _process_media_id_update(
    db: AsyncSession, update_item: MediaIdUpdate, target_media: MediaItem, dry_run: bool
) -> dict[str, Any]:
    if not any(
        [
            update_item.imdb is not None,
            update_item.tmdb is not None,
            update_item.tvdb is not None,
            update_item.tvmaze is not None,
            update_item.kitsu is not None,
            update_item.myanimelist is not None,
            update_item.anilist is not None,
        ]
    ):
        return {
            "id": update_item.id,
            "status": "unchanged",
            "message": "No changes requested",
        }

    changes_made = False
    merge_info: dict[str, Any] | None = None

    updates = {
        "imdb": update_item.imdb,
        "tmdb": update_item.tmdb,
        "tvdb": update_item.tvdb,
        "tvmaze": update_item.tvmaze,
        "kitsu": update_item.kitsu,
        "myanimelist": update_item.myanimelist,
        "anilist": update_item.anilist,
    }

    for field, value in updates.items():
        if value is None:
            continue

        if field == "imdb":
            conflict = await _find_conflict_by_imdb(db, value, target_media.id)
        else:
            conflict = await _find_conflict_by_provider(
                db, field, value, target_media.media_type, target_media.id
            )

        if conflict:
            if conflict.id == target_media.id:
                current_value = getattr(target_media, f"{field}_id")
                if current_value == value.lower() if field == "imdb" else value:
                    continue
                setattr(target_media, f"{field}_id", value.lower() if field == "imdb" else value)
                changes_made = True
            else:
                merge_info = await _merge_media_items(db, target_media, conflict, dry_run)
                target_media = conflict if merge_info["kept_id"] == conflict.id else target_media
                if not dry_run:
                    await db.commit()
                return {
                    "id": update_item.id,
                    "status": "merged",
                    "message": f"Merged with {conflict.id} due to {field} conflict",
                    **merge_info,
                }
        else:
            current_value = getattr(target_media, f"{field}_id")
            normalized_value = value.lower() if field == "imdb" else value
            if current_value != normalized_value:
                setattr(target_media, f"{field}_id", normalized_value)
                changes_made = True

    if not changes_made:
        return {
            "id": update_item.id,
            "status": "unchanged",
            "message": "No changes made - all IDs already set to requested values",
        }

    if not dry_run:
        await db.commit()

    return {
        "id": update_item.id,
        "status": "updated",
        "message": "External IDs updated successfully",
    }


async def _find_conflict_by_imdb(
    db: AsyncSession, imdb_id: str, exclude_id: str
) -> MediaItem | None:
    result = await db.execute(
        select(MediaItem).where(
            MediaItem.imdb_id == imdb_id.lower(),
            MediaItem.id != exclude_id,
        )
    )
    return result.scalars().first()


async def _find_conflict_by_provider(
    db: AsyncSession, provider: str, provider_id: str, media_type: str, exclude_id: str
) -> MediaItem | None:
    result = await db.execute(
        select(MediaItem).where(
            getattr(MediaItem, f"{provider}_id") == provider_id,
            MediaItem.media_type == media_type,
            MediaItem.id != exclude_id,
        )
    )
    return result.scalars().first()


async def _merge_media_items(
    db: AsyncSession, target: MediaItem, duplicate: MediaItem, dry_run: bool
) -> dict[str, Any]:
    keep = target
    remove = duplicate
    migrated: dict[str, int] = {}

    if dry_run:
        watched_result = await db.execute(
            select(func.count())
            .select_from(WatchedItem)
            .where(WatchedItem.media_item_id == remove.id)
        )
        migrated["watched_count"] = int(watched_result.scalar() or 0)

        # Deduplicate episodes: count non-conflicting remove episodes
        keep_episode_keys_result = await db.execute(
            select(EpisodeItem.season_number, EpisodeItem.episode_number).where(
                EpisodeItem.show_media_item_id == keep.id
            )
        )
        keep_episode_keys = {
            (row.season_number, row.episode_number) for row in keep_episode_keys_result.all()
        }

        remove_episode_keys_result = await db.execute(
            select(EpisodeItem.season_number, EpisodeItem.episode_number).where(
                EpisodeItem.show_media_item_id == remove.id
            )
        )
        remove_episode_keys = [
            (row.season_number, row.episode_number) for row in remove_episode_keys_result.all()
        ]
        migrated["episode_count"] = len(
            [k for k in remove_episode_keys if k not in keep_episode_keys]
        )

        # Deduplicate watchlist: count non-conflicting remove watchlist
        keep_watchlist_users_result = await db.execute(
            select(WatchlistItem.user_id).where(WatchlistItem.media_item_id == keep.id)
        )
        keep_watchlist_users = {row.user_id for row in keep_watchlist_users_result.all()}

        remove_watchlist_users_result = await db.execute(
            select(WatchlistItem.user_id).where(WatchlistItem.media_item_id == remove.id)
        )
        remove_watchlist_users = [row.user_id for row in remove_watchlist_users_result.all()]
        migrated["watchlist_count"] = len(
            [u for u in remove_watchlist_users if u not in keep_watchlist_users]
        )

        watch_event_result = await db.execute(
            select(func.count())
            .select_from(WatchEvent)
            .where(WatchEvent.media_item_id == remove.id)
        )
        migrated["watch_event_count"] = int(watch_event_result.scalar() or 0)

        watchlist_source_result = await db.execute(
            select(func.count())
            .select_from(WatchlistSourceItem)
            .where(WatchlistSourceItem.media_item_id == remove.id)
        )
        migrated["watchlist_source_count"] = int(watchlist_source_result.scalar() or 0)

        return {
            "kept_id": keep.id,
            "merged_from": remove.id,
            "migrated": migrated,
        }

    watched_result = await db.execute(
        select(WatchedItem).where(WatchedItem.media_item_id == remove.id)
    )
    watched_items = watched_result.scalars().all()

    for watched in watched_items:
        watched.media_item_id = keep.id

    migrated["watched_count"] = len(watched_items)

    # Deduplicate episodes: get existing episode keys for keep
    keep_episode_keys_result = await db.execute(
        select(EpisodeItem.season_number, EpisodeItem.episode_number).where(
            EpisodeItem.show_media_item_id == keep.id
        )
    )
    keep_episode_keys = {
        (row.season_number, row.episode_number) for row in keep_episode_keys_result.all()
    }

    episode_result = await db.execute(
        select(EpisodeItem).where(EpisodeItem.show_media_item_id == remove.id)
    )
    remove_episodes = episode_result.scalars().all()

    reassigned_episodes = []
    for episode in remove_episodes:
        if (episode.season_number, episode.episode_number) in keep_episode_keys:
            await db.delete(episode)
        else:
            episode.show_media_item_id = keep.id
            reassigned_episodes.append(episode)

    migrated["episode_count"] = len(reassigned_episodes)

    # Deduplicate watchlist items: get existing user_ids for keep
    keep_watchlist_users_result = await db.execute(
        select(WatchlistItem.user_id).where(WatchlistItem.media_item_id == keep.id)
    )
    keep_watchlist_users = {row.user_id for row in keep_watchlist_users_result.all()}

    watchlist_result = await db.execute(
        select(WatchlistItem).where(WatchlistItem.media_item_id == remove.id)
    )
    remove_watchlist_items = watchlist_result.scalars().all()

    reassigned_watchlist = []
    for wl_item in remove_watchlist_items:
        if wl_item.user_id in keep_watchlist_users:
            await db.delete(wl_item)
        else:
            wl_item.media_item_id = keep.id
            reassigned_watchlist.append(wl_item)

    migrated["watchlist_count"] = len(reassigned_watchlist)

    # Reassign WatchEvents
    watch_event_result = await db.execute(
        select(WatchEvent).where(WatchEvent.media_item_id == remove.id)
    )
    watch_events = watch_event_result.scalars().all()

    for event in watch_events:
        event.media_item_id = keep.id

    migrated["watch_event_count"] = len(watch_events)

    # Reassign WatchlistSourceItems
    watchlist_source_result = await db.execute(
        select(WatchlistSourceItem).where(WatchlistSourceItem.media_item_id == remove.id)
    )
    watchlist_source_items = watchlist_source_result.scalars().all()

    for source_item in watchlist_source_items:
        source_item.media_item_id = keep.id

    migrated["watchlist_source_count"] = len(watchlist_source_items)

    watched_ids = [w.id for w in watched_items]

    if watched_ids:
        sync_result = await db.execute(
            select(WatchSync).where(WatchSync.watched_item_id.in_(watched_ids))
        )
        syncs = sync_result.scalars().all()

        outbox_result = await db.execute(
            select(OutboxJob).where(
                or_(
                    OutboxJob.payload["watched_item_id"].as_string().in_(watched_ids),
                    OutboxJob.payload["watch_sync_id"].in_([s.id for s in syncs]),
                )
            )
        )
        jobs = outbox_result.scalars().all()

        for job in jobs:
            payload = dict(job.payload or {})
            watched_id = payload.get("watched_item_id")
            if watched_id in watched_ids:
                payload["watched_item_id"] = watched_id
            sync_id = payload.get("watch_sync_id")
            if sync_id and any(s.id == sync_id for s in syncs):
                payload["watch_sync_id"] = sync_id
            job.payload = payload

    _merge_media_fields(keep, remove)

    await db.delete(remove)

    return {
        "kept_id": keep.id,
        "merged_from": remove.id,
        "migrated": migrated,
    }
