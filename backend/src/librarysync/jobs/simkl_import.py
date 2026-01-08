from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from librarysync.config import settings
from librarysync.connectors.services.simkl import (
    SimklClient,
    SimklError,
    has_required_simkl_fields,
    is_token_expired,
    parse_expires_at,
    token_to_secret_payload,
)
from librarysync.core.import_schedule import parse_datetime
from librarysync.core.integrations import load_integration_with_secrets
from librarysync.core.ratings import normalize_ten_point_rating
from librarysync.core.security import encrypt_value
from librarysync.db.models import (
    EpisodeItem,
    Integration,
    IntegrationSecret,
    MediaItem,
    WatchEvent,
)
from librarysync.jobs.import_base import ImportContext, ImportResult, ImportStrategy
from librarysync.jobs.import_pipeline import (
    BlacklistIds,
    ImportCandidate,
    ImportItems,
    process_import_candidates,
)
from librarysync.jobs.import_utils import chunked

LOOKBACK_DAYS = settings.history_lookback_days
logger = logging.getLogger(__name__)
SIMKL_ACTIVITY_KEYS = {
    "movies": "movies",
    "shows": "tv_shows",
    "anime": "anime",
}
ENTRY_KEY_BATCH_SIZE = 200


@dataclass(frozen=True)
class MovieSummary:
    title: str
    year: int | None
    imdb_id: str | None
    tmdb_id: str | None
    simkl_id: str | None
    raw: dict[str, Any]


@dataclass(frozen=True)
class ShowSummary:
    title: str
    year: int | None
    imdb_id: str | None
    tmdb_id: str | None
    tvdb_id: str | None
    simkl_id: str | None
    raw: dict[str, Any]


@dataclass(frozen=True)
class EpisodeSummary:
    season_number: int
    episode_number: int
    title: str | None
    imdb_id: str | None
    tmdb_id: str | None
    tvdb_id: str | None
    simkl_id: str | None
    raw: dict[str, Any]


class SimklImportStrategy(ImportStrategy):
    provider = "simkl"

    def __init__(
        self,
        lookback_days: int = LOOKBACK_DAYS,
    ) -> None:
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
    if not settings.simkl_client_id or not settings.simkl_client_secret:
        return ImportResult(imported=0, attempted=False)
    integration, secret_data = await load_integration_with_secrets(
        db, integration.user_id, "simkl"
    )
    if not integration or not secret_data:
        return ImportResult(imported=0, attempted=False)
    if not has_required_simkl_fields(secret_data):
        return ImportResult(imported=0, attempted=False)
    client = SimklClient(
        client_id=settings.simkl_client_id,
        client_secret=settings.simkl_client_secret,
    )
    try:
        access_token = await _ensure_simkl_access_token(
            db, integration.id, secret_data, client
        )
    except SimklError as exc:
        logger.warning(
            "SIMKL token refresh failed for user %s: %s",
            integration.user_id,
            exc,
        )
        return ImportResult(imported=0, attempted=True)

    activities = await _fetch_simkl_activities(
        client, access_token, integration.user_id
    )
    has_history = await _has_simkl_import_history(db, integration.user_id)
    date_from, initial_sync = _select_date_from(
        integration.config, activities, now, lookback_days, has_history
    )
    if initial_sync:
        logger.info(
            "SIMKL import using initial lookback window starting %s for user %s",
            date_from.isoformat() if date_from else "None",
            integration.user_id,
        )
    else:
        logger.info(
            "SIMKL import using date_from=%s for user %s",
            date_from.isoformat() if date_from else "None",
            integration.user_id,
        )
    imported = 0

    categories = _select_simkl_categories(integration.config, activities, has_history)
    if not categories:
        _update_simkl_activity_config(integration, activities, now)
        await db.commit()
        return ImportResult(imported=0, attempted=True)

    history_payload: dict[str, dict[str, Any]] = {}
    if "movies" in categories:
        history_payload["movies"] = await client.fetch_all_items(
            access_token,
            category="movies",
            date_from=date_from,
        )
        imported += await _import_movies_payload(
            db, integration.user_id, history_payload.get("movies", {}), date_from, now
        )

    if "shows" in categories:
        history_payload["shows"] = await client.fetch_all_items(
            access_token,
            category="shows",
            date_from=date_from,
            extended="full",
            episode_watched_at=True,
        )
        imported += await _import_shows_payload(
            db,
            integration.user_id,
            history_payload.get("shows", {}),
            date_from,
            label="shows",
            now=now,
        )

    if "anime" in categories:
        history_payload["anime"] = await client.fetch_all_items(
            access_token,
            category="anime",
            date_from=date_from,
            extended="full",
            episode_watched_at=True,
        )
        imported += await _import_shows_payload(
            db,
            integration.user_id,
            history_payload.get("anime", {}),
            date_from,
            label="anime",
            now=now,
        )

    _update_simkl_activity_config(integration, activities, now)
    await db.commit()

    if imported:
        logger.info(
            "Imported %s SIMKL entries for user %s",
            imported,
            integration.user_id,
        )
    return ImportResult(imported=imported, attempted=True)


async def _fetch_simkl_activities(
    client: SimklClient, access_token: str, user_id: str
) -> dict[str, Any]:
    try:
        return await client.fetch_activities(access_token)
    except SimklError as exc:
        logger.warning("SIMKL activities fetch failed for user %s: %s", user_id, exc)
        return {}


