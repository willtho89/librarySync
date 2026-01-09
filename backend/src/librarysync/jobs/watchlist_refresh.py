import logging
from datetime import datetime, timezone
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from librarysync.db.models import User, WatchlistItem, MediaItem
from librarysync.db.session import SessionLocal, init_session_factory
from librarysync.core.watchlist import evaluate_show_watchlist_status, log_watchlist_event

logger = logging.getLogger(__name__)


async def process_watchlist_refresh_once() -> int:
    init_session_factory()
    async with SessionLocal() as db:
        await run_watchlist_refresh(db)
    return 1


async def run_watchlist_refresh(db: AsyncSession) -> None:
    logger.info("Starting daily watchlist refresh")

    # Get all users
    result = await db.execute(select(User.id))
    user_ids = result.scalars().all()

    for user_id in user_ids:
        # Get all active/hidden items
        items_result = await db.execute(
            select(WatchlistItem, MediaItem)
            .join(MediaItem, WatchlistItem.media_item_id == MediaItem.id)
            .where(WatchlistItem.user_id == user_id, WatchlistItem.status.in_(["active", "hidden"]))
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
    if not media.release_date:
        return

    is_released = media.release_date <= now_date
    new_status = "active" if is_released else "hidden"

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
