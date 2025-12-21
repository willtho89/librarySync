from __future__ import annotations

from collections.abc import Awaitable, Callable
from datetime import datetime, timezone

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from librarysync.config import settings
from librarysync.connectors.services.letterboxd import has_required_letterboxd_fields
from librarysync.connectors.services.simkl import has_required_simkl_fields
from librarysync.connectors.services.trakt import has_required_trakt_fields
from librarysync.core.integrations import load_integration_with_secrets
from librarysync.core.metadata_enrichment import enrich_watched_metadata
from librarysync.db.models import (
    EpisodeItem,
    MediaItem,
    OutboxJob,
    WatchedItem,
    WatchSync,
)

SyncStrategy = Callable[
    [AsyncSession, WatchedItem, MediaItem | None, EpisodeItem | None, bool],
    Awaitable[None],
]

SUCCESS_STATUSES = {
    "succeeded",
    "assumed_tracked",
    "synced_from_trakt",
    "synced_from_letterboxd",
    "synced_from_simkl",
}


def is_synced_status(status: str | None) -> bool:
    if not status:
        return False
    if status in SUCCESS_STATUSES:
        return True
    return status.startswith("synced_from_")


async def enqueue_new_item_job(
    db: AsyncSession,
    user_id: str,
    watched_item_id: str,
    is_rewatch: bool | None = None,
    source: str | None = None,
) -> OutboxJob:
    payload: dict[str, object] = {"watched_item_id": watched_item_id}
    if is_rewatch is not None:
        payload["is_rewatch"] = bool(is_rewatch)
    if source:
        payload["source"] = source
    job = OutboxJob(
        user_id=user_id,
        target_provider="internal",
        job_type="new_item_added",
        payload=payload,
        status="pending",
    )
    db.add(job)
    return job


async def process_new_item_job(db: AsyncSession, job: OutboxJob) -> None:
    payload = job.payload or {}
    watched_id = payload.get("watched_item_id")
    if not watched_id:
        raise ValueError("new_item_added requires watched_item_id")
    watched = await db.get(WatchedItem, str(watched_id))
    if not watched:
        raise ValueError("watched item not found")
    media_item = None
    episode_item = None
    if watched.media_item_id:
        media_item = await db.get(MediaItem, watched.media_item_id)
    if watched.episode_item_id:
        episode_item = await db.get(EpisodeItem, watched.episode_item_id)
        if episode_item and not media_item:
            media_item = await db.get(MediaItem, episode_item.show_media_item_id)
    if not media_item and not episode_item:
        raise ValueError("watched item missing media references")
    is_rewatch = bool(payload.get("is_rewatch"))

    await enrich_watched_metadata(db, watched.user_id, media_item, episode_item)
    await _sync_to_integrations(db, watched, media_item, episode_item, is_rewatch)


async def _sync_to_integrations(
    db: AsyncSession,
    watched: WatchedItem,
    media_item: MediaItem | None,
    episode_item: EpisodeItem | None,
    is_rewatch: bool,
) -> None:
    for strategy in SYNC_STRATEGIES.values():
        await strategy(db, watched, media_item, episode_item, is_rewatch)


async def _sync_letterboxd(
    db: AsyncSession,
    watched: WatchedItem,
    media_item: MediaItem | None,
    episode_item: EpisodeItem | None,
    is_rewatch: bool,
) -> None:
    if episode_item:
        return
    if not media_item or media_item.media_type != "movie":
        return
    if not media_item.imdb_id and not media_item.tmdb_id:
        return
    integration, secret_data = await load_integration_with_secrets(
        db, watched.user_id, "letterboxd"
    )
    if not integration or not secret_data:
        return
    if not has_required_letterboxd_fields(secret_data):
        return
    watch_sync = await _get_watch_sync(db, watched.id, "letterboxd")
    if watch_sync and is_synced_status(watch_sync.status):
        return
    if watch_sync and watch_sync.status in {"pending", "in_progress"}:
        return
    if not watch_sync:
        watch_sync = WatchSync(
            user_id=watched.user_id,
            watched_item_id=watched.id,
            provider="letterboxd",
            status="pending",
            is_rewatch=is_rewatch,
        )
        db.add(watch_sync)
        await db.flush()
    else:
        watch_sync.status = "pending"
        watch_sync.last_error = None

    imdb_id = media_item.imdb_id.lower() if media_item.imdb_id else None
    tmdb_id = media_item.tmdb_id if media_item.tmdb_id else None
    job = OutboxJob(
        user_id=watched.user_id,
        target_provider="letterboxd",
        job_type="push_watched",
        payload={
            "watch_sync_id": watch_sync.id,
            "watched_item_id": watched.id,
            "media_item_id": media_item.id,
            "imdb_id": imdb_id,
            "tmdb_id": tmdb_id,
            "watched_at": watched.watched_at.isoformat(),
            "is_rewatch": is_rewatch,
            "rating": watched.rating,
        },
        status="pending",
    )
    db.add(job)