def _select_date_from(
    config: dict | None,
    activities: dict[str, Any] | None,
    now: datetime,
    lookback_days: int,
    has_history: bool,
) -> tuple[datetime | None, bool]:
    config = config or {}
    full_history = lookback_days < 0
    if not has_history:
        if full_history:
            return None, True
        return now - timedelta(days=lookback_days), True
    last_activity = parse_datetime(config.get("simkl_activity_all"))
    if last_activity:
        return last_activity, False
    activity_all = _extract_activity_timestamp(activities)
    if full_history:
        if activity_all:
            return activity_all, True
        return None, True
    fallback = now - timedelta(days=lookback_days)
    if activity_all and activity_all < fallback:
        return activity_all, True
    return fallback, True


def _extract_activity_timestamp(payload: dict[str, Any] | None) -> datetime | None:
    if not isinstance(payload, dict):
        return None
    parsed = parse_datetime(payload.get("all"))
    if parsed:
        return parsed
    for key in ("movies", "tv_shows", "anime"):
        block = payload.get(key)
        if isinstance(block, dict):
            parsed = parse_datetime(block.get("all"))
            if parsed:
                return parsed
    return None


def _extract_activity_block_timestamp(
    payload: dict[str, Any] | None, key: str
) -> datetime | None:
    if not isinstance(payload, dict):
        return None
    block = payload.get(key)
    if not isinstance(block, dict):
        return None
    for candidate in ("all", key, key.rstrip("s")):
        parsed = parse_datetime(block.get(candidate))
        if parsed:
            return parsed
    return None


def _select_simkl_categories(
    config: dict | None,
    activities: dict[str, Any] | None,
    has_history: bool,
) -> list[str]:
    if not has_history:
        return list(SIMKL_ACTIVITY_KEYS.keys())
    if not isinstance(activities, dict):
        return list(SIMKL_ACTIVITY_KEYS.keys())

    last_activity = parse_datetime((config or {}).get("simkl_activity_all"))
    activity_all = _extract_activity_timestamp(activities)
    if activity_all and last_activity and activity_all <= last_activity:
        return []

    selected: list[str] = []
    for category, activity_key in SIMKL_ACTIVITY_KEYS.items():
        activity_time = _extract_activity_block_timestamp(activities, activity_key)
        last_time = parse_datetime((config or {}).get(f"simkl_activity_{category}"))
        if activity_time and last_time and activity_time <= last_time:
            continue
        selected.append(category)
    return selected if selected else list(SIMKL_ACTIVITY_KEYS.keys())


def _update_simkl_activity_config(
    integration: Integration,
    activities: dict[str, Any] | None,
    now: datetime,
) -> None:
    if not isinstance(activities, dict):
        return
    config = dict(integration.config or {})
    activity_all = _extract_activity_timestamp(activities)
    if activity_all:
        config["simkl_activity_all"] = activity_all.isoformat()
    for category, activity_key in SIMKL_ACTIVITY_KEYS.items():
        activity_time = _extract_activity_block_timestamp(activities, activity_key)
        if activity_time:
            config[f"simkl_activity_{category}"] = activity_time.isoformat()
    integration.config = config
    integration.updated_at = now
    # Caller is responsible for committing.


async def _has_simkl_import_history(db: AsyncSession, user_id: str) -> bool:
    result = await db.execute(
        select(WatchEvent.id).where(
            WatchEvent.user_id == user_id,
            WatchEvent.event_type == "simkl_imported",
        )
    )
    return result.scalars().first() is not None


async def _import_movies_payload(
    db: AsyncSession,
    user_id: str,
    payload: dict[str, Any],
    date_from: datetime | None,
    now: datetime,
) -> int:
    entries = _extract_all_items_entries(payload, "movies", {"completed"})
    logger.info(
        "SIMKL all-items extracted %s movie entries for user %s",
        len(entries),
        user_id,
    )
    if not entries:
        _log_empty_all_items_payload("movies", payload)
        return 0
    imported = 0
    for batch in chunked(entries, ENTRY_KEY_BATCH_SIZE):
        candidates: list[ImportCandidate] = []
        for entry in batch:
            watched_at = _extract_item_watched_at(entry) or now
            if date_from and watched_at < date_from:
                continue
            candidate = _build_movie_candidate(entry, watched_at, now)
            if candidate:
                candidates.append(candidate)
        imported += await process_import_candidates(
            db,
            user_id,
            "simkl",
            candidates,
            now=now,
        )
    return imported


async def _import_shows_payload(
    db: AsyncSession,
    user_id: str,
    payload: dict[str, Any],
    date_from: datetime | None,
    label: str,
    now: datetime,
) -> int:
    raw_type = "anime" if label == "anime" else "show"
    entries = _extract_all_items_entries(payload, label, {"completed", "watching"})
    logger.info(
        "SIMKL all-items extracted %s %s entries for user %s",
        len(entries),
        label,
        user_id,
    )
    if not entries:
        _log_empty_all_items_payload(label, payload)
        return 0
    imported = 0
    for batch in chunked(entries, ENTRY_KEY_BATCH_SIZE):
        candidates: list[ImportCandidate] = []
        for entry in batch:
            show_payload = _extract_show_payload(entry)
            episodes = _extract_episode_entries(entry)
            if not episodes:
                watched_at = _extract_item_watched_at(entry)
                if not watched_at:
                    continue
                normalized = dict(entry)
                normalized["show"] = show_payload or entry
                candidate = _build_show_candidate(normalized, watched_at, raw_type, now)
                if candidate:
                    candidates.append(candidate)
                continue
            for episode in episodes:
                normalized = dict(entry)
                normalized["show"] = show_payload or entry
                normalized["episode"] = episode
                watched_at = _extract_item_watched_at(episode)
                if not watched_at:
                    watched_at = _extract_item_watched_at(entry)
                if not watched_at:
                    watched_at = now
                if date_from and watched_at < date_from:
                    continue
                candidate = _build_episode_candidate(normalized, watched_at, raw_type, now)
                if candidate:
                    candidates.append(candidate)
        imported += await process_import_candidates(
            db,
            user_id,
            "simkl",
            candidates,
            now=now,
        )
    return imported


