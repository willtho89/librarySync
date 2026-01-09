import logging
from datetime import datetime, timezone

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from librarysync.connectors.metadata.base import EpisodeMetadataProvider
from librarysync.core.metadata_providers import MetadataProviderService
from librarysync.db.models import (
    EpisodeItem,
    MediaItem,
    WatchedItem,
    WatchEvent,
    WatchlistItem,
)

logger = logging.getLogger(__name__)


def _parse_air_date(value: str | None) -> datetime.date | None:
    if not value:
        return None
    try:
        return datetime.strptime(value, "%Y-%m-%d").date()
    except ValueError:
        return None


async def _persist_episode_list_for_media_item(
    db: AsyncSession,
    media_item: MediaItem,
    provider: str,
    provider_item_id: str,
    season_number: int,
    episodes: list,
) -> bool:
    if not episodes:
        return False

    result = await db.execute(
        select(EpisodeItem).where(
            EpisodeItem.show_media_item_id == media_item.id,
            EpisodeItem.season_number == season_number,
        )
    )
    existing = result.scalars().all()
    by_number = {item.episode_number: item for item in existing}
    by_tmdb = {item.tmdb_id: item for item in existing if item.tmdb_id}

    dirty = False
    for episode in episodes:
        episode_number = episode.episode_number
        if episode_number is None:
            continue
        tmdb_id = episode.provider_id if provider == "tmdb" else None
        episode_item = None
        if tmdb_id:
            episode_item = by_tmdb.get(tmdb_id)
        if not episode_item:
            episode_item = by_number.get(episode_number)

        air_date = _parse_air_date(episode.air_date)
        raw = {
            "source": "metadata",
            "provider": provider,
            "provider_item_id": provider_item_id,
        }
        if episode.still_url:
            raw["still_url"] = episode.still_url

        if not episode_item:
            episode_item = EpisodeItem(
                show_media_item_id=media_item.id,
                season_number=season_number,
                episode_number=episode_number,
                title=episode.title,
                air_date=air_date,
                tmdb_id=tmdb_id,
                raw=raw,
            )
            db.add(episode_item)
            dirty = True
            continue

        if episode.title and not episode_item.title:
            episode_item.title = episode.title
            dirty = True
        if air_date and not episode_item.air_date:
            episode_item.air_date = air_date
            dirty = True
        if tmdb_id and not episode_item.tmdb_id:
            episode_item.tmdb_id = tmdb_id
            dirty = True
        if episode.still_url:
            existing_raw = episode_item.raw if isinstance(episode_item.raw, dict) else {}
            if "still_url" not in existing_raw:
                existing_raw["still_url"] = episode.still_url
                episode_item.raw = existing_raw
                dirty = True

    return dirty


async def backfill_show_episodes(
    db: AsyncSession,
    user_id: str,
    media_item: MediaItem,
) -> None:
    if media_item.media_type != "tv":
        return

    service = MetadataProviderService(db, user_id)
    provider = await service.load_provider("tmdb")
    if not provider or not isinstance(provider, EpisodeMetadataProvider):
        return

    media_dirty = False
    if not media_item.tmdb_id:
        if media_item.imdb_id and provider.capabilities.supports_external_id:
            try:
                candidates = await provider.find_by_external_id(media_item.imdb_id.lower(), "tv")
            except Exception as exc:
                logger.warning("TMDB external ID lookup failed for %s: %s", media_item.id, exc)
            else:
                candidate = next(
                    (
                        item
                        for item in candidates
                        if item.provider_id and item.media_type == "tv"
                    ),
                    None,
                )
                if candidate and candidate.provider_id:
                    media_item.tmdb_id = candidate.provider_id
                    if not media_item.title and candidate.title:
                        media_item.title = candidate.title
                    if media_item.year is None and candidate.year is not None:
                        media_item.year = candidate.year
                    if not media_item.poster_url and candidate.poster_url:
                        media_item.poster_url = candidate.poster_url
                    media_dirty = True
        if not media_item.tmdb_id:
            return

    try:
        seasons = await provider.list_seasons(media_item.tmdb_id)
    except Exception as exc:
        logger.warning("TMDB season lookup failed for %s: %s", media_item.id, exc)
        return

    episodes_dirty = False
    for season in seasons:
        try:
            episodes = await provider.list_episodes(media_item.tmdb_id, season.season_number)
        except Exception as exc:
            logger.warning(
                "TMDB episode lookup failed for %s season %s: %s",
                media_item.id,
                season.season_number,
                exc,
            )
            continue
        if await _persist_episode_list_for_media_item(
            db,
            media_item,
            "tmdb",
            media_item.tmdb_id,
            season.season_number,
            episodes,
        ):
            episodes_dirty = True

    if media_dirty or episodes_dirty:
        await db.commit()


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
