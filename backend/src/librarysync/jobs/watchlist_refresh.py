import logging
from datetime import datetime, timedelta, timezone
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from librarysync.db.models import User, WatchlistItem, MediaItem
from librarysync.db.session import SessionLocal, init_session_factory
from librarysync.core.scheduler import (
    claim_scheduled_job,
    complete_scheduled_job,
    release_scheduled_job,
)
from librarysync.core.watchlist import (
    determine_movie_watchlist_status,
    evaluate_show_watchlist_status,
    log_watchlist_event,
)

logger = logging.getLogger(__name__)
WATCHLIST_REFRESH_JOB = "watchlist_refresh"
WATCHLIST_REFRESH_INTERVAL = timedelta(minutes=30)
WATCHLIST_REFRESH_LEASE = timedelta(minutes=20)
WATCHLIST_REFRESH_RETRY_DELAY = timedelta(minutes=5)


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
            await run_watchlist_refresh(db)
        except Exception:
            logger.exception("Watchlist refresh failed")
            await release_scheduled_job(db, job, WATCHLIST_REFRESH_RETRY_DELAY)
            return 0
        await complete_scheduled_job(db, job, WATCHLIST_REFRESH_INTERVAL)
    return 1


async def run_watchlist_refresh(db: AsyncSession) -> None:
    logger.info("Starting watchlist refresh")

    # Get all users
    result = await db.execute(select(User.id))
    user_ids = result.scalars().all()

    for user_id in user_ids:
        # Get all active/hidden items
        items_result = await db.execute(
            select(WatchlistItem, MediaItem)
            .join(MediaItem, WatchlistItem.media_item_id == MediaItem.id)
            .where(
                WatchlistItem.user_id == user_id,
                WatchlistItem.status.in_(
                    [
                        "added",
                        "in_progress",
                        "watched",
                        "not_released",
                        "active",
                        "waiting",
                        "hidden",
                    ]
                ),
            )
        )
        rows = items_result.all()

        for item, media in rows:
            if media.media_type == "movie":
                await _refresh_movie_status(db, user_id, item, media)
            elif media.media_type == "tv":
                await evaluate_show_watchlist_status(db, user_id, item, media)

    await db.commit()
    logger.info("Finished daily watchlist refresh")


async def _refresh_movie_status(
    db: AsyncSession,
    user_id: str,
    item: WatchlistItem,
    media: MediaItem,
) -> None:
    now_date = datetime.now(timezone.utc).date()
    has_watched = item.status == "watched"
    new_status = determine_movie_watchlist_status(
        media,
        has_watched=has_watched,
        now_date=now_date,
    )

    if item.status != new_status:
        item.status = new_status
        item.updated_at = datetime.now(timezone.utc)
        await log_watchlist_event(
            db,
            user_id,
            media.id,
            "watchlist_status_changed",
            {"status": new_status, "reason": "release_date_check"},
        )