def _extract_all_items_entries(
    payload: dict[str, Any], key: str, statuses: set[str] | None = None
) -> list[dict[str, Any]]:
    if not isinstance(payload, dict):
        return []
    container = payload.get(key)
    if container is None and statuses and any(status in payload for status in statuses):
        container = payload
    entries: list[dict[str, Any]] = []
    if isinstance(container, dict):
        has_status_keys = bool(statuses) and any(
            status in container for status in statuses or set()
        )
        if statuses:
            for status in statuses:
                entries.extend(_extract_entries_from_container(container.get(status)))
            if entries:
                return entries
            entries.extend(_extract_entries_from_container(container.get("all")))
            if entries or has_status_keys:
                return entries
        for value in container.values():
            entries.extend(_extract_entries_from_container(value))
        return entries
    if isinstance(container, list):
        entries = _coerce_entry_list(container)
        if statuses:
            known = [entry for entry in entries if _entry_status(entry)]
            if known:
                return [entry for entry in entries if _entry_status(entry) in statuses]
        return entries
    return []


def _entry_status(entry: dict[str, Any]) -> str | None:
    for key in ("status", "list", "to"):
        value = entry.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip().lower()
    return None


def _extract_entries_from_container(value: object) -> list[dict[str, Any]]:
    if isinstance(value, list):
        return [item for item in value if isinstance(item, dict)]
    if isinstance(value, dict):
        if _looks_like_payload(value) or any(
            key in value for key in ("movie", "show", "item", "episode", "anime")
        ):
            return [value]
        entries: list[dict[str, Any]] = []
        for key in (
            "items",
            "movies",
            "shows",
            "anime",
            "entries",
            "list",
            "history",
            "records",
        ):
            entries.extend(_extract_entries_from_container(value.get(key)))
        if entries:
            return entries
        for nested in value.values():
            entries.extend(_extract_entries_from_container(nested))
        return entries
    return []


def _coerce_entry_list(value: object) -> list[dict[str, Any]]:
    if isinstance(value, list):
        return [item for item in value if isinstance(item, dict)]
    if isinstance(value, dict):
        return [value]
    return []


def _extract_show_payload(entry: dict[str, Any]) -> dict[str, Any] | None:
    for key in ("show", "anime", "item"):
        payload = entry.get(key)
        if isinstance(payload, dict):
            return payload
    if _looks_like_payload(entry):
        return entry
    return None


def _extract_episode_entries(entry: dict[str, Any]) -> list[dict[str, Any]]:
    episode_season = _coerce_int(
        entry.get("season") or entry.get("season_number") or entry.get("number")
    )
    episodes = _coerce_episode_list(entry.get("episodes"), episode_season)
    if episodes:
        return episodes
    seasons = entry.get("seasons")
    results: list[dict[str, Any]] = []
    if isinstance(seasons, dict):
        for season_key, season in seasons.items():
            season_number = _extract_season_number(season, season_key)
            results.extend(
                _coerce_episode_list(_extract_season_episodes(season), season_number)
            )
    elif isinstance(seasons, list):
        for season in seasons:
            if not isinstance(season, dict):
                continue
            season_number = _extract_season_number(season, None)
            results.extend(
                _coerce_episode_list(_extract_season_episodes(season), season_number)
            )
    if results:
        return results
    last_watched = _extract_last_watched_label(entry)
    if last_watched:
        parsed = _parse_last_watched_label(last_watched)
        if parsed:
            season, episode = parsed
            return [{"season": season, "episode": episode}]
    return results


def _extract_last_watched_label(entry: dict[str, Any]) -> str | None:
    value = entry.get("last_watched")
    if isinstance(value, str) and value.strip():
        return value
    for key in ("show", "item", "anime"):
        nested = entry.get(key)
        if isinstance(nested, dict):
            value = nested.get("last_watched")
            if isinstance(value, str) and value.strip():
                return value
    return None


def _parse_last_watched_label(value: str) -> tuple[int, int] | None:
    cleaned = value.strip().upper()
    if not cleaned:
        return None
    match = re.match(r"^S(?P<season>\d+)E(?P<episode>\d+)$", cleaned)
    if match:
        return int(match.group("season")), int(match.group("episode"))
    match = re.match(r"^(?P<season>\d+)X(?P<episode>\d+)$", cleaned)
    if match:
        return int(match.group("season")), int(match.group("episode"))
    match = re.match(r"^E(?P<episode>\d+)$", cleaned)
    if match:
        return 1, int(match.group("episode"))
    return None


def _extract_season_number(payload: object, key: object) -> int | None:
    if isinstance(payload, dict):
        return (
            _coerce_int(
                payload.get("season")
                or payload.get("season_number")
                or payload.get("number")
            )
            or _coerce_int(key)
        )
    return _coerce_int(key)


def _extract_season_episodes(payload: dict[str, Any] | None) -> object:
    if isinstance(payload, list):
        return payload
    if not isinstance(payload, dict):
        return None
    return (
        payload.get("episodes")
        or payload.get("items")
        or payload.get("list")
        or payload.get("episode")
    )


