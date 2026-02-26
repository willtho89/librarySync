from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from librarysync.config import settings
from librarysync.connectors.services.publicmetadb import (
    PublicMetaDbClient,
    PublicMetaDbError,
    has_required_publicmetadb_fields,
)
from librarysync.core.import_schedule import parse_datetime
from librarysync.core.integrations import load_integration_with_secrets
from librarysync.core.publicmetadb import is_publicmetadb_sync_enabled
from librarysync.core.ratings import coerce_star_rating, normalize_ten_point_rating
from librarysync.db.models import EpisodeItem, Integration, MediaItem
from librarysync.jobs.import_base import ImportContext, ImportResult, ImportStrategy
from librarysync.jobs.import_pipeline import (
    BlacklistIds,
    ImportCandidate,
    ImportItems,
    process_import_candidates,
)
from librarysync.jobs.import_utils import chunked

LOOKBACK_DAYS = settings.history_lookback_days
ENTRY_KEY_BATCH_SIZE = 200
logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class PublicMetaDbMovie:
    title: str
    year: int | None
    tmdb_id: str
    imdb_id: str | None
    tvdb_id: str | None
    raw: dict[str, Any]


@dataclass(frozen=True)
class PublicMetaDbEpisode:
    show_title: str
    year: int | None
    show_tmdb_id: str
    show_imdb_id: str | None
    show_tvdb_id: str | None
    season_number: int
    episode_number: int
    episode_title: str | None
    raw: dict[str, Any]


@dataclass(frozen=True)
class PublicMetaDbWatchEntry:
    entry_key: str
    watched_at: datetime
    external_id: str | None
    rating: float | None
    raw: dict[str, Any]
    movie: PublicMetaDbMovie | None = None
    episode: PublicMetaDbEpisode | None = None


class PublicMetaDbImportStrategy(ImportStrategy):
    provider = "publicmetadb"

    def __init__(self, lookback_days: int = LOOKBACK_DAYS) -> None:
        self._lookback_days = lookback_days

    async def import_for_integration(
        self,
        context: ImportContext,
        integration: Integration,
        requested_at: datetime | None,
    ) -> ImportResult:
        return await _import_for_integration(
            context.db,
            integration,
            self._lookback_days,
            context.now,
        )


async def _import_for_integration(
    db: AsyncSession,
    integration: Integration,
    lookback_days: int,
    now: datetime,
) -> ImportResult:
    integration, secret_data = await load_integration_with_secrets(
        db, integration.user_id, "publicmetadb"
    )
    if not integration or not secret_data:
        return ImportResult(imported=0, attempted=False)
    if not has_required_publicmetadb_fields(secret_data):
        return ImportResult(imported=0, attempted=False)
    if not is_publicmetadb_sync_enabled(dict(integration.config or {})):
        return ImportResult(imported=0, attempted=False)

    api_key = _coerce_str(secret_data.get("api_key"))
    if not api_key:
        return ImportResult(imported=0, attempted=False)

    client = PublicMetaDbClient()
    try:
        payload, _response_code = await client.list_watched(api_key)
    except PublicMetaDbError as exc:
        logger.warning(
            "PublicMetaDB watched history fetch failed for user %s: %s",
            integration.user_id,
            exc,
        )
        return ImportResult(imported=0, attempted=True)

    watched_since = None if lookback_days < 0 else now - timedelta(days=lookback_days)
    entries: list[PublicMetaDbWatchEntry] = []
    for item in _extract_items(payload):
        entry = _build_watch_entry(item, now)
        if not entry:
            continue
        if watched_since and entry.watched_at < watched_since:
            continue
        entries.append(entry)

    if not entries:
        return ImportResult(imported=0, attempted=True)

    imported = 0
    for batch in chunked(entries, ENTRY_KEY_BATCH_SIZE):
        candidates = [candidate for entry in batch if (candidate := _build_candidate(entry))]
        if not candidates:
            continue
        imported += await process_import_candidates(
            db,
            integration.user_id,
            "publicmetadb",
            candidates,
            now=now,
        )

    if imported:
        logger.info(
            "Imported %s PublicMetaDB entries for user %s",
            imported,
            integration.user_id,
        )
    return ImportResult(imported=imported, attempted=True)


def _extract_items(payload: dict[str, Any]) -> list[dict[str, Any]]:
    items = payload.get("items")
    if not isinstance(items, list):
        return []
    return [item for item in items if isinstance(item, dict)]