async def _sync_trakt(
    db: AsyncSession,
    watched: WatchedItem,
    media_item: MediaItem | None,
    episode_item: EpisodeItem | None,
    is_rewatch: bool,
) -> None:
    if not media_item:
        return
    if not settings.trakt_client_id or not settings.trakt_client_secret:
        return
    payload = build_trakt_payload(
        media_item,
        episode_item,
        watched.watched_at,
        watched.rating,
    )
    if not payload:
        return
    integration, secret_data = await load_integration_with_secrets(
        db, watched.user_id, "trakt"
    )
    if not integration or not secret_data:
        return
    if not has_required_trakt_fields(secret_data):
        return

    watch_sync = await _get_watch_sync(db, watched.id, "trakt")
    if watch_sync and is_synced_status(watch_sync.status):
        return
    if watch_sync and watch_sync.status in {"pending", "in_progress"}:
        return

    same_day_duplicate = await _has_same_day_watch(
        db,
        watched.user_id,
        media_item.id if not episode_item else None,
        episode_item.id if episode_item else None,
        watched.watched_at,
        watched.id,
    )
    now = datetime.now(timezone.utc)
    watch_status = "pending"
    if same_day_duplicate and watched.rating is None:
        watch_status = "assumed_tracked"

    if not watch_sync:
        watch_sync = WatchSync(
            user_id=watched.user_id,
            watched_item_id=watched.id,
            provider="trakt",
            status=watch_status,
            is_rewatch=is_rewatch,
        )
        if watch_status == "assumed_tracked":
            watch_sync.last_synced_at = now
        db.add(watch_sync)
        await db.flush()
    else:
        watch_sync.status = watch_status
        watch_sync.last_error = None
        if watch_status == "assumed_tracked":
            watch_sync.last_synced_at = now

    payload["watch_sync_id"] = watch_sync.id
    payload["watched_item_id"] = watched.id
    if watch_status != "assumed_tracked" and not same_day_duplicate:
        job = OutboxJob(
            user_id=watched.user_id,
            target_provider="trakt",
            job_type="push_watched",
            payload=payload,
            status="pending",
        )
        db.add(job)
    if watched.rating is not None:
        rating_payload = dict(payload)
        rating_payload["rating"] = watched.rating
        rating_job = OutboxJob(
            user_id=watched.user_id,
            target_provider="trakt",
            job_type="push_rating",
            payload=rating_payload,
            status="pending",
        )
        db.add(rating_job)


async def _sync_simkl(
    db: AsyncSession,
    watched: WatchedItem,
    media_item: MediaItem | None,
    episode_item: EpisodeItem | None,
    is_rewatch: bool,
) -> None:
    if not media_item:
        return
    if not settings.simkl_client_id or not settings.simkl_client_secret:
        return
    payload = build_simkl_payload(
        media_item,
        episode_item,
        watched.watched_at,
        watched.rating,
    )
    if not payload:
        return
    integration, secret_data = await load_integration_with_secrets(
        db, watched.user_id, "simkl"
    )
    if not integration or not secret_data:
        return
    if not has_required_simkl_fields(secret_data):
        return

    watch_sync = await _get_watch_sync(db, watched.id, "simkl")
    if watch_sync and is_synced_status(watch_sync.status):
        return
    if watch_sync and watch_sync.status in {"pending", "in_progress"}:
        return

    same_day_duplicate = await _has_same_day_watch(
        db,
        watched.user_id,
        media_item.id if not episode_item else None,
        episode_item.id if episode_item else None,
        watched.watched_at,
        watched.id,
    )
    now = datetime.now(timezone.utc)
    watch_status = "pending"
    if same_day_duplicate and watched.rating is None:
        watch_status = "assumed_tracked"

    if not watch_sync:
        watch_sync = WatchSync(
            user_id=watched.user_id,
            watched_item_id=watched.id,
            provider="simkl",
            status=watch_status,
            is_rewatch=is_rewatch,
        )
        if watch_status == "assumed_tracked":
            watch_sync.last_synced_at = now
        db.add(watch_sync)
        await db.flush()
    else:
        watch_sync.status = watch_status
        watch_sync.last_error = None
        if watch_status == "assumed_tracked":
            watch_sync.last_synced_at = now

    payload["watch_sync_id"] = watch_sync.id
    payload["watched_item_id"] = watched.id
    if watch_status != "assumed_tracked" and not same_day_duplicate:
        job = OutboxJob(
            user_id=watched.user_id,
            target_provider="simkl",
            job_type="push_watched",
            payload=payload,
            status="pending",
        )
        db.add(job)
    if watched.rating is not None:
        rating_payload = dict(payload)
        rating_payload["rating"] = watched.rating
        rating_job = OutboxJob(
            user_id=watched.user_id,
            target_provider="simkl",
            job_type="push_rating",
            payload=rating_payload,
            status="pending",
        )
        db.add(rating_job)


