import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy import exists, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from librarysync.core.release_dates import get_release_now_date
from librarysync.core.scheduler import (
    claim_scheduled_job,
    complete_scheduled_job,
    extend_scheduled_job,
    release_scheduled_job,
)
from librarysync.core.watchlist import (
    apply_watchlist_status_change,
    backfill_show_episodes,
    determine_movie_watchlist_status,
    evaluate_show_watchlist_status,
)
from librarysync.db.models import EpisodeItem, MediaItem, ScheduledJob, User, WatchlistItem
from librarysync.db.session import SessionLocal, init_session_factory

logger = logging.getLogger(__name__)
WATCHLIST_REFRESH_JOB = "watchlist_refresh"
WATCHLIST_REFRESH_INTERVAL = timedelta(minutes=30)
WATCHLIST_REFRESH_LEASE = timedelta(minutes=20)
WATCHLIST_REFRESH_RETRY_DELAY = timedelta(minutes=5)
WATCHLIST_REFRESH_STATUSES = (
    "added",
    "in_progress",
    "watched",
    "not_released",
    "active",
    "waiting",
    "hidden",
)


async def process_watchlist_refresh_once() -> int:
    init_session_factory()
    async with SessionLocal() as db:
        job = await claim_scheduled_job(
            db,
            WATCHLIST_REFRESH_JOB,
            WATCHLIST_REFRESH_INTERVAL,
            WATCHLIST_REFRESH_LEASE,
        )
        if not job:
            return 0
        try:
            await run_watchlist_refresh(db, job)
        except Exception:
            logger.exception("Watchlist refresh failed")
            await release_scheduled_job(db, job, WATCHLIST_REFRESH_RETRY_DELAY)
            return 0
        await complete_scheduled_job(db, job, WATCHLIST_REFRESH_INTERVAL)
    return 1


async def run_watchlist_refresh(db: AsyncSession, job: ScheduledJob) -> None:
    logger.info("Starting watchlist refresh")
    last_run_at = job.last_run_at

    # Get all users
    result = await db.execute(select(User.id))
    user_ids = result.scalars().all()

    for user_id in user_ids:
        # Get all active/hidden items
        rows = await _load_watchlist_rows_for_refresh(db, user_id, last_run_at)

        await _backfill_missing_show_episodes(db, user_id, rows)

        for item, media in rows:
            if media.media_type == "movie":
                await _refresh_movie_status(db, user_id, item, media)
            elif media.media_type == "tv":
                await evaluate_show_watchlist_status(db, user_id, item, media)
        await extend_scheduled_job(db, job, WATCHLIST_REFRESH_LEASE)

    await db.commit()
    logger.info("Finished daily watchlist refresh")