def _build_watch_entry(
    item: dict[str, Any], default_watched_at: datetime
) -> PublicMetaDbWatchEntry | None:
    tmdb_id = _extract_tmdb_id(item)
    if not tmdb_id:
        return None
    media_type = _normalize_media_type(item)
    watched_at = _extract_watched_at(item) or default_watched_at
    external_id = _coerce_str(item.get("id") or item.get("watched_id"))
    rating = _extract_rating(item)

    if media_type == "movie":
        movie = PublicMetaDbMovie(
            title=_extract_movie_title(item, tmdb_id),
            year=_extract_year(item),
            tmdb_id=tmdb_id,
            imdb_id=_extract_imdb_id(item),
            tvdb_id=_extract_tvdb_id(item),
            raw=dict(item),
        )
        entry_key = _build_entry_key(external_id, tmdb_id, watched_at, None, None, "movie")
        return PublicMetaDbWatchEntry(
            entry_key=entry_key,
            watched_at=watched_at,
            external_id=external_id,
            rating=rating,
            raw=_build_event_raw(entry_key, external_id, watched_at, item),
            movie=movie,
        )

    season_number = _coerce_int(item.get("season"))
    episode_number = _coerce_int(item.get("episode"))
    if season_number is None or episode_number is None:
        return None
    episode = PublicMetaDbEpisode(
        show_title=_extract_show_title(item, tmdb_id),
        year=_extract_year(item),
        show_tmdb_id=tmdb_id,
        show_imdb_id=_extract_imdb_id(item),
        show_tvdb_id=_extract_tvdb_id(item),
        season_number=season_number,
        episode_number=episode_number,
        episode_title=_extract_episode_title(item),
        raw=dict(item),
    )
    entry_key = _build_entry_key(
        external_id,
        tmdb_id,
        watched_at,
        season_number,
        episode_number,
        "episode",
    )
    return PublicMetaDbWatchEntry(
        entry_key=entry_key,
        watched_at=watched_at,
        external_id=external_id,
        rating=rating,
        raw=_build_event_raw(entry_key, external_id, watched_at, item),
        episode=episode,
    )


def _build_candidate(entry: PublicMetaDbWatchEntry) -> ImportCandidate | None:
    if entry.movie:

        async def _build_items(db: AsyncSession) -> ImportItems:
            media_item = await _get_or_create_movie_item(db, entry.movie)
            return ImportItems(media_item=media_item, episode_item=None, show_item=None)

        return ImportCandidate(
            entry_key=entry.entry_key,
            watched_at=entry.watched_at,
            media_type="movie",
            raw=entry.raw,
            rating=entry.rating,
            external_id=entry.external_id,
            blacklist_ids=None,
            blacklist_enabled=False,
            is_rewatch=False,
            build_items=_build_items,
        )

    if entry.episode:

        async def _build_items(db: AsyncSession) -> ImportItems:
            show_item = await _get_or_create_show_item(db, entry.episode)
            if not show_item:
                return ImportItems(media_item=None, episode_item=None, show_item=None)
            episode_item = await _get_or_create_episode_item(db, show_item, entry.episode)
            return ImportItems(media_item=None, episode_item=episode_item, show_item=show_item)

        episode = entry.episode
        return ImportCandidate(
            entry_key=entry.entry_key,
            watched_at=entry.watched_at,
            media_type="episode",
            raw=entry.raw,
            rating=entry.rating,
            external_id=entry.external_id,
            blacklist_ids=BlacklistIds(
                imdb_id=episode.show_imdb_id,
                tmdb_id=episode.show_tmdb_id,
                tvdb_id=episode.show_tvdb_id,
                tvmaze_id=None,
            ),
            blacklist_enabled=True,
            is_rewatch=False,
            build_items=_build_items,
        )

    return None


async def _get_or_create_movie_item(
    db: AsyncSession, movie: PublicMetaDbMovie
) -> MediaItem | None:
    item = await _find_media_item(
        db,
        media_type="movie",
        tmdb_id=movie.tmdb_id,
        imdb_id=movie.imdb_id,
        tvdb_id=movie.tvdb_id,
    )
    if not item:
        item = MediaItem(
            media_type="movie",
            title=movie.title,
            year=movie.year,
            tmdb_id=movie.tmdb_id,
            imdb_id=movie.imdb_id,
            tvdb_id=movie.tvdb_id,
            raw={"publicmetadb": movie.raw},
        )
        db.add(item)
        await db.flush()
        return item
    _apply_media_updates(item, movie.title, movie.year, movie.tmdb_id, movie.imdb_id, movie.tvdb_id)
    _apply_media_raw(item, movie.raw)
    return item


