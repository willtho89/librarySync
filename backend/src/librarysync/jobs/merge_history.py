from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from librarysync.core.import_all import (
    DEFAULT_IMPORT_QUEUE_ORDER,
    IMPORT_ALL_PROVIDER,
    get_import_queue_order,
    import_all_active,
)
from librarysync.core.import_control import (
    MERGE_REQUIRED_AT_KEY,
    mark_merge_completed,
    mark_merge_failed,
    merge_pending,
    quick_import_active,
)
from librarysync.core.import_history import update_import_history_merge
from librarysync.core.import_schedule import parse_datetime
from librarysync.core.scheduler import (
    claim_scheduled_job,
    complete_scheduled_job,
    extend_scheduled_job,
    release_scheduled_job,
)
from librarysync.db.models import (
    EpisodeItem,
    Integration,
    MediaItem,
    OutboxJob,
    ScheduledJob,
    User,
    WatchedItem,
    WatchSync,
)
from librarysync.db.session import SessionLocal, init_session_factory

logger = logging.getLogger(__name__)
MERGE_ALL_HISTORY_JOB = "merge_all_history"
MERGE_ALL_HISTORY_INTERVAL = timedelta(days=1)
MERGE_ALL_HISTORY_LEASE = timedelta(hours=2)
MERGE_ALL_HISTORY_RETRY_DELAY = timedelta(hours=1)
MERGE_PENDING_JOB = "merge_history"
MERGE_PENDING_INTERVAL = timedelta(minutes=5)
MERGE_PENDING_LEASE = timedelta(hours=1)
MERGE_PENDING_RETRY_DELAY = timedelta(minutes=10)


async def enqueue_merge_history(db: AsyncSession, now: datetime | None = None) -> None:
    if now is None:
        now = datetime.now(timezone.utc)
    result = await db.execute(
        select(ScheduledJob).where(ScheduledJob.name == MERGE_PENDING_JOB).with_for_update()
    )
    job = result.scalars().first()
    if not job:
        job = ScheduledJob(name=MERGE_PENDING_JOB, next_run_at=now)
        db.add(job)
        return
    if job.next_run_at is None or job.next_run_at > now:
        job.next_run_at = now
        job.updated_at = now


@dataclass
class WatchedRow:
    watched: WatchedItem
    media: MediaItem
    episode: EpisodeItem | None = None


async def merge_history_for_user(db: AsyncSession, user_id: str) -> int:
    system_result = await db.execute(
        select(Integration.config).where(
            Integration.user_id == user_id,
            Integration.provider == IMPORT_ALL_PROVIDER,
        )
    )
    system_config = system_result.scalar_one_or_none()
    queue_order = get_import_queue_order(system_config)
    if not queue_order:
        queue_order = list(DEFAULT_IMPORT_QUEUE_ORDER)
    priority_map = {provider: index for index, provider in enumerate(queue_order)}
    show_item = aliased(MediaItem)
    result = await db.execute(
        select(WatchedItem, MediaItem, show_item, EpisodeItem)
        .outerjoin(MediaItem, WatchedItem.media_item_id == MediaItem.id)
        .outerjoin(EpisodeItem, WatchedItem.episode_item_id == EpisodeItem.id)
        .outerjoin(show_item, EpisodeItem.show_media_item_id == show_item.id)
        .where(WatchedItem.user_id == user_id)
        .order_by(WatchedItem.watched_at)
    )
    rows = [
        WatchedRow(watched, media or show, episode)
        for watched, media, show, episode in result.all()
    ]
    if not rows:
        return 0
    merged_count = await _merge_history(db, rows, priority_map)
    if merged_count:
        logger.info("Merged %s watched entries for user %s", merged_count, user_id)
    return merged_count