async def _load_watchlist_rows_for_refresh(
    db: AsyncSession,
    user_id: str,
    last_run_at: datetime | None,
) -> list[tuple[WatchlistItem, MediaItem]]:
    base_query = (
        select(WatchlistItem, MediaItem)
        .join(MediaItem, WatchlistItem.media_item_id == MediaItem.id)
        .where(
            WatchlistItem.user_id == user_id,
            WatchlistItem.status.in_(WATCHLIST_REFRESH_STATUSES),
        )
    )
    if not last_run_at:
        return (await db.execute(base_query)).all()

    now = datetime.now(timezone.utc)
    now_date = get_release_now_date(now)
    last_run_date = last_run_at.date()

    media_ids: set[str] = set()

    new_watchlist_result = await db.execute(
        select(MediaItem.id)
        .join(WatchlistItem, WatchlistItem.media_item_id == MediaItem.id)
        .where(
            WatchlistItem.user_id == user_id,
            WatchlistItem.status.in_(WATCHLIST_REFRESH_STATUSES),
            or_(
                WatchlistItem.created_at > last_run_at,
                WatchlistItem.updated_at > last_run_at,
            ),
        )
    )
    media_ids.update(new_watchlist_result.scalars().all())

    updated_result = await db.execute(
        select(MediaItem.id)
        .join(WatchlistItem, WatchlistItem.media_item_id == MediaItem.id)
        .where(
            WatchlistItem.user_id == user_id,
            WatchlistItem.status.in_(WATCHLIST_REFRESH_STATUSES),
            MediaItem.updated_at.is_not(None),
            MediaItem.updated_at > last_run_at,
        )
    )
    media_ids.update(updated_result.scalars().all())

    episode_update_result = await db.execute(
        select(EpisodeItem.show_media_item_id)
        .join(MediaItem, EpisodeItem.show_media_item_id == MediaItem.id)
        .join(WatchlistItem, WatchlistItem.media_item_id == MediaItem.id)
        .where(
            WatchlistItem.user_id == user_id,
            WatchlistItem.status.in_(WATCHLIST_REFRESH_STATUSES),
            MediaItem.media_type == "tv",
            EpisodeItem.updated_at.is_not(None),
            EpisodeItem.updated_at > last_run_at,
        )
        .distinct()
    )
    media_ids.update(episode_update_result.scalars().all())

    released_result = await db.execute(
        select(EpisodeItem.show_media_item_id)
        .join(MediaItem, EpisodeItem.show_media_item_id == MediaItem.id)
        .join(WatchlistItem, WatchlistItem.media_item_id == MediaItem.id)
        .where(
            WatchlistItem.user_id == user_id,
            WatchlistItem.status.in_(WATCHLIST_REFRESH_STATUSES),
            MediaItem.media_type == "tv",
            EpisodeItem.air_date.is_not(None),
            EpisodeItem.air_date >= last_run_date,
            EpisodeItem.air_date <= now_date,
            EpisodeItem.season_number > 0,
        )
        .distinct()
    )
    media_ids.update(released_result.scalars().all())

    movie_release_result = await db.execute(
        select(MediaItem.id)
        .join(WatchlistItem, WatchlistItem.media_item_id == MediaItem.id)
        .where(
            WatchlistItem.user_id == user_id,
            WatchlistItem.status.in_(WATCHLIST_REFRESH_STATUSES),
            MediaItem.media_type == "movie",
            MediaItem.release_date.is_not(None),
            MediaItem.release_date >= last_run_date,
            MediaItem.release_date <= now_date,
        )
    )
    media_ids.update(movie_release_result.scalars().all())

    if now_date.year > last_run_date.year:
        movie_year_result = await db.execute(
            select(MediaItem.id)
            .join(WatchlistItem, WatchlistItem.media_item_id == MediaItem.id)
            .where(
                WatchlistItem.user_id == user_id,
                WatchlistItem.status.in_(WATCHLIST_REFRESH_STATUSES),
                MediaItem.media_type == "movie",
                MediaItem.release_date.is_(None),
                MediaItem.year.is_not(None),
                MediaItem.year > last_run_date.year,
                MediaItem.year <= now_date.year,
            )
        )
        media_ids.update(movie_year_result.scalars().all())

    missing_episodes_result = await db.execute(
        select(MediaItem.id)
        .join(WatchlistItem, WatchlistItem.media_item_id == MediaItem.id)
        .where(
            WatchlistItem.user_id == user_id,
            WatchlistItem.status.in_(WATCHLIST_REFRESH_STATUSES),
            MediaItem.media_type == "tv",
            ~exists(
                select(EpisodeItem.id).where(
                    EpisodeItem.show_media_item_id == MediaItem.id
                )
            ),
        )
    )
    media_ids.update(missing_episodes_result.scalars().all())

    if not media_ids:
        return []

    result = await db.execute(base_query.where(MediaItem.id.in_(media_ids)))
    return result.all()


async def _backfill_missing_show_episodes(
    db: AsyncSession,
    user_id: str,
    rows: list[tuple[WatchlistItem, MediaItem]],
) -> None:
    media_by_id = {
        media.id: media for item, media in rows if media.media_type == "tv"
    }
    if not media_by_id:
        return

    result = await db.execute(
        select(EpisodeItem.show_media_item_id)
        .where(EpisodeItem.show_media_item_id.in_(list(media_by_id.keys())))
        .distinct()
    )
    existing_ids = set(result.scalars().all())
    missing_ids = [media_id for media_id in media_by_id.keys() if media_id not in existing_ids]
    for media_id in missing_ids:
        media_item = media_by_id[media_id]
        if not (media_item.tmdb_id or media_item.imdb_id):
            continue
        await backfill_show_episodes(db, user_id, media_item)


async def _refresh_movie_status(
    db: AsyncSession,
    user_id: str,
    item: WatchlistItem,
    media: MediaItem,
) -> None:
    now_date = get_release_now_date()
    has_watched = item.status == "watched"
    new_status = determine_movie_watchlist_status(
        media,
        has_watched=has_watched,
        now_date=now_date,
    )

    if item.status != new_status:
        await apply_watchlist_status_change(
            db,
            item,
            user_id,
            media.id,
            new_status,
            reason="release_date_check",
        )