def _coerce_episode_list(
    value: object, season_number: int | None
) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    if isinstance(value, dict):
        for key, episode in value.items():
            episode_number = _coerce_int(key)
            normalized = _normalize_episode_payload(
                episode, episode_number, season_number
            )
            if normalized:
                results.append(normalized)
        return results
    if isinstance(value, list):
        for episode in value:
            normalized = _normalize_episode_payload(episode, None, season_number)
            if normalized:
                results.append(normalized)
        return results
    normalized = _normalize_episode_payload(value, None, season_number)
    if normalized:
        results.append(normalized)
    return results


def _normalize_episode_payload(
    payload: object, episode_number: int | None, season_number: int | None
) -> dict[str, Any] | None:
    if isinstance(payload, dict):
        normalized = dict(payload)
    elif isinstance(payload, (int, str)):
        normalized = {"episode": payload}
    else:
        return None
    if (
        episode_number is not None
        and "episode" not in normalized
        and "number" not in normalized
    ):
        normalized["episode"] = episode_number
    if (
        season_number is not None
        and "season" not in normalized
        and "season_number" not in normalized
    ):
        normalized["season"] = season_number
    return normalized


def _extract_item_watched_at(entry: dict[str, Any]) -> datetime | None:
    for key in (
        "watched_at",
        "last_watched_at",
        "completed_at",
        "last_watched",
        "watched",
    ):
        parsed = _parse_datetime(entry.get(key))
        if parsed:
            return parsed
    for nested_key in ("movie", "show", "item", "episode", "anime"):
        nested = entry.get(nested_key)
        if not isinstance(nested, dict):
            continue
        for key in (
            "watched_at",
            "last_watched_at",
            "completed_at",
            "last_watched",
            "watched",
        ):
            parsed = _parse_datetime(nested.get(key))
            if parsed:
                return parsed
    return None


def _log_empty_all_items_payload(label: str, payload: dict[str, Any]) -> None:
    summary = _summarize_payload(payload)
    if summary:
        logger.info("SIMKL all-items payload empty for %s: %s", label, summary)


def _build_movie_candidate(
    entry: dict[str, Any],
    watched_at: datetime | None,
    default_watched_at: datetime,
) -> ImportCandidate | None:
    history_id = _extract_history_id(entry)
    watched_at = watched_at or default_watched_at
    movie = _extract_movie_summary(entry)
    if not movie:
        return None
    rating = _extract_entry_rating(entry, include_show_rating=True)
    entry_key = _build_entry_key(
        history_id,
        movie.imdb_id,
        movie.tmdb_id,
        movie.simkl_id,
        watched_at,
        "movie",
    )
    if not entry_key:
        return None

    async def _build_items(db: AsyncSession) -> ImportItems:
        media_item = await _get_or_create_movie_item(db, movie)
        return ImportItems(media_item=media_item, episode_item=None, show_item=None)

    return ImportCandidate(
        entry_key=entry_key,
        watched_at=watched_at,
        media_type="movie",
        raw=_build_event_raw(entry_key, history_id, watched_at, movie, None, rating),
        rating=rating,
        external_id=history_id,
        blacklist_ids=None,
        blacklist_enabled=False,
        is_rewatch=False,
        build_items=_build_items,
    )


def _build_show_candidate(
    entry: dict[str, Any],
    watched_at: datetime | None,
    raw_type: str,
    default_watched_at: datetime,
) -> ImportCandidate | None:
    history_id = _extract_history_id(entry)
    watched_at = watched_at or default_watched_at
    show = _extract_show_summary(entry)
    if not show:
        return None
    rating = _extract_entry_rating(entry, include_show_rating=True)
    entry_key = _build_entry_key(
        history_id,
        show.imdb_id,
        show.tmdb_id,
        show.simkl_id,
        watched_at,
        "show",
    )
    if not entry_key:
        return None

    async def _build_items(db: AsyncSession) -> ImportItems:
        media_item = await _get_or_create_show_item(db, show, raw_type)
        return ImportItems(media_item=media_item, episode_item=None, show_item=media_item)

    blacklist_enabled = raw_type != "anime"
    return ImportCandidate(
        entry_key=entry_key,
        watched_at=watched_at,
        media_type="show",
        raw=_build_event_raw(entry_key, history_id, watched_at, show, None, rating),
        rating=rating,
        external_id=history_id,
        blacklist_ids=BlacklistIds(
            imdb_id=show.imdb_id,
            tmdb_id=show.tmdb_id,
            tvdb_id=show.tvdb_id,
            tvmaze_id=None,
        ),
        blacklist_enabled=blacklist_enabled,
        is_rewatch=False,
        build_items=_build_items,
    )


def _build_episode_candidate(
    entry: dict[str, Any],
    watched_at: datetime | None,
    raw_type: str,
    default_watched_at: datetime,
) -> ImportCandidate | None:
    history_id = _extract_history_id(entry)
    watched_at = watched_at or default_watched_at
    show = _extract_show_summary(entry)
    episode = _extract_episode_summary(entry)
    if not show or not episode:
        return None
    rating = _extract_entry_rating(entry, include_show_rating=False)
    entry_key = _build_episode_entry_key(history_id, show, episode, watched_at)
    if not entry_key:
        return None

    async def _build_items(db: AsyncSession) -> ImportItems:
        show_item = await _get_or_create_show_item(db, show, raw_type)
        if not show_item:
            return ImportItems(media_item=None, episode_item=None, show_item=None)
        episode_item = await _get_or_create_episode_item(db, show_item, episode)
        return ImportItems(
            media_item=None,
            episode_item=episode_item,
            show_item=show_item,
        )

    blacklist_enabled = raw_type != "anime"
    return ImportCandidate(
        entry_key=entry_key,
        watched_at=watched_at,
        media_type="episode",
        raw=_build_event_raw(entry_key, history_id, watched_at, show, episode, rating),
        rating=rating,
        external_id=history_id,
        blacklist_ids=BlacklistIds(
            imdb_id=show.imdb_id,
            tmdb_id=show.tmdb_id,
            tvdb_id=show.tvdb_id,
            tvmaze_id=None,
        ),
        blacklist_enabled=blacklist_enabled,
        is_rewatch=False,
        build_items=_build_items,
    )