async def _merge_history(
    db: AsyncSession,
    rows: list[WatchedRow],
    priority_map: dict[str, int],
) -> int:
    movie_rows = [row for row in rows if row.episode is None]
    tv_rows = [row for row in rows if row.episode is not None]

    merged = 0
    # Merge movies (same-day)
    movie_grouped: dict[tuple[str, date], list[WatchedRow]] = {}
    for row in movie_rows:
        watched_date = row.watched.watched_at.date()
        key = (row.watched.user_id, watched_date)
        movie_grouped.setdefault(key, []).append(row)

    for (user_id, watched_date), day_rows in movie_grouped.items():
        clusters = _cluster_rows(day_rows)
        for cluster in clusters:
            if len(cluster) < 2:
                continue
            merged += await _merge_cluster(
                db,
                user_id,
                watched_date,
                cluster,
                priority_map,
            )

    # Merge TV episodes (same-day per episode)
    tv_grouped: dict[tuple[str, date, int, int], list[WatchedRow]] = {}
    for row in tv_rows:
        watched_date = row.watched.watched_at.date()
        season = row.episode.season_number
        episode = row.episode.episode_number
        key = (row.watched.user_id, watched_date, season, episode)
        tv_grouped.setdefault(key, []).append(row)

    for (user_id, watched_date, season, episode), day_rows in tv_grouped.items():
        clusters = _cluster_rows(day_rows)
        for cluster in clusters:
            if len(cluster) < 2:
                continue
            merged += await _merge_cluster(
                db,
                user_id,
                watched_date,
                cluster,
                priority_map,
            )
    return merged


def _cluster_rows(rows: list[WatchedRow]) -> list[list[WatchedRow]]:
    key_map: dict[str, str] = {}
    groups: dict[str, list[WatchedRow]] = {}
    for row in rows:
        keys = _merge_keys(row)
        group_id = None
        for key in keys:
            group_id = key_map.get(key)
            if group_id:
                break
        if not group_id:
            group_id = keys[0] if keys else f"id:{row.watched.id}"
            groups[group_id] = [row]
            for key in keys:
                key_map[key] = group_id
            continue
        groups[group_id].append(row)
        for key in keys:
            key_map.setdefault(key, group_id)
    return list(groups.values())


async def _merge_cluster(
    db: AsyncSession,
    user_id: str,
    watched_date: date,
    cluster: list[WatchedRow],
    priority_map: dict[str, int],
) -> int:
    primary = max(cluster, key=lambda row: _row_sort_key(row, priority_map))
    if primary.watched.source == "letterboxd":
        logger.debug("Merging letterboxd item on %s for user %s", watched_date, user_id)
    primary_media = primary.media
    primary_watched = primary.watched

    duplicate_rows = [row for row in cluster if row.watched.id != primary_watched.id]
    if not duplicate_rows:
        return 0

    _merge_media(primary_media, [row.media for row in duplicate_rows])
    _merge_watched(primary_watched, [row.watched for row in duplicate_rows])

    duplicate_ids = [row.watched.id for row in duplicate_rows]
    syncs = await _load_syncs(db, duplicate_ids + [primary_watched.id])
    sync_map, delete_syncs = _select_syncs(syncs, primary_watched.id)
    await _repoint_outbox_jobs(db, sync_map, primary_watched.id, duplicate_ids)
    for sync in delete_syncs:
        await db.delete(sync)

    for row in duplicate_rows:
        await db.delete(row.watched)

    await db.commit()
    return len(duplicate_rows)


def _merge_keys(row: WatchedRow) -> list[str]:
    media = row.media
    keys: list[str] = []
    if row.episode:
        # TV episode
        episode = row.episode
        for label in ("imdb_id", "tmdb_id", "tvdb_id", "tvmaze_id"):
            value = getattr(episode, label)
            if value:
                keys.append(f"episode:id:{label}:{value}")
        for label in ("imdb_id", "tmdb_id", "tvdb_id", "tvmaze_id"):
            value = getattr(media, label)
            if value:
                keys.append(f"show:id:{label}:{value}")
        title_key = _title_key(media.title, media.year)
        if title_key:
            season = episode.season_number
            ep_num = episode.episode_number
            keys.append(f"show:title:{title_key}:s{season}e{ep_num}")
    else:
        # Movie
        for label in ("imdb_id", "tmdb_id", "tvdb_id"):
            value = getattr(media, label)
            if value:
                keys.append(f"id:{label}:{value}")
        title_key = _title_key(media.title, media.year)
        if title_key:
            keys.append(f"title:{title_key}")
    if not keys:
        keys.append(f"id:{row.watched.id}")
    return keys


def _row_sort_key(row: WatchedRow, priority_map: dict[str, int]) -> tuple[int, int]:
    score = 0
    media = row.media
    if row.episode:
        episode = row.episode
        if episode.imdb_id:
            score += 6
        if episode.tmdb_id:
            score += 5
        if episode.tvdb_id:
            score += 4
        if episode.tvmaze_id:
            score += 3
        if media.imdb_id:
            score += 3
        if media.tmdb_id:
            score += 2
        if media.tvdb_id:
            score += 2
    else:
        if media.imdb_id:
            score += 6
        if media.tmdb_id:
            score += 5
        if media.tvdb_id:
            score += 4
    if row.watched.source in {"trakt", "simkl"}:
        score += 4
    elif row.watched.source == "manual":
        score += 2
    elif row.watched.source == "aiostreams":
        score += 1
    if media.poster_url:
        score += 1
    if media.title:
        score += 1
    source = row.watched.source or ""
    rank = priority_map.get(source, len(priority_map) + 1)
    return (score, -rank)