async def _get_or_create_show_item(
    db: AsyncSession, episode: PublicMetaDbEpisode
) -> MediaItem | None:
    item = await _find_media_item(
        db,
        media_type="tv",
        tmdb_id=episode.show_tmdb_id,
        imdb_id=episode.show_imdb_id,
        tvdb_id=episode.show_tvdb_id,
    )
    if not item:
        item = MediaItem(
            media_type="tv",
            title=episode.show_title,
            year=episode.year,
            tmdb_id=episode.show_tmdb_id,
            imdb_id=episode.show_imdb_id,
            tvdb_id=episode.show_tvdb_id,
            raw={"publicmetadb": episode.raw},
        )
        db.add(item)
        await db.flush()
        return item
    _apply_media_updates(
        item,
        episode.show_title,
        episode.year,
        episode.show_tmdb_id,
        episode.show_imdb_id,
        episode.show_tvdb_id,
    )
    _apply_media_raw(item, episode.raw)
    return item


async def _get_or_create_episode_item(
    db: AsyncSession, show_item: MediaItem, episode: PublicMetaDbEpisode
) -> EpisodeItem:
    result = await db.execute(
        select(EpisodeItem).where(
            EpisodeItem.show_media_item_id == show_item.id,
            EpisodeItem.season_number == episode.season_number,
            EpisodeItem.episode_number == episode.episode_number,
        )
    )
    item = result.scalars().first()
    if not item:
        item = EpisodeItem(
            show_media_item_id=show_item.id,
            season_number=episode.season_number,
            episode_number=episode.episode_number,
            title=episode.episode_title,
            raw={"publicmetadb": episode.raw},
        )
        db.add(item)
        await db.flush()
        return item
    if episode.episode_title and not item.title:
        item.title = episode.episode_title
    _apply_episode_raw(item, episode.raw)
    return item


async def _find_media_item(
    db: AsyncSession,
    *,
    media_type: str,
    tmdb_id: str | None,
    imdb_id: str | None,
    tvdb_id: str | None,
) -> MediaItem | None:
    if imdb_id:
        result = await db.execute(select(MediaItem).where(MediaItem.imdb_id == imdb_id))
        item = result.scalars().first()
        if item:
            return item
    if tmdb_id:
        result = await db.execute(
            select(MediaItem).where(
                MediaItem.media_type == media_type,
                MediaItem.tmdb_id == tmdb_id,
            )
        )
        item = result.scalars().first()
        if item:
            return item
    if tvdb_id:
        result = await db.execute(
            select(MediaItem).where(
                MediaItem.media_type == media_type,
                MediaItem.tvdb_id == tvdb_id,
            )
        )
        item = result.scalars().first()
        if item:
            return item
    return None


def _apply_media_updates(
    item: MediaItem,
    title: str,
    year: int | None,
    tmdb_id: str | None,
    imdb_id: str | None,
    tvdb_id: str | None,
) -> None:
    if title and (not item.title or item.title.startswith("TMDB ")):
        item.title = title
    if year and not item.year:
        item.year = year
    if tmdb_id and not item.tmdb_id:
        item.tmdb_id = tmdb_id
    if imdb_id and not item.imdb_id:
        item.imdb_id = imdb_id
    if tvdb_id and not item.tvdb_id:
        item.tvdb_id = tvdb_id


def _apply_media_raw(item: MediaItem, payload: dict[str, Any]) -> None:
    raw = item.raw if isinstance(item.raw, dict) else {}
    raw["publicmetadb"] = payload
    item.raw = raw


def _apply_episode_raw(item: EpisodeItem, payload: dict[str, Any]) -> None:
    raw = item.raw if isinstance(item.raw, dict) else {}
    raw["publicmetadb"] = payload
    item.raw = raw


def _extract_tmdb_id(item: dict[str, Any]) -> str | None:
    direct = _coerce_str(item.get("tmdb_id") or item.get("tmdbId"))
    if direct:
        return direct
    ids = item.get("ids")
    if isinstance(ids, dict):
        nested = _coerce_str(ids.get("tmdb") or ids.get("tmdb_id"))
        if nested:
            return nested
    for key in ("movie_ids", "show_ids"):
        container = item.get(key)
        if not isinstance(container, dict):
            continue
        nested = _coerce_str(container.get("tmdb") or container.get("tmdb_id"))
        if nested:
            return nested
    return None