async def _get_or_create_movie_item(
    db: AsyncSession, movie: MovieSummary
) -> MediaItem | None:
    item = await _find_media_item(
        db, movie.imdb_id, movie.tmdb_id, movie.simkl_id, "movie"
    )
    if item:
        _apply_movie_updates(item, movie)
        return item
    if not movie.imdb_id and not movie.tmdb_id and not movie.simkl_id:
        return None
    item = MediaItem(
        media_type="movie",
        title=movie.title,
        year=movie.year,
        imdb_id=movie.imdb_id,
        tmdb_id=movie.tmdb_id,
        poster_url=None,
        raw=_build_media_raw(movie.simkl_id, movie.raw, "movie"),
    )
    db.add(item)
    await db.flush()
    return item


async def _get_or_create_show_item(
    db: AsyncSession, show: ShowSummary, raw_type: str = "show"
) -> MediaItem | None:
    item = await _find_media_item(
        db, show.imdb_id, show.tmdb_id, show.simkl_id, "tv"
    )
    if item:
        _apply_show_updates(item, show, raw_type)
        return item
    if not show.imdb_id and not show.tmdb_id and not show.simkl_id and not show.tvdb_id:
        return None
    item = MediaItem(
        media_type="tv",
        title=show.title,
        year=show.year,
        imdb_id=show.imdb_id,
        tmdb_id=show.tmdb_id,
        tvdb_id=show.tvdb_id,
        poster_url=None,
        raw=_build_media_raw(show.simkl_id, show.raw, raw_type),
    )
    db.add(item)
    await db.flush()
    return item


async def _get_or_create_episode_item(
    db: AsyncSession, show_item: MediaItem, episode: EpisodeSummary
) -> EpisodeItem | None:
    item = await _find_episode_item(db, episode, show_item.id)
    if item:
        _apply_episode_updates(item, episode)
        return item
    item = EpisodeItem(
        show_media_item_id=show_item.id,
        season_number=episode.season_number,
        episode_number=episode.episode_number,
        title=episode.title,
        tmdb_id=episode.tmdb_id,
        tvdb_id=episode.tvdb_id,
        imdb_id=episode.imdb_id,
        raw=_build_episode_raw(episode.simkl_id, episode.raw),
    )
    db.add(item)
    await db.flush()
    return item


async def _find_media_item(
    db: AsyncSession,
    imdb_id: str | None,
    tmdb_id: str | None,
    simkl_id: str | None,
    media_type: str,
) -> MediaItem | None:
    item: MediaItem | None = None
    if imdb_id:
        result = await db.execute(
            select(MediaItem).where(MediaItem.imdb_id == imdb_id)
        )
        item = result.scalars().first()
    if tmdb_id:
        result = await db.execute(
            select(MediaItem).where(
                MediaItem.tmdb_id == tmdb_id, MediaItem.media_type == media_type
            )
        )
        tmdb_item = result.scalars().first()
        if item and tmdb_item and item.id != tmdb_item.id:
            return item
        if not item:
            item = tmdb_item
    if not item and simkl_id:
        result = await db.execute(
            select(MediaItem).where(
                MediaItem.media_type == media_type,
                MediaItem.raw["simkl_id"].as_string() == simkl_id,
            )
        )
        item = result.scalars().first()
    return item


async def _find_episode_item(
    db: AsyncSession, episode: EpisodeSummary, show_media_item_id: str | None = None
) -> EpisodeItem | None:
    item: EpisodeItem | None = None
    if episode.imdb_id:
        result = await db.execute(
            select(EpisodeItem).where(EpisodeItem.imdb_id == episode.imdb_id)
        )
        item = result.scalars().first()
    if episode.tmdb_id:
        result = await db.execute(
            select(EpisodeItem).where(EpisodeItem.tmdb_id == episode.tmdb_id)
        )
        tmdb_item = result.scalars().first()
        if item and tmdb_item and item.id != tmdb_item.id:
            return item
        if not item:
            item = tmdb_item
    if episode.tvdb_id:
        result = await db.execute(
            select(EpisodeItem).where(EpisodeItem.tvdb_id == episode.tvdb_id)
        )
        tvdb_item = result.scalars().first()
        if item and tvdb_item and item.id != tvdb_item.id:
            return item
        if not item:
            item = tvdb_item
    if not item and episode.simkl_id:
        result = await db.execute(
            select(EpisodeItem).where(
                EpisodeItem.raw["simkl_id"].as_string() == episode.simkl_id
            )
        )
        item = result.scalars().first()
    if not item and show_media_item_id:
        result = await db.execute(
            select(EpisodeItem).where(
                EpisodeItem.show_media_item_id == show_media_item_id,
                EpisodeItem.season_number == episode.season_number,
                EpisodeItem.episode_number == episode.episode_number,
            )
        )
        item = result.scalars().first()
    return item