def _merge_media(primary: MediaItem, others: list[MediaItem]) -> None:
    for other in others:
        if not primary.imdb_id and other.imdb_id:
            primary.imdb_id = other.imdb_id
        if not primary.tmdb_id and other.tmdb_id:
            primary.tmdb_id = other.tmdb_id
        if not primary.tvdb_id and other.tvdb_id:
            primary.tvdb_id = other.tvdb_id
        if primary.year is None and other.year is not None:
            primary.year = other.year
        if not primary.poster_url and other.poster_url:
            primary.poster_url = other.poster_url
        if other.title and (not primary.title or len(other.title) > len(primary.title)):
            primary.title = other.title
        if isinstance(primary.raw, dict) and isinstance(other.raw, dict):
            for key, value in other.raw.items():
                primary.raw.setdefault(key, value)
        elif primary.raw is None and other.raw is not None:
            primary.raw = dict(other.raw)


def _merge_watched(primary: WatchedItem, others: list[WatchedItem]) -> None:
    for other in others:
        if primary.rating is None and other.rating is not None:
            primary.rating = other.rating
        primary.watched_at = _prefer_precise_time(primary.watched_at, other.watched_at)


def _prefer_precise_time(current: datetime, candidate: datetime) -> datetime:
    if candidate is None:
        return current
    if current is None:
        return candidate
    if _is_midnight(current) and not _is_midnight(candidate):
        return candidate
    if not _is_midnight(current) and _is_midnight(candidate):
        return current
    return max(current, candidate)


def _is_midnight(value: datetime) -> bool:
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.time().hour == 0 and value.time().minute == 0 and value.time().second == 0


async def _load_syncs(db: AsyncSession, watched_ids: list[str]) -> list[WatchSync]:
    result = await db.execute(select(WatchSync).where(WatchSync.watched_item_id.in_(watched_ids)))
    return result.scalars().all()


def _select_syncs(
    syncs: list[WatchSync], primary_watched_id: str
) -> tuple[dict[str, str], list[WatchSync]]:
    by_provider: dict[str, list[WatchSync]] = {}
    for sync in syncs:
        by_provider.setdefault(sync.provider, []).append(sync)

    mapping: dict[str, str] = {}
    to_delete: list[WatchSync] = []
    for provider, provider_syncs in by_provider.items():
        primary_sync = next(
            (sync for sync in provider_syncs if sync.watched_item_id == primary_watched_id),
            None,
        )
        if primary_sync:
            keep = primary_sync
            others = [sync for sync in provider_syncs if sync.id != keep.id]
        else:
            provider_syncs.sort(key=_sync_score, reverse=True)
            keep = provider_syncs[0]
            others = provider_syncs[1:]
        for sync in others:
            _merge_sync_fields(keep, sync)
            mapping[sync.id] = keep.id
            to_delete.append(sync)
        if keep.watched_item_id != primary_watched_id:
            keep.watched_item_id = primary_watched_id
    return mapping, to_delete


def _sync_score(sync: WatchSync) -> int:
    status_rank = {
        "succeeded": 5,
        "synced_from_trakt": 5,
        "synced_from_letterboxd": 5,
        "synced_from_simkl": 5,
        "synced_from_anilist": 5,
        "synced_from_stremio": 5,
        "synced_from_aiostreams": 5,
        "assumed_tracked": 4,
        "pending": 3,
        "in_progress": 2,
        "failed_retryable": 1,
        "failed_permanent": 0,
    }
    score = status_rank.get(sync.status, 0)
    if sync.external_id:
        score += 2
    if sync.last_error:
        score -= 1
    return score


def _merge_sync_fields(primary: WatchSync, other: WatchSync) -> None:
    if not primary.external_id and other.external_id:
        primary.external_id = other.external_id
    if primary.last_error and not other.last_error:
        primary.last_error = None
    if other.status and _sync_score(other) > _sync_score(primary):
        primary.status = other.status
    if other.last_synced_at and not primary.last_synced_at:
        primary.last_synced_at = other.last_synced_at
    if other.is_rewatch and not primary.is_rewatch:
        primary.is_rewatch = other.is_rewatch


