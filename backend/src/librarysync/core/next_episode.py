"""Helpers for finding and marking the next unwatched episode of a show."""

from __future__ import annotations

from datetime import date, datetime, timezone
from typing import Iterable, Sequence

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from librarysync.core.watch_pipeline import enqueue_new_item_job
from librarysync.db.models import EpisodeItem, MediaItem, WatchedItem, WatchEvent

SHOW_MEDIA_TYPES = ("tv", "anime")


def select_next_episode(
    released_episodes: Iterable[EpisodeItem],
    watched_episode_ids: set[str],
) -> EpisodeItem | None:
    """Return the next episode after the user's latest watched point.

    ``released_episodes`` must already be ordered by season/episode number.
    This avoids recommending an earlier, previously-unwatched episode from an
    older season when the user has already progressed to a later season.
    """
    ordered_episodes = list(released_episodes)
    if not ordered_episodes:
        return None

    last_watched = None
    for episode in ordered_episodes:
        if episode.id in watched_episode_ids:
            last_watched = episode

    if last_watched is None:
        for episode in ordered_episodes:
            if episode.id not in watched_episode_ids:
                return episode
        return None

    last_key = (last_watched.season_number, last_watched.episode_number)
    for episode in ordered_episodes:
        episode_key = (episode.season_number, episode.episode_number)
        if episode_key <= last_key:
            continue
        if episode.id not in watched_episode_ids:
            return episode
    return None


def episode_to_payload(episode: EpisodeItem) -> dict:
    return {
        "episode_item_id": episode.id,
        "season_number": episode.season_number,
        "episode_number": episode.episode_number,
        "title": episode.title,
        "air_date": episode.air_date.isoformat() if episode.air_date else None,
    }


async def _released_episodes(
    db: AsyncSession,
    show_media_item_ids: Sequence[str],
    now_date: date,
) -> list[EpisodeItem]:
    result = await db.execute(
        select(EpisodeItem)
        .where(
            EpisodeItem.show_media_item_id.in_(show_media_item_ids),
            EpisodeItem.air_date.is_not(None),
            EpisodeItem.air_date <= now_date,
            EpisodeItem.season_number > 0,  # Exclude specials (season 0)
        )
        .order_by(
            EpisodeItem.show_media_item_id,
            EpisodeItem.season_number,
            EpisodeItem.episode_number,
        )
    )
    return list(result.scalars().all())


async def _watched_episode_ids(
    db: AsyncSession,
    user_id: str,
    show_media_item_ids: Sequence[str],
    now_date: date,
) -> set[str]:
    result = await db.execute(
        select(WatchedItem.episode_item_id)
        .join(EpisodeItem, WatchedItem.episode_item_id == EpisodeItem.id)
        .where(
            WatchedItem.user_id == user_id,
            WatchedItem.media_item_id.is_(None),
            EpisodeItem.show_media_item_id.in_(show_media_item_ids),
            EpisodeItem.air_date.is_not(None),
            EpisodeItem.air_date <= now_date,
            EpisodeItem.season_number > 0,
        )
    )
    return set(result.scalars().all())


async def find_next_episode(
    db: AsyncSession,
    user_id: str,
    show_media_item_id: str,
    now_date: date | None = None,
) -> EpisodeItem | None:
    """Return the first released, unwatched episode for a show, if any."""
    next_episodes = await find_next_episodes_bulk(db, user_id, [show_media_item_id], now_date)
    return next_episodes.get(show_media_item_id)


async def find_next_episodes_bulk(
    db: AsyncSession,
    user_id: str,
    show_media_item_ids: Sequence[str],
    now_date: date | None = None,
) -> dict[str, EpisodeItem]:
    """Return the first released, unwatched episode per show."""
    show_ids = list(dict.fromkeys(show_media_item_ids))
    if not show_ids:
        return {}
    now_date = now_date or datetime.now(timezone.utc).date()
    released = await _released_episodes(db, show_ids, now_date)
    watched_ids = await _watched_episode_ids(db, user_id, show_ids, now_date)
    next_episodes: dict[str, EpisodeItem] = {}
    per_show_episodes: dict[str, list[EpisodeItem]] = {}
    for episode in released:
        per_show_episodes.setdefault(episode.show_media_item_id, []).append(episode)

    for show_id, episodes in per_show_episodes.items():
        next_episode = select_next_episode(episodes, watched_ids)
        if next_episode is not None:
            next_episodes[show_id] = next_episode
    return next_episodes


async def has_released_episodes(
    db: AsyncSession,
    show_media_item_id: str,
    now_date: date | None = None,
) -> bool:
    now_date = now_date or datetime.now(timezone.utc).date()
    result = await db.execute(
        select(EpisodeItem.id)
        .where(
            EpisodeItem.show_media_item_id == show_media_item_id,
            EpisodeItem.air_date.is_not(None),
            EpisodeItem.air_date <= now_date,
            EpisodeItem.season_number > 0,
        )
        .limit(1)
    )
    return result.scalars().first() is not None


async def mark_next_episode_watched(
    db: AsyncSession,
    user_id: str,
    media_item: MediaItem,
    *,
    source: str = "manual",
    watched_at: datetime | None = None,
    event_raw_extra: dict | None = None,
) -> tuple[WatchedItem, EpisodeItem]:
    """Record a watched entry for the next released, unwatched episode of a show.

    Raises ValueError when the media item is not a show or no released,
    unwatched episode exists. Does not commit; the caller owns the transaction.
    """
    if media_item.media_type not in SHOW_MEDIA_TYPES:
        raise ValueError("Next episode tracking is only available for shows")
    next_episode = await find_next_episode(db, user_id, media_item.id)
    if not next_episode:
        raise ValueError("No released, unwatched episode found for this show")
    watched_at = watched_at or datetime.now(timezone.utc)
    watched = WatchedItem(
        user_id=user_id,
        media_item_id=None,
        episode_item_id=next_episode.id,
        watched_at=watched_at,
        source=source,
    )
    event_raw: dict = {
        "source": source,
        "episode": {
            "season_number": next_episode.season_number,
            "episode_number": next_episode.episode_number,
            "title": next_episode.title,
            "id": next_episode.id,
        },
    }
    if event_raw_extra:
        event_raw.update(event_raw_extra)
    event = WatchEvent(
        user_id=user_id,
        media_item_id=None,
        episode_item_id=next_episode.id,
        event_type="manual_watched",
        occurred_at=watched_at,
        raw=event_raw,
    )
    db.add_all([watched, event])
    await db.flush()
    await enqueue_new_item_job(db, user_id, watched.id, is_rewatch=False, source=source)
    return watched, next_episode