def _apply_movie_updates(item: MediaItem, movie: MovieSummary) -> None:
    if movie.imdb_id and not item.imdb_id:
        item.imdb_id = movie.imdb_id
    if movie.tmdb_id and not item.tmdb_id:
        item.tmdb_id = movie.tmdb_id
    if movie.year is not None and item.year is None:
        item.year = movie.year
    if movie.title and item.title.startswith("SIMKL movie"):
        item.title = movie.title
    item.raw = _merge_media_raw(item.raw, movie.simkl_id, movie.raw, "movie")


def _apply_show_updates(item: MediaItem, show: ShowSummary, raw_type: str) -> None:
    if show.imdb_id and not item.imdb_id:
        item.imdb_id = show.imdb_id
    if show.tmdb_id and not item.tmdb_id:
        item.tmdb_id = show.tmdb_id
    if show.tvdb_id and not item.tvdb_id:
        item.tvdb_id = show.tvdb_id
    if show.year is not None and item.year is None:
        item.year = show.year
    if show.title and item.title.startswith("SIMKL show"):
        item.title = show.title
    item.raw = _merge_media_raw(item.raw, show.simkl_id, show.raw, raw_type)


def _apply_episode_updates(item: EpisodeItem, episode: EpisodeSummary) -> None:
    if episode.imdb_id and not item.imdb_id:
        item.imdb_id = episode.imdb_id
    if episode.tmdb_id and not item.tmdb_id:
        item.tmdb_id = episode.tmdb_id
    if episode.tvdb_id and not item.tvdb_id:
        item.tvdb_id = episode.tvdb_id
    if episode.title and not item.title:
        item.title = episode.title
    item.raw = _merge_episode_raw(item.raw, episode.simkl_id, episode.raw)


def _build_media_raw(
    simkl_id: str | None, raw_payload: dict[str, Any], raw_type: str
) -> dict[str, Any]:
    raw = {"source": "simkl", "type": raw_type}
    if simkl_id:
        raw["simkl_id"] = simkl_id
    if raw_payload:
        raw["simkl"] = raw_payload
    return raw


def _merge_media_raw(
    existing: dict | None,
    simkl_id: str | None,
    raw_payload: dict[str, Any],
    raw_type: str,
) -> dict:
    raw = existing if isinstance(existing, dict) else {}
    if simkl_id and not raw.get("simkl_id"):
        raw["simkl_id"] = simkl_id
    if raw_payload and not raw.get("simkl"):
        raw["simkl"] = raw_payload
    if raw_type and (raw.get("type") is None or raw_type == "anime"):
        raw["type"] = raw_type
    return raw


def _build_episode_raw(simkl_id: str | None, raw_payload: dict[str, Any]) -> dict:
    raw = {"source": "simkl", "type": "episode"}
    if simkl_id:
        raw["simkl_id"] = simkl_id
    if raw_payload:
        raw["simkl"] = raw_payload
    return raw


def _merge_episode_raw(
    existing: dict | None, simkl_id: str | None, raw_payload: dict[str, Any]
) -> dict:
    raw = existing if isinstance(existing, dict) else {}
    if simkl_id and not raw.get("simkl_id"):
        raw["simkl_id"] = simkl_id
    if raw_payload and not raw.get("simkl"):
        raw["simkl"] = raw_payload
    return raw


def _build_entry_key(
    history_id: str | None,
    imdb_id: str | None,
    tmdb_id: str | None,
    simkl_id: str | None,
    watched_at: datetime,
    prefix: str,
) -> str | None:
    if history_id:
        return f"history:{history_id}"
    if imdb_id:
        return f"{prefix}:imdb:{imdb_id}:{watched_at.date().isoformat()}"
    if tmdb_id:
        return f"{prefix}:tmdb:{tmdb_id}:{watched_at.date().isoformat()}"
    if simkl_id:
        return f"{prefix}:simkl:{simkl_id}:{watched_at.date().isoformat()}"
    return None


def _build_episode_entry_key(
    history_id: str | None,
    show: ShowSummary,
    episode: EpisodeSummary,
    watched_at: datetime,
) -> str | None:
    if history_id:
        return f"history:{history_id}"
    season = episode.season_number
    number = episode.episode_number
    suffix = f"s{season:02d}e{number:02d}:{watched_at.date().isoformat()}"
    if episode.imdb_id:
        return f"episode:imdb:{episode.imdb_id}:{suffix}"
    if episode.tmdb_id:
        return f"episode:tmdb:{episode.tmdb_id}:{suffix}"
    if episode.tvdb_id:
        return f"episode:tvdb:{episode.tvdb_id}:{suffix}"
    if episode.simkl_id:
        return f"episode:simkl:{episode.simkl_id}:{suffix}"
    if show.imdb_id:
        return f"episode:show_imdb:{show.imdb_id}:{suffix}"
    if show.tmdb_id:
        return f"episode:show_tmdb:{show.tmdb_id}:{suffix}"
    if show.tvdb_id:
        return f"episode:show_tvdb:{show.tvdb_id}:{suffix}"
    if show.simkl_id:
        return f"episode:show_simkl:{show.simkl_id}:{suffix}"
    title = show.title or "unknown"
    safe_title = re.sub(r"[^a-z0-9]+", "-", title.strip().lower()).strip("-")
    if safe_title:
        return f"episode:title:{safe_title}:{suffix}"
    return None


def _log_empty_history_payload(history_type: str, payload: object) -> None:
    summary = _summarize_payload(payload)
    if summary:
        logger.info(
            "SIMKL history payload empty for %s: %s",
            history_type,
            summary,
        )