async def _repoint_outbox_jobs(
    db: AsyncSession,
    sync_map: dict[str, str],
    primary_watched_id: str,
    duplicate_watched_ids: list[str],
) -> None:
    if not sync_map and not duplicate_watched_ids:
        return
    criteria = []
    if sync_map:
        criteria.append(OutboxJob.payload["watch_sync_id"].as_string().in_(list(sync_map.keys())))
    if duplicate_watched_ids:
        criteria.append(OutboxJob.payload["watched_item_id"].as_string().in_(duplicate_watched_ids))
    result = await db.execute(select(OutboxJob).where(or_(*criteria)))
    jobs = result.scalars().all()
    duplicate_set = set(duplicate_watched_ids)
    for job in jobs:
        payload = dict(job.payload or {})
        old_sync_id = payload.get("watch_sync_id")
        if old_sync_id in sync_map:
            payload["watch_sync_id"] = sync_map[old_sync_id]
        if payload.get("watched_item_id") in duplicate_set or sync_map:
            payload["watched_item_id"] = primary_watched_id
        job.payload = payload


def _title_key(title: object, year: object) -> str | None:
    if not isinstance(title, str):
        return None
    cleaned = re.sub(r"[^a-z0-9]+", " ", title.strip().lower())
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    if not cleaned:
        return None
    year_value = str(year) if isinstance(year, int) else ""
    return f"{cleaned}:{year_value}" if year_value else cleaned


async def process_merge_history_once() -> int:
    init_session_factory()
    async with SessionLocal() as db:
        job = await claim_scheduled_job(
            db,
            MERGE_PENDING_JOB,
            MERGE_PENDING_INTERVAL,
            MERGE_PENDING_LEASE,
        )
        if not job:
            return 0
        try:
            total = await run_merge_history(db, job)
        except Exception:
            logger.exception("Merge history failed")
            await release_scheduled_job(db, job, MERGE_PENDING_RETRY_DELAY)
            return 0
        await complete_scheduled_job(db, job, MERGE_PENDING_INTERVAL)
        return total


async def run_merge_history(db: AsyncSession, job: ScheduledJob) -> int:
    logger.info("Starting merge history")
    result = await db.execute(
        select(Integration).where(Integration.provider == IMPORT_ALL_PROVIDER)
    )
    integrations = result.scalars().all()
    total = 0
    now = datetime.now(timezone.utc)
    for integration in integrations:
        config = dict(integration.config or {})
        if not merge_pending(config):
            continue
        if import_all_active(config) or quick_import_active(config):
            continue
        required_at = parse_datetime(config.get(MERGE_REQUIRED_AT_KEY))
        try:
            await merge_history_for_user(db, integration.user_id)
            config = mark_merge_completed(config, now)
            config = update_import_history_merge(config, required_at, now, None)
        except Exception as exc:
            await db.rollback()
            error = str(exc)[:500]
            config = mark_merge_failed(config, now, error)
            config = update_import_history_merge(config, required_at, now, error)
        integration.config = config
        integration.updated_at = now
        db.add(integration)
        await db.commit()
        total += 1
        await extend_scheduled_job(db, job, MERGE_PENDING_LEASE)
    logger.info("Finished merge history (users=%s)", total)
    return total


async def process_merge_all_history_once() -> int:
    init_session_factory()
    async with SessionLocal() as db:
        job = await claim_scheduled_job(
            db,
            MERGE_ALL_HISTORY_JOB,
            MERGE_ALL_HISTORY_INTERVAL,
            MERGE_ALL_HISTORY_LEASE,
        )
        if not job:
            return 0
        try:
            total = await run_merge_all_history(db, job)
        except Exception:
            logger.exception("Merge-all history failed")
            await release_scheduled_job(db, job, MERGE_ALL_HISTORY_RETRY_DELAY)
            return 0
        await complete_scheduled_job(db, job, MERGE_ALL_HISTORY_INTERVAL)
        return total


async def run_merge_all_history(db: AsyncSession, job: ScheduledJob) -> int:
    logger.info("Starting merge-all history")
    result = await db.execute(select(User.id))
    users = [row[0] for row in result.all()]
    total = 0
    for uid in users:
        total += await merge_history_for_user(db, uid)
        await extend_scheduled_job(db, job, MERGE_ALL_HISTORY_LEASE)
    logger.info("Finished merge-all history (merged=%s)", total)
    return total
