from datetime import datetime, timezone
from sqlalchemy import func, select, and_, or_
from sqlalchemy.ext.asyncio import AsyncSession
from librarysync.db.models import (
    WatchlistItem,
    MediaItem,
    EpisodeItem,
    WatchedItem,
    WatchEvent,
)


async def check_and_update_watchlist(
    db: AsyncSession,
    user_id: str,
    media_item_id: str,
) -> None:
    result = await db.execute(
        select(WatchlistItem).where(
            WatchlistItem.user_id == user_id,
            WatchlistItem.media_item_id == media_item_id,
        )
    )
    item = result.scalars().first()
    if not item:
        return

    result = await db.execute(select(MediaItem).where(MediaItem.id == media_item_id))
    media_item = result.scalars().first()
    if not media_item:
        return

    if media_item.media_type == "movie":
        if item.status != "watched":
            item.status = "watched"
            item.updated_at = datetime.now(timezone.utc)
            await log_watchlist_event(
                db,
                user_id,
                media_item_id,
                "watchlist_status_changed",
                {"status": "watched", "reason": "watched"},
            )

    elif media_item.media_type == "tv":
        await evaluate_show_watchlist_status(db, user_id, item, media_item)


async def evaluate_show_watchlist_status(
    db: AsyncSession,
    user_id: str,
    watchlist_item: WatchlistItem,
    media_item: MediaItem,
) -> None:
    # Logic:
    # 1. Get all released episodes for this show
    # 2. Get all watched episodes for this show by user
    # 3. If there is at least one released episode that is NOT watched -> Active
    # 4. If all released episodes are watched -> Waiting (if returning) or Watched (if ended)

    if watchlist_item.status == "removed":
        return

    # Find released episodes
    now_date = datetime.now(timezone.utc).date()

    result = await db.execute(
        select(EpisodeItem).where(
            EpisodeItem.show_media_item_id == media_item.id,
            EpisodeItem.air_date != None,
            EpisodeItem.air_date <= now_date,
        )
    )
    released_episodes = result.scalars().all()

    if not released_episodes:
        # If no released episodes found, check if we have any episodes at all
        all_eps = await db.execute(
            select(func.count(EpisodeItem.id)).where(
                EpisodeItem.show_media_item_id == media_item.id
            )
        )
        count = all_eps.scalar() or 0
        if count == 0:
            return

    # Get watched episode IDs
    result = await db.execute(
        select(WatchedItem.episode_item_id).where(
            WatchedItem.user_id == user_id,
            WatchedItem.media_item_id == None,
            WatchedItem.episode_item_id.in_([e.id for e in released_episodes]),
        )
    )
    watched_episode_ids = set(result.scalars().all())

    has_unwatched = False
    for ep in released_episodes:
        if ep.id not in watched_episode_ids:
            has_unwatched = True
            break

    new_status = "active"
    if not has_unwatched:
        # All released episodes watched.
        # Check if show status implies more episodes coming
        # If unknown, assume waiting.
        # "waiting" corresponds to "Caught up / Hidden"
        new_status = "waiting"
        # Ideally we check media_item.status if we had it (e.g. "Ended", "Canceled")
        # For now, "waiting" is safe default for caught up.
        # If we knew it ended, we could set "watched".
        # Let's use "waiting" for now as it hides it from "Active" list.

    if watchlist_item.status != new_status:
        watchlist_item.status = new_status
        watchlist_item.updated_at = datetime.now(timezone.utc)
        await log_watchlist_event(
            db,
            user_id,
            media_item.id,
            "watchlist_status_changed",
            {"status": new_status, "reason": "auto_evaluation"},
        )


async def log_watchlist_event(
    db: AsyncSession, user_id: str, media_item_id: str, event_type: str, raw: dict
) -> None:
    event = WatchEvent(
        user_id=user_id,
        media_item_id=media_item_id,
        event_type=event_type,
        occurred_at=datetime.now(timezone.utc),
        raw=raw,
    )
    db.add(event)