def _summarize_payload(payload: object, limit: int = 8) -> str:
    if isinstance(payload, list):
        return f"list[{len(payload)}]"
    if not isinstance(payload, dict):
        return f"type={type(payload).__name__}"
    parts: list[str] = []
    for key in sorted(payload.keys()):
        if len(parts) >= limit:
            parts.append("...")
            break
        value = payload.get(key)
        if isinstance(value, list):
            parts.append(f"{key}=list[{len(value)}]")
        elif isinstance(value, dict):
            parts.append(f"{key}={_summarize_container(value)}")
        else:
            parts.append(f"{key}={type(value).__name__}")
    return ", ".join(parts)


def _summarize_container(payload: dict[str, Any]) -> str:
    for key in (
        "items",
        "all",
        "history",
        "entries",
        "list",
        "records",
        "episodes",
        "movies",
        "shows",
    ):
        value = payload.get(key)
        if isinstance(value, list):
            return f"list[{len(value)}]"
        if isinstance(value, dict):
            nested = _summarize_container(value)
            if nested:
                return nested
    keys = sorted(payload.keys())
    if not keys:
        return "dict[empty]"
    preview = ", ".join(keys[:5])
    return f"dict[{preview}]"


def _extract_entry_rating(entry: dict[str, Any], include_show_rating: bool) -> float | None:
    candidates: list[object] = []
    if include_show_rating:
        candidates.extend([entry.get("user_rating"), entry.get("rating")])
        for key in ("movie", "show", "anime", "item"):
            nested = entry.get(key)
            if isinstance(nested, dict):
                candidates.append(nested.get("user_rating"))
                candidates.append(nested.get("rating"))
    episode = entry.get("episode")
    if isinstance(episode, dict):
        candidates.append(episode.get("user_rating"))
        candidates.append(episode.get("rating"))
    for candidate in candidates:
        rating = normalize_ten_point_rating(candidate)
        if rating is not None:
            return rating
    return None


def _build_event_raw(
    entry_key: str,
    history_id: str | None,
    watched_at: datetime,
    show_or_movie: MovieSummary | ShowSummary,
    episode: EpisodeSummary | None,
    rating: float | None,
) -> dict[str, Any]:
    raw: dict[str, Any] = {
        "source": "simkl",
        "entry_key": entry_key,
        "history_id": history_id,
        "watched_at": watched_at.isoformat(),
    }
    raw["ids"] = {
        "imdb": show_or_movie.imdb_id,
        "tmdb": show_or_movie.tmdb_id,
        "simkl": show_or_movie.simkl_id,
    }
    if isinstance(show_or_movie, ShowSummary):
        raw["ids"]["tvdb"] = show_or_movie.tvdb_id
    if episode:
        raw["episode"] = {
            "season": episode.season_number,
            "number": episode.episode_number,
            "title": episode.title,
            "ids": {
                "imdb": episode.imdb_id,
                "tmdb": episode.tmdb_id,
                "tvdb": episode.tvdb_id,
                "simkl": episode.simkl_id,
            },
        }
    if rating is not None:
        raw["rating"] = rating
    return raw


def _extract_movie_summary(entry: dict[str, Any]) -> MovieSummary | None:
    movie = _extract_entry_payload(entry, "movie")
    if not movie:
        return None
    ids = _coerce_ids(movie.get("ids"))
    title = _coerce_str(movie.get("title")) or "SIMKL movie"
    year = _coerce_int(movie.get("year"))
    return MovieSummary(
        title=title,
        year=year,
        imdb_id=_normalize_imdb_id(ids.get("imdb")),
        tmdb_id=_coerce_str(ids.get("tmdb")),
        simkl_id=_coerce_str(ids.get("simkl")),
        raw=_sanitize_simkl_payload(movie),
    )


def _extract_show_summary(entry: dict[str, Any]) -> ShowSummary | None:
    show = _extract_entry_payload(entry, "show")
    if not show:
        return None
    ids = _coerce_ids(show.get("ids"))
    title = _coerce_str(show.get("title")) or "SIMKL show"
    year = _coerce_int(show.get("year"))
    return ShowSummary(
        title=title,
        year=year,
        imdb_id=_normalize_imdb_id(ids.get("imdb")),
        tmdb_id=_coerce_str(ids.get("tmdb")),
        tvdb_id=_coerce_str(ids.get("tvdb")),
        simkl_id=_coerce_str(ids.get("simkl")),
        raw=_sanitize_simkl_payload(show),
    )


def _extract_episode_summary(entry: dict[str, Any]) -> EpisodeSummary | None:
    payload = entry.get("episode")
    if isinstance(payload, (int, str)):
        payload = {
            "season": entry.get("season") or entry.get("season_number"),
            "episode": payload,
            "title": entry.get("episode_title") or entry.get("title"),
            "ids": entry.get("episode_ids") or entry.get("ids"),
        }
    if not isinstance(payload, dict):
        payload = entry if "season" in entry or "episode" in entry else None
    if not isinstance(payload, dict):
        return None
    season = _coerce_int(payload.get("season") or payload.get("season_number"))
    number = _coerce_int(
        payload.get("episode") or payload.get("number") or payload.get("episode_number")
    )
    if season is None or number is None:
        return None
    ids = _coerce_ids(payload.get("ids") or payload.get("episode_ids"))
    return EpisodeSummary(
        season_number=season,
        episode_number=number,
        title=_coerce_str(payload.get("title")),
        imdb_id=_normalize_imdb_id(ids.get("imdb")),
        tmdb_id=_coerce_str(ids.get("tmdb")),
        tvdb_id=_coerce_str(ids.get("tvdb")),
        simkl_id=_coerce_str(ids.get("simkl")),
        raw=_sanitize_simkl_payload(payload),
    )


