"""Refresh local episode calendars without importing anyone's watch history."""

import logging
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from librarysync.config import settings
from librarysync.connectors.services.simkl_calendar import CalendarEpisode, fetch_calendar
from librarysync.core.scheduler import claim_scheduled_job, complete_scheduled_job, fail_scheduled_job
from librarysync.db.models import EpisodeItem, MediaItem
from librarysync.db.session import SessionLocal, init_session_factory

logger = logging.getLogger(__name__)
CALENDAR_JOB = "simkl_calendar"
CALENDAR_INTERVAL = timedelta(hours=6)
CALENDAR_LEASE = timedelta(minutes=30)
CALENDAR_RETRY = timedelta(minutes=15)


async def process_simkl_calendar_once() -> int:
    client_id = settings.simkl_client_id
    if not client_id or client_id.startswith("your_"):
        return 0
    init_session_factory()
    async with SessionLocal() as db:
        job = await claim_scheduled_job(db, CALENDAR_JOB, CALENDAR_INTERVAL, CALENDAR_LEASE)
        if job is None:
            return 0
        try:
            updated = await refresh_simkl_calendar(db, client_id)
            await complete_scheduled_job(db, job, CALENDAR_INTERVAL)
        except Exception:
            logger.exception("SIMKL calendar refresh failed")
            await fail_scheduled_job(db, CALENDAR_JOB, CALENDAR_RETRY)
            return 0
    logger.info("SIMKL calendar refreshed %s local episodes", updated)
    return 1


def _show_keys(show: MediaItem):
    raw = show.raw if isinstance(show.raw, dict) else {}
    for key, value in (
        ("simkl", raw.get("simkl_id")),
        ("imdb", show.imdb_id),
        ("tmdb", show.tmdb_id),
        ("tvdb", show.tvdb_id),
    ):
        if value is not None:
            yield key, str(value).strip().lower()


async def refresh_simkl_calendar(db: AsyncSession, client_id: str, today: date | None = None) -> int:
    today = today or datetime.now(timezone.utc).date()
    first = today.replace(day=1)
    previous = first - timedelta(days=1)
    # Fetch all files before modifying local data. Archives cover recently aired
    # episodes on a fresh installation; the rolling file wins if a schedule changed.
    airings: list[CalendarEpisode] = []
    for catalog in ("tv", "anime"):
        for month in (previous, first, None):
            airings.extend(await fetch_calendar(client_id, catalog, month))

    shows = list((await db.scalars(select(MediaItem).where(MediaItem.media_type.in_(("tv", "anime"))))).all())
    identifiers: dict[tuple[str, str], set[str]] = defaultdict(set)
    for show in shows:
        for key in _show_keys(show):
            identifiers[key].add(show.id)
    matched: dict[str, list[CalendarEpisode]] = defaultdict(list)
    for airing in airings:
        candidates = set().union(*(identifiers.get(key, set()) for key in airing.show_ids.items()))
        if len(candidates) == 1:
            matched[next(iter(candidates))].append(airing)
    updated = 0
    for show_id, episodes in matched.items():
        existing = list((await db.scalars(select(EpisodeItem).where(EpisodeItem.show_media_item_id == show_id))).all())
        by_number = {(episode.season_number, episode.episode_number): episode for episode in existing}
        seasons = {episode.season_number for episode in existing if episode.season_number > 0}
        for airing in episodes:
            season = airing.season
            if season is None:
                # Anime uses absolute numbering. Only update an existing episode in
                # a single-season catalog; never guess a canonical season or create one.
                if seasons != {1} or (1, airing.number) not in by_number:
                    continue
                season = 1
            key = season, airing.number
            item = by_number.get(key)
            if item is None:
                item = EpisodeItem(show_media_item_id=show_id, season_number=season, episode_number=airing.number)
                try:
                    async with db.begin_nested():
                        db.add(item)
                        await db.flush()
                except IntegrityError:
                    # Another metadata worker can create this episode concurrently.
                    item = await db.scalar(
                        select(EpisodeItem).where(
                            EpisodeItem.show_media_item_id == show_id,
                            EpisodeItem.season_number == season,
                            EpisodeItem.episode_number == airing.number,
                        )
                    )
                    if item is None:
                        raise
                by_number[key] = item
            raw = dict(item.raw) if isinstance(item.raw, dict) else {}
            raw["simkl_calendar"] = {
                "simkl_id": airing.show_ids["simkl"],
                "date": airing.aired_at.isoformat(),
                "finale_type": airing.finale_type,
            }
            item.raw = raw
            item.air_date = airing.aired_at.date()
            if airing.title:
                item.title = airing.title
            updated += 1
    return updated