def _extract_imdb_id(item: dict[str, Any]) -> str | None:
    direct = _coerce_str(item.get("imdb_id") or item.get("imdb") or item.get("imdbId"))
    if direct:
        return direct
    ids = item.get("ids")
    if isinstance(ids, dict):
        return _coerce_str(ids.get("imdb") or ids.get("imdb_id"))
    return None


def _extract_tvdb_id(item: dict[str, Any]) -> str | None:
    direct = _coerce_str(item.get("tvdb_id") or item.get("tvdb") or item.get("tvdbId"))
    if direct:
        return direct
    ids = item.get("ids")
    if isinstance(ids, dict):
        return _coerce_str(ids.get("tvdb") or ids.get("tvdb_id"))
    return None


def _extract_watched_at(item: dict[str, Any]) -> datetime | None:
    for key in ("watched_at", "watchedAt", "created_at", "createdAt", "updated_at", "updatedAt"):
        value = item.get(key)
        if value is None:
            continue
        parsed = parse_datetime(value)
        if parsed:
            return parsed
        epoch = _coerce_float(value)
        if epoch is None:
            continue
        if epoch > 10_000_000_000:
            epoch = epoch / 1000.0
        if epoch <= 0:
            continue
        try:
            return datetime.fromtimestamp(epoch, tz=timezone.utc)
        except (OverflowError, OSError, ValueError):
            continue
    return None


def _extract_rating(item: dict[str, Any]) -> float | None:
    candidates: list[object] = []
    for key in ("rating", "stars", "score"):
        if key in item:
            candidates.append(item.get(key))
    rating_obj = item.get("rating")
    if isinstance(rating_obj, dict):
        for key in ("stars", "score", "value"):
            if key in rating_obj:
                candidates.append(rating_obj.get(key))
    for value in candidates:
        rating = coerce_star_rating(value)
        if rating is not None:
            return rating
        rating = normalize_ten_point_rating(value)
        if rating is not None:
            return rating
        numeric = _coerce_float(value)
        if numeric is None:
            continue
        if 0 <= numeric <= 100:
            rating = coerce_star_rating(numeric / 20.0)
            if rating is not None:
                return rating
    return None


def _extract_year(item: dict[str, Any]) -> int | None:
    for key in ("year", "release_year", "releaseYear"):
        year = _coerce_int(item.get(key))
        if year and year > 1800:
            return year
    return None


def _extract_movie_title(item: dict[str, Any], tmdb_id: str) -> str:
    title = _coerce_str(item.get("title") or item.get("movie_title") or item.get("name"))
    return title or f"TMDB Movie {tmdb_id}"


def _extract_show_title(item: dict[str, Any], tmdb_id: str) -> str:
    title = _coerce_str(item.get("show_title") or item.get("title") or item.get("name"))
    return title or f"TMDB TV {tmdb_id}"


def _extract_episode_title(item: dict[str, Any]) -> str | None:
    return _coerce_str(item.get("episode_title") or item.get("episode_name"))


def _normalize_media_type(item: dict[str, Any]) -> str:
    media_type = _coerce_str(item.get("media_type") or item.get("mediaType")) or "movie"
    if media_type.lower() in {"tv", "show", "series", "episode"}:
        return "episode"
    return "movie"


def _build_entry_key(
    external_id: str | None,
    tmdb_id: str,
    watched_at: datetime,
    season_number: int | None,
    episode_number: int | None,
    media_type: str,
) -> str:
    if external_id:
        return f"publicmetadb:{external_id}"
    timestamp = watched_at.astimezone(timezone.utc).isoformat()
    if media_type == "episode":
        return (
            f"publicmetadb:tv:{tmdb_id}:s{season_number or 0}"
            f"e{episode_number or 0}:{timestamp}"
        )
    return f"publicmetadb:movie:{tmdb_id}:{timestamp}"


def _build_event_raw(
    entry_key: str,
    external_id: str | None,
    watched_at: datetime,
    item: dict[str, Any],
) -> dict[str, Any]:
    return {
        "entry_key": entry_key,
        "external_id": external_id,
        "watched_at": watched_at.astimezone(timezone.utc).isoformat(),
        "item": item,
    }


def _coerce_int(value: object) -> int | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return None


def _coerce_float(value: object) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        return float(str(value).strip())
    except (TypeError, ValueError):
        return None


def _coerce_str(value: object) -> str | None:
    if value is None:
        return None
    cleaned = str(value).strip()
    return cleaned or None