def _sanitize_simkl_payload(payload: dict[str, Any]) -> dict[str, Any]:
    keep: dict[str, Any] = {}
    for key in ("title", "year", "season", "episode", "number"):
        if key in payload:
            keep[key] = payload[key]
    ids = payload.get("ids")
    if isinstance(ids, dict):
        keep["ids"] = {
            key: value
            for key, value in ids.items()
            if isinstance(value, (str, int))
        }
    return keep


def _extract_entry_payload(entry: dict[str, Any], key: str) -> dict[str, Any] | None:
    payload = entry.get(key)
    if isinstance(payload, dict):
        return payload
    item_payload = entry.get("item")
    if isinstance(item_payload, dict):
        return item_payload
    if key == "show":
        anime_payload = entry.get("anime")
        if isinstance(anime_payload, dict):
            return anime_payload
    if key in {"movie", "show"} and _looks_like_payload(entry):
        if key == "movie" and ("show" in entry or "episode" in entry):
            return None
        return entry
    return None


def _looks_like_payload(entry: dict[str, Any]) -> bool:
    if "ids" not in entry:
        return False
    if "title" not in entry:
        return False
    return True


def _extract_history_id(entry: dict[str, Any]) -> str | None:
    for key in ("history_id", "historyId", "history"):
        value = entry.get(key)
        if isinstance(value, (str, int)):
            return str(value)
    return None


def _extract_watched_at(entry: dict[str, Any]) -> datetime | None:
    for key in ("watched_at", "last_watched_at", "watched", "last_watched"):
        parsed = _parse_datetime(entry.get(key))
        if parsed:
            return parsed
    episode = entry.get("episode")
    if isinstance(episode, dict):
        for key in ("watched_at", "last_watched_at", "watched", "last_watched"):
            parsed = _parse_datetime(episode.get(key))
            if parsed:
                return parsed
    return None


def _expand_episode_entries(entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
    expanded: list[dict[str, Any]] = []
    for entry in entries:
        if "episode" in entry:
            expanded.append(entry)
            continue
        episodes = entry.get("episodes")
        if isinstance(episodes, list):
            base_watched = entry.get("watched_at") or entry.get("last_watched_at")
            for episode in episodes:
                if not isinstance(episode, dict):
                    continue
                merged = {"show": entry.get("show"), "episode": episode}
                watched_override = episode.get("watched_at") or base_watched
                if watched_override:
                    merged["watched_at"] = watched_override
                expanded.append(merged)
            continue
        season = _coerce_int(entry.get("season") or entry.get("season_number"))
        number = _coerce_int(
            entry.get("episode") or entry.get("number") or entry.get("episode_number")
        )
        if season is not None and number is not None and entry.get("show"):
            expanded.append(
                {
                    "show": entry.get("show"),
                    "episode": {
                        "season": season,
                        "episode": number,
                        "title": entry.get("episode_title") or entry.get("title"),
                        "ids": entry.get("episode_ids") or entry.get("ids"),
                    },
                    "watched_at": entry.get("watched_at"),
                }
            )
    return expanded


def _coerce_ids(value: object) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    return {}


def _normalize_imdb_id(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    cleaned = value.strip().lower()
    if cleaned.startswith("tt") and cleaned[2:].isdigit():
        return cleaned
    return None


def _coerce_str(value: object) -> str | None:
    if value is None:
        return None
    if isinstance(value, str):
        cleaned = value.strip()
        return cleaned or None
    if isinstance(value, (int, float)):
        return str(value)
    return None


def _coerce_int(value: object) -> int | None:
    if value is None:
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    if isinstance(value, str):
        cleaned = value.strip()
        if cleaned.isdigit():
            return int(cleaned)
    return None


def _parse_datetime(value: object) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(float(value), tz=timezone.utc)
    if isinstance(value, str):
        cleaned = value.strip()
        if not cleaned:
            return None
        if cleaned.isdigit():
            return datetime.fromtimestamp(float(cleaned), tz=timezone.utc)
        try:
            if cleaned.endswith("Z"):
                cleaned = f"{cleaned[:-1]}+00:00"
            parsed = datetime.fromisoformat(cleaned)
        except ValueError:
            parsed = None
        if parsed:
            if parsed.tzinfo is None:
                return parsed.replace(tzinfo=timezone.utc)
            return parsed
    return None


async def _ensure_simkl_access_token(
    db: AsyncSession,
    integration_id: str,
    secret_data: dict[str, object],
    client: SimklClient,
) -> str:
    access_token = secret_data.get("access_token")
    refresh_token = secret_data.get("refresh_token")
    if not isinstance(access_token, str) or not access_token:
        raise SimklError("SIMKL access token is missing", status_code=401)
    expires_at = parse_expires_at(secret_data.get("expires_at"))
    if not is_token_expired(expires_at):
        return access_token
    if not isinstance(refresh_token, str) or not refresh_token:
        raise SimklError("SIMKL refresh token is missing", status_code=401)
    token = await client.refresh_access_token(refresh_token)
    updated = dict(secret_data)
    updated.update(token_to_secret_payload(token))
    await _save_integration_secret(db, integration_id, updated)
    return token.access_token


async def _save_integration_secret(
    db: AsyncSession, integration_id: str, secret_data: dict[str, object]
) -> None:
    encrypted = encrypt_value(json.dumps(secret_data))
    result = await db.execute(
        select(IntegrationSecret).where(
            IntegrationSecret.integration_id == integration_id
        )
    )
    secret = result.scalars().first()
    if not secret:
        secret = IntegrationSecret(
            integration_id=integration_id,
            secret_data=encrypted,
        )
    else:
        secret.secret_data = encrypted
    db.add(secret)