async def _get_watch_sync(
    db: AsyncSession, watched_id: str, provider: str
) -> WatchSync | None:
    result = await db.execute(
        select(WatchSync).where(
            WatchSync.watched_item_id == watched_id,
            WatchSync.provider == provider,
        )
    )
    return result.scalars().first()


async def _has_same_day_watch(
    db: AsyncSession,
    user_id: str,
    media_item_id: str | None,
    episode_item_id: str | None,
    watched_at: datetime,
    exclude_watched_id: str | None = None,
) -> bool:
    if not media_item_id and not episode_item_id:
        return False
    target_date = watched_at.date()
    query = select(WatchedItem.id).where(
        WatchedItem.user_id == user_id,
        func.date(WatchedItem.watched_at) == target_date,
    )
    if media_item_id:
        query = query.where(WatchedItem.media_item_id == media_item_id)
    if episode_item_id:
        query = query.where(WatchedItem.episode_item_id == episode_item_id)
    if exclude_watched_id:
        query = query.where(WatchedItem.id != exclude_watched_id)
    query = query.limit(1)
    result = await db.execute(query)
    return result.scalars().first() is not None


def build_trakt_payload(
    media_item: MediaItem,
    episode_item: EpisodeItem | None,
    watched_at: datetime,
    rating: float | None,
) -> dict[str, object] | None:
    if episode_item:
        show_ids = collect_trakt_ids(
            media_item.imdb_id, media_item.tmdb_id, media_item.tvdb_id
        )
        episode_ids = collect_trakt_ids(
            episode_item.imdb_id, episode_item.tmdb_id, episode_item.tvdb_id
        )
        if not show_ids and not episode_ids:
            return None
        payload: dict[str, object] = {
            "media_type": "tv",
            "season_number": episode_item.season_number,
            "episode_number": episode_item.episode_number,
            "watched_at": watched_at.isoformat(),
        }
        if show_ids:
            payload["show_ids"] = show_ids
        if episode_ids:
            payload["episode_ids"] = episode_ids
        if rating is not None:
            payload["rating"] = rating
        return payload

    if media_item.media_type != "movie":
        return None
    movie_ids = collect_trakt_ids(
        media_item.imdb_id, media_item.tmdb_id, media_item.tvdb_id
    )
    if not movie_ids:
        return None
    payload: dict[str, object] = {
        "media_type": "movie",
        "movie_ids": movie_ids,
        "watched_at": watched_at.isoformat(),
    }
    if rating is not None:
        payload["rating"] = rating
    return payload


def collect_trakt_ids(
    imdb_id: str | None, tmdb_id: str | None, tvdb_id: str | None
) -> dict[str, object]:
    ids: dict[str, object] = {}
    if imdb_id:
        ids["imdb"] = imdb_id.lower()
    if tmdb_id:
        ids["tmdb"] = tmdb_id
    if tvdb_id:
        ids["tvdb"] = tvdb_id
    return ids


def build_simkl_payload(
    media_item: MediaItem,
    episode_item: EpisodeItem | None,
    watched_at: datetime,
    rating: float | None,
) -> dict[str, object] | None:
    if episode_item:
        show_ids = collect_simkl_ids(
            media_item.imdb_id, media_item.tmdb_id, media_item.tvdb_id
        )
        episode_ids = collect_simkl_ids(
            episode_item.imdb_id, episode_item.tmdb_id, episode_item.tvdb_id
        )
        if not show_ids and not episode_ids:
            return None
        payload: dict[str, object] = {
            "media_type": "tv",
            "season_number": episode_item.season_number,
            "episode_number": episode_item.episode_number,
            "watched_at": watched_at.isoformat(),
        }
        if show_ids:
            payload["show_ids"] = show_ids
        if episode_ids:
            payload["episode_ids"] = episode_ids
        if rating is not None:
            payload["rating"] = rating
        return payload

    if media_item.media_type != "movie":
        return None
    movie_ids = collect_simkl_ids(
        media_item.imdb_id, media_item.tmdb_id, media_item.tvdb_id
    )
    if not movie_ids:
        return None
    payload: dict[str, object] = {
        "media_type": "movie",
        "movie_ids": movie_ids,
        "watched_at": watched_at.isoformat(),
    }
    if rating is not None:
        payload["rating"] = rating
    return payload


def collect_simkl_ids(
    imdb_id: str | None, tmdb_id: str | None, tvdb_id: str | None
) -> dict[str, object]:
    ids: dict[str, object] = {}
    if imdb_id:
        ids["imdb"] = imdb_id.lower()
    if tmdb_id:
        ids["tmdb"] = tmdb_id
    if tvdb_id:
        ids["tvdb"] = tvdb_id
    return ids


SYNC_STRATEGIES: dict[str, SyncStrategy] = {
    "letterboxd": _sync_letterboxd,
    "trakt": _sync_trakt,
    "simkl": _sync_simkl,
}
