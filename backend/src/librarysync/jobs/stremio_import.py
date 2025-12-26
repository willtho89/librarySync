from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from librarysync.config import settings
from librarysync.connectors.services.stremio import (
    DEFAULT_STREMIO_API_BASE_URL,
    StremioClient,
    StremioError,
    has_required_stremio_fields,
)
from librarysync.core.import_schedule import (
    DEFAULT_IMPORT_INTERVAL_SECONDS,
    IMPORT_LAST_RUN_KEY,
    IMPORT_REQUESTED_KEY,
    parse_datetime,
    record_import_run,
    should_run_import,
)
from librarysync.core.integrations import load_integration_with_secrets
from librarysync.core.watch_pipeline import enqueue_new_item_job
from librarysync.db.models import (
    EpisodeItem,
    Integration,
    MediaItem,
    WatchedItem,
    WatchEvent,
    WatchSync,
)
from librarysync.db.session import SessionLocal, init_session_factory

LOOKBACK_HOURS = settings.history_lookback_days * 24
BATCH_SIZE = 50
IMDB_ID_RE = re.compile(r"(tt\d{3,10})", re.IGNORECASE)
TMDB_ID_RE = re.compile(r"tmdb[:/](?:movie|tv|show|series)?[:/](\d+)", re.IGNORECASE)
TMDB_SIMPLE_RE = re.compile(r"tmdb[:/](\d+)", re.IGNORECASE)
TVDB_ID_RE = re.compile(r"tvdb[:/](\d+)", re.IGNORECASE)
EPISODE_HINT_RE = re.compile(r":(\d+):(\d+)")
logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class MovieSummary:
    title: str
    year: int | None
    imdb_id: str | None
    tmdb_id: str | None
    tvdb_id: str | None
    stremio_id: str | None
    poster_url: str | None
    raw: dict[str, Any]


@dataclass(frozen=True)
class ShowSummary:
    title: str
    year: int | None
    imdb_id: str | None
    tmdb_id: str | None
    tvdb_id: str | None
    stremio_id: str | None
    poster_url: str | None
    raw: dict[str, Any]


@dataclass(frozen=True)
class EpisodeSummary:
    season_number: int
    episode_number: int
    title: str | None
    stremio_video_id: str | None
    raw: dict[str, Any]


@dataclass(frozen=True)
class ImportResult:
    imported: int
    attempted: bool


async def process_stremio_imports_once(
    lookback_hours: int = LOOKBACK_HOURS,
    batch_size: int = BATCH_SIZE,
) -> int:
    init_session_factory()
    async with SessionLocal() as db:
        result = await db.execute(select(Integration).where(Integration.provider == "stremio"))
        integrations = result.scalars().all()
        if not integrations:
            return 0
        now = datetime.now(timezone.utc)
        total_imported = 0
        for integration in integrations:
            if not should_run_import(
                integration.config,
                now,
                default_interval_seconds=DEFAULT_IMPORT_INTERVAL_SECONDS,
            ):
                continue
            requested_at = parse_datetime((integration.config or {}).get(IMPORT_REQUESTED_KEY))
            import_result = await _import_for_integration(
                db, integration, lookback_hours, batch_size, requested_at
            )
            total_imported += import_result.imported
            if import_result.attempted or requested_at is None:
                integration.config = record_import_run(integration.config, now)
                db.add(integration)
                await db.commit()
        return total_imported


async def _import_for_integration(
    db: AsyncSession,
    integration: Integration,
    lookback_hours: int,
    batch_size: int,
    requested_at: datetime | None,
) -> ImportResult:
    integration, secret_data = await load_integration_with_secrets(
        db, integration.user_id, "stremio"
    )
    if not integration or not secret_data:
        return ImportResult(imported=0, attempted=False)
    if not has_required_stremio_fields(secret_data):
        return ImportResult(imported=0, attempted=False)
    auth_key = _coerce_str(secret_data.get("auth_key"))
    if not auth_key:
        return ImportResult(imported=0, attempted=False)
    api_base_url = DEFAULT_STREMIO_API_BASE_URL
    if integration.config and integration.config.get("api_base_url"):
        api_base_url = str(integration.config["api_base_url"])
    client = StremioClient(api_base_url=api_base_url)

    now = datetime.now(timezone.utc)
    since = now - timedelta(hours=lookback_hours)
    last_run = parse_datetime((integration.config or {}).get(IMPORT_LAST_RUN_KEY))
    force_full = bool(requested_at and (last_run is None or requested_at > last_run))
    if force_full:
        since = None
    elif last_run and last_run > since:
        since = last_run - timedelta(minutes=5)

    try:
        timestamps = await client.get_library_item_timestamps(auth_key)
    except StremioError as exc:
        logger.warning(
            "Stremio timestamp fetch failed for user %s: %s",
            integration.user_id,
            exc,
        )
        return ImportResult(imported=0, attempted=True)

    ids = _select_item_ids(timestamps, since)
    if not ids:
        if force_full:
            logger.info(
                "Stremio import falling back to full library fetch for user %s",
                integration.user_id,
            )
            try:
                items = await client.get_library_items(auth_key, ids=None)
            except StremioError as exc:
                logger.warning(
                    "Stremio full library fetch failed for user %s: %s",
                    integration.user_id,
                    exc,
                )
                return ImportResult(imported=0, attempted=True)
            imported = 0
            for item in items:
                try:
                    if await _import_library_item(db, integration.user_id, item):
                        imported += 1
                except Exception:
                    logger.exception(
                        "Stremio item import failed for user %s",
                        integration.user_id,
                    )
                    await db.rollback()
            if imported:
                logger.info(
                    "Imported %s Stremio entries for user %s",
                    imported,
                    integration.user_id,
                )
            return ImportResult(imported=imported, attempted=True)
        logger.info(
            "Stremio import found no matching items for user %s",
            integration.user_id,
        )
        return ImportResult(imported=0, attempted=True)

    imported = 0
    for chunk in _chunked(ids, batch_size):
        try:
            items = await client.get_library_items(auth_key, ids=chunk)
        except StremioError as exc:
            logger.warning(
                "Stremio library fetch failed for user %s: %s",
                integration.user_id,
                exc,
            )
            continue
        for item in items:
            try:
                if await _import_library_item(db, integration.user_id, item):
                    imported += 1
            except Exception:
                logger.exception("Stremio item import failed for user %s", integration.user_id)
                await db.rollback()
    if imported:
        logger.info(
            "Imported %s Stremio entries for user %s",
            imported,
            integration.user_id,
        )
    return ImportResult(imported=imported, attempted=True)


async def _import_library_item(db: AsyncSession, user_id: str, item: dict[str, Any]) -> bool:
    item_id = _coerce_str(item.get("_id") or item.get("id"))
    if not item_id:
        return False
    item_type = (_coerce_str(item.get("type")) or "").lower()
    state = item.get("state")
    state_data = state if isinstance(state, dict) else {}
    watched_at = _parse_state_watched_at(state_data)
    if not watched_at:
        if not _state_indicates_watched(state_data):
            return False
        watched_at = _parse_item_timestamp(item)
    if not watched_at:
        return False

    if item_type == "movie":
        movie = _build_movie_summary(item_id, item, state_data)
        if not movie:
            return False
        entry_key = _build_entry_key(
            "movie",
            movie.imdb_id or movie.stremio_id or item_id,
            None,
            watched_at,
        )
        if not entry_key:
            return False
        if await _entry_already_imported(db, user_id, entry_key):
            return False
        media_item = await _get_or_create_movie_item(db, movie)
        if not media_item:
            return False
        watched = WatchedItem(
            user_id=user_id,
            media_item_id=media_item.id,
            episode_item_id=None,
            watched_at=watched_at,
            rating=None,
            source="stremio",
        )
        event = WatchEvent(
            user_id=user_id,
            media_item_id=media_item.id,
            episode_item_id=None,
            event_type="stremio_imported",
            occurred_at=watched_at,
            raw=_build_event_raw(entry_key, item_id, watched_at, movie.raw, None, None),
        )
        db.add_all([watched, event])
        await db.flush()
        watch_sync = WatchSync(
            user_id=user_id,
            watched_item_id=watched.id,
            provider="stremio",
            status="synced_from_stremio",
            is_rewatch=False,
            external_id=item_id,
            last_synced_at=datetime.now(timezone.utc),
        )
        db.add(watch_sync)
        await enqueue_new_item_job(
            db,
            user_id,
            watched.id,
            is_rewatch=False,
            source="stremio_import",
        )
        await db.commit()
        return True

    if item_type in {"series", "show", "tv"}:
        hint_video_id = _extract_behavior_hint_id(item)
        show = _build_show_summary(item_id, item, state_data)
        if not show:
            return False
        episode = _build_episode_summary(state_data, hint_video_id)
        if not episode:
            return False
        entry_key = _build_entry_key(
            "episode",
            show.imdb_id or show.stremio_id or item_id,
            episode.stremio_video_id,
            watched_at,
            episode.season_number,
            episode.episode_number,
        )
        if not entry_key:
            return False
        if await _entry_already_imported(db, user_id, entry_key):
            return False
        show_item = await _get_or_create_show_item(db, show)
        if not show_item:
            return False
        episode_item = await _get_or_create_episode_item(db, show_item, episode)
        if not episode_item:
            return False
        watched = WatchedItem(
            user_id=user_id,
            media_item_id=None,
            episode_item_id=episode_item.id,
            watched_at=watched_at,
            rating=None,
            source="stremio",
        )
        event = WatchEvent(
            user_id=user_id,
            media_item_id=None,
            episode_item_id=episode_item.id,
            event_type="stremio_imported",
            occurred_at=watched_at,
            raw=_build_event_raw(
                entry_key,
                item_id,
                watched_at,
                show.raw,
                episode.raw,
                episode.stremio_video_id,
            ),
        )
        db.add_all([watched, event])
        await db.flush()
        watch_sync = WatchSync(
            user_id=user_id,
            watched_item_id=watched.id,
            provider="stremio",
            status="synced_from_stremio",
            is_rewatch=False,
            external_id=episode.stremio_video_id or item_id,
            last_synced_at=datetime.now(timezone.utc),
        )
        db.add(watch_sync)
        await enqueue_new_item_job(
            db,
            user_id,
            watched.id,
            is_rewatch=False,
            source="stremio_import",
        )
        await db.commit()
        return True

    return False


def _build_movie_summary(
    item_id: str, item: dict[str, Any], state: dict[str, Any]
) -> MovieSummary | None:
    hint = _extract_behavior_hint_id(item)
    imdb_id = _extract_first_imdb_id(
        item_id,
        item.get("imdb_id"),
        item.get("imdbId"),
        state.get("video_id"),
        state.get("videoId"),
        hint,
    )
    tmdb_id = _extract_first_tmdb_id(
        item_id,
        item.get("tmdb_id"),
        item.get("tmdbId"),
        hint,
    )
    tvdb_id = _extract_first_tvdb_id(
        item_id,
        item.get("tvdb_id"),
        item.get("tvdbId"),
        hint,
    )
    title = _coerce_str(item.get("name")) or "Stremio movie"
    year = _coerce_int(item.get("year"))
    poster_url = _coerce_str(item.get("poster"))
    raw = _sanitize_item(item, state)
    return MovieSummary(
        title=title,
        year=year,
        imdb_id=imdb_id,
        tmdb_id=tmdb_id,
        tvdb_id=tvdb_id,
        stremio_id=item_id,
        poster_url=poster_url,
        raw=raw,
    )


def _build_show_summary(
    item_id: str, item: dict[str, Any], state: dict[str, Any]
) -> ShowSummary | None:
    hint = _extract_behavior_hint_id(item)
    imdb_id = _extract_first_imdb_id(
        item_id,
        item.get("imdb_id"),
        item.get("imdbId"),
        state.get("video_id"),
        state.get("videoId"),
        hint,
    )
    tmdb_id = _extract_first_tmdb_id(
        item_id,
        item.get("tmdb_id"),
        item.get("tmdbId"),
        hint,
    )
    tvdb_id = _extract_first_tvdb_id(
        item_id,
        item.get("tvdb_id"),
        item.get("tvdbId"),
        hint,
    )
    title = _coerce_str(item.get("name")) or "Stremio show"
    year = _coerce_int(item.get("year"))
    poster_url = _coerce_str(item.get("poster"))
    raw = _sanitize_item(item, state)
    return ShowSummary(
        title=title,
        year=year,
        imdb_id=imdb_id,
        tmdb_id=tmdb_id,
        tvdb_id=tvdb_id,
        stremio_id=item_id,
        poster_url=poster_url,
        raw=raw,
    )


def _build_episode_summary(
    state: dict[str, Any], fallback_video_id: str | None = None
) -> EpisodeSummary | None:
    season_number = _coerce_int(state.get("season"))
    episode_number = _coerce_int(state.get("episode"))
    video_id = _coerce_str(state.get("video_id") or state.get("videoId"))
    if not video_id and fallback_video_id:
        video_id = _coerce_str(fallback_video_id)
    if season_number is not None and season_number <= 0:
        season_number = None
    if episode_number is not None and episode_number <= 0:
        episode_number = None
    if (season_number is None or episode_number is None) and video_id:
        match = EPISODE_HINT_RE.search(video_id)
        if match:
            if season_number is None:
                season_number = _coerce_int(match.group(1))
            if episode_number is None:
                episode_number = _coerce_int(match.group(2))
    if season_number is None or episode_number is None:
        return None
    raw = {
        "season": season_number,
        "episode": episode_number,
        "video_id": video_id,
        "state": _sanitize_state(state),
    }
    return EpisodeSummary(
        season_number=season_number,
        episode_number=episode_number,
        title=None,
        stremio_video_id=video_id,
        raw=raw,
    )


async def _get_or_create_movie_item(db: AsyncSession, movie: MovieSummary) -> MediaItem | None:
    item = await _find_media_item(
        db,
        movie.imdb_id,
        movie.tmdb_id,
        movie.tvdb_id,
        movie.stremio_id,
        "movie",
    )
    if item:
        _apply_media_updates(item, movie.title, movie.year, movie.poster_url)
        await _apply_media_ids(db, item, movie.imdb_id, movie.tmdb_id, movie.tvdb_id)
        _apply_media_raw(item, movie.stremio_id, movie.raw, "movie")
        return item
    if not movie.imdb_id and not movie.tmdb_id and not movie.tvdb_id and not movie.stremio_id:
        return None
    item = MediaItem(
        media_type="movie",
        title=movie.title,
        year=movie.year,
        imdb_id=movie.imdb_id,
        tmdb_id=movie.tmdb_id,
        tvdb_id=movie.tvdb_id,
        poster_url=movie.poster_url,
        raw=_build_media_raw(movie.stremio_id, movie.raw, "movie"),
    )
    db.add(item)
    await db.flush()
    return item


async def _get_or_create_show_item(db: AsyncSession, show: ShowSummary) -> MediaItem | None:
    item = await _find_media_item(
        db,
        show.imdb_id,
        show.tmdb_id,
        show.tvdb_id,
        show.stremio_id,
        "tv",
    )
    if item:
        _apply_media_updates(item, show.title, show.year, show.poster_url)
        await _apply_media_ids(db, item, show.imdb_id, show.tmdb_id, show.tvdb_id)
        _apply_media_raw(item, show.stremio_id, show.raw, "show")
        return item
    if not show.imdb_id and not show.tmdb_id and not show.tvdb_id and not show.stremio_id:
        return None
    item = MediaItem(
        media_type="tv",
        title=show.title,
        year=show.year,
        imdb_id=show.imdb_id,
        tmdb_id=show.tmdb_id,
        tvdb_id=show.tvdb_id,
        poster_url=show.poster_url,
        raw=_build_media_raw(show.stremio_id, show.raw, "show"),
    )
    db.add(item)
    await db.flush()
    return item


async def _get_or_create_episode_item(
    db: AsyncSession, show_item: MediaItem, episode: EpisodeSummary
) -> EpisodeItem | None:
    result = await db.execute(
        select(EpisodeItem).where(
            EpisodeItem.show_media_item_id == show_item.id,
            EpisodeItem.season_number == episode.season_number,
            EpisodeItem.episode_number == episode.episode_number,
        )
    )
    item = result.scalars().first()
    if item:
        _apply_episode_raw(item, episode.stremio_video_id, episode.raw)
        return item
    item = EpisodeItem(
        show_media_item_id=show_item.id,
        season_number=episode.season_number,
        episode_number=episode.episode_number,
        title=episode.title,
        raw=_build_episode_raw(episode.stremio_video_id, episode.raw),
    )
    db.add(item)
    await db.flush()
    return item


async def _find_media_item(
    db: AsyncSession,
    imdb_id: str | None,
    tmdb_id: str | None,
    tvdb_id: str | None,
    stremio_id: str | None,
    media_type: str,
) -> MediaItem | None:
    item: MediaItem | None = None
    if imdb_id:
        result = await db.execute(select(MediaItem).where(MediaItem.imdb_id == imdb_id))
        item = result.scalars().first()
    if not item and tmdb_id:
        result = await db.execute(
            select(MediaItem).where(
                MediaItem.tmdb_id == tmdb_id,
                MediaItem.media_type == media_type,
            )
        )
        item = result.scalars().first()
    if not item and tvdb_id:
        result = await db.execute(
            select(MediaItem).where(
                MediaItem.tvdb_id == tvdb_id,
                MediaItem.media_type == media_type,
            )
        )
        item = result.scalars().first()
    if not item and stremio_id:
        result = await db.execute(
            select(MediaItem).where(
                MediaItem.media_type == media_type,
                MediaItem.raw["stremio_id"].as_string() == stremio_id,
            )
        )
        item = result.scalars().first()
    return item


async def _entry_already_imported(db: AsyncSession, user_id: str, entry_key: str) -> bool:
    result = await db.execute(
        select(WatchEvent.id).where(
            WatchEvent.user_id == user_id,
            WatchEvent.event_type == "stremio_imported",
            WatchEvent.raw["entry_key"].as_string() == entry_key,
        )
    )
    return result.scalars().first() is not None


def _select_item_ids(timestamps: list[Any], since: datetime | None) -> list[str]:
    entries: list[tuple[str, datetime]] = []
    seen: set[str] = set()
    for entry in timestamps:
        parsed = _parse_library_timestamp(entry)
        if not parsed:
            continue
        item_id, modified_at = parsed
        if since and modified_at < since:
            continue
        if item_id in seen:
            continue
        seen.add(item_id)
        entries.append((item_id, modified_at))
    # Sort by modified_at descending (most recent first) and take the first 50
    entries.sort(key=lambda x: x[1], reverse=True)
    return [item_id for item_id, _ in entries[:50]]


def _parse_library_timestamp(entry: Any) -> tuple[str, datetime] | None:
    if isinstance(entry, (list, tuple)) and len(entry) >= 2:
        item_id = _coerce_str(entry[0])
        modified_at = _parse_datetime(entry[1])
    elif isinstance(entry, dict):
        item_id = _coerce_str(entry.get("_id") or entry.get("id"))
        modified_at = _parse_datetime(
            entry.get("mtime") or entry.get("modified_at") or entry.get("lastModified")
        )
    else:
        return None
    if not item_id or not modified_at:
        return None
    return item_id, modified_at


def _build_entry_key(
    kind: str,
    item_id: str | None,
    video_id: str | None,
    watched_at: datetime,
    season_number: int | None = None,
    episode_number: int | None = None,
) -> str | None:
    if not item_id:
        return None
    base = f"{kind}:stremio:{item_id}:{watched_at.isoformat()}"
    if season_number is not None and episode_number is not None:
        return f"{base}:s{season_number}e{episode_number}"
    if video_id:
        return f"{base}:{video_id}"
    return base


def _build_event_raw(
    entry_key: str,
    item_id: str,
    watched_at: datetime,
    item_payload: dict[str, Any],
    episode_payload: dict[str, Any] | None,
    video_id: str | None,
) -> dict[str, Any]:
    data: dict[str, Any] = {
        "entry_key": entry_key,
        "stremio_id": item_id,
        "watched_at": watched_at.isoformat(),
        "item": item_payload,
    }
    if episode_payload:
        data["episode"] = episode_payload
    if video_id:
        data["video_id"] = video_id
    return data


def _build_media_raw(stremio_id: str | None, payload: dict[str, Any], label: str) -> dict[str, Any]:
    raw = {"source": "stremio", "type": label}
    if stremio_id:
        raw["stremio_id"] = stremio_id
    if payload:
        raw["stremio"] = payload
    return raw


def _build_episode_raw(stremio_video_id: str | None, payload: dict[str, Any]) -> dict[str, Any]:
    raw = {"source": "stremio", "type": "episode"}
    if stremio_video_id:
        raw["stremio_video_id"] = stremio_video_id
    if payload:
        raw["stremio"] = payload
    return raw


def _apply_media_updates(
    item: MediaItem, title: str, year: int | None, poster_url: str | None
) -> None:
    if title and (not item.title or item.title.startswith("Stremio ")):
        item.title = title
    if year is not None and item.year is None:
        item.year = year
    if poster_url and not item.poster_url:
        item.poster_url = poster_url


def _apply_media_raw(
    item: MediaItem, stremio_id: str | None, payload: dict[str, Any], label: str
) -> None:
    if not payload and not stremio_id:
        return
    raw = dict(item.raw or {})
    if stremio_id and not raw.get("stremio_id"):
        raw["stremio_id"] = stremio_id
    if payload and not raw.get("stremio"):
        raw["stremio"] = payload
    if raw.get("source") != "stremio":
        raw["source"] = "stremio"
    if raw.get("type") != label:
        raw["type"] = label
    item.raw = raw


def _apply_episode_raw(
    item: EpisodeItem, stremio_video_id: str | None, payload: dict[str, Any]
) -> None:
    if not payload and not stremio_video_id:
        return
    raw = dict(item.raw or {})
    if stremio_video_id and not raw.get("stremio_video_id"):
        raw["stremio_video_id"] = stremio_video_id
    if payload and not raw.get("stremio"):
        raw["stremio"] = payload
    if raw.get("source") != "stremio":
        raw["source"] = "stremio"
    if raw.get("type") != "episode":
        raw["type"] = "episode"
    item.raw = raw


def _sanitize_item(item: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
    payload = {
        "id": _coerce_str(item.get("_id") or item.get("id")),
        "type": _coerce_str(item.get("type")),
        "name": _coerce_str(item.get("name")),
        "year": _coerce_int(item.get("year")),
        "poster": _coerce_str(item.get("poster")),
        "state": _sanitize_state(state),
    }
    return {key: value for key, value in payload.items() if value is not None}


def _sanitize_state(state: dict[str, Any]) -> dict[str, Any]:
    payload = {
        "lastWatched": _coerce_str(state.get("lastWatched")),
        "timeWatched": _coerce_int(state.get("timeWatched")),
        "timeOffset": _coerce_int(state.get("timeOffset")),
        "duration": _coerce_int(state.get("duration")),
        "timesWatched": _coerce_int(state.get("timesWatched")),
        "flaggedWatched": _coerce_int(state.get("flaggedWatched")),
        "season": _coerce_int(state.get("season")),
        "episode": _coerce_int(state.get("episode")),
        "video_id": _coerce_str(state.get("video_id") or state.get("videoId")),
    }
    return {key: value for key, value in payload.items() if value is not None}


def _parse_state_watched_at(state: dict[str, Any]) -> datetime | None:
    for key in ("lastWatched", "last_watched"):
        parsed = _parse_datetime(state.get(key))
        if parsed:
            return parsed
    watched_value = state.get("watched")
    if _looks_like_watch_flag(watched_value):
        return None
    return _parse_datetime(watched_value)


def _state_indicates_watched(state: dict[str, Any]) -> bool:
    if _parse_state_watched_at(state):
        return True
    if _coerce_bool(state.get("watched")):
        return True
    flagged = _coerce_int(state.get("flaggedWatched"))
    if flagged and flagged > 0:
        return True
    times_watched = _coerce_int(state.get("timesWatched"))
    if times_watched and times_watched > 0:
        return True
    time_watched = _coerce_number(state.get("timeWatched") or state.get("overallTimeWatched"))
    duration = _coerce_number(state.get("duration"))
    if time_watched and duration and duration > 0:
        threshold = max(settings.completion_threshold_percent, 0.0) / 100.0
        if time_watched / duration >= threshold:
            return True
    return False


def _parse_item_timestamp(item: dict[str, Any]) -> datetime | None:
    for key in ("_mtime", "mtime", "lastModified", "_ctime", "ctime"):
        parsed = _parse_datetime(item.get(key))
        if parsed:
            return parsed
    return None


def _looks_like_watch_flag(value: object) -> bool:
    if value is None:
        return False
    if isinstance(value, bool):
        return True
    if isinstance(value, (int, float)):
        return value < 1_000_000_000
    if isinstance(value, str):
        cleaned = value.strip().lower()
        if cleaned in {"true", "false", "yes", "no"}:
            return True
        if cleaned.isdigit():
            try:
                return int(cleaned) < 1_000_000_000
            except ValueError:
                return False
    return False


def _parse_datetime(value: Any) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if isinstance(value, (int, float)):
        return _parse_timestamp(float(value))
    if isinstance(value, str):
        cleaned = value.strip()
        if not cleaned:
            return None
        if cleaned.isdigit():
            try:
                return _parse_timestamp(float(cleaned))
            except ValueError:
                return None
        try:
            if cleaned.endswith("Z"):
                cleaned = f"{cleaned[:-1]}+00:00"
            parsed = datetime.fromisoformat(cleaned)
            return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
        except ValueError:
            return None
    return None


def _parse_timestamp(value: float) -> datetime:
    if value > 10_000_000_000:
        value /= 1000.0
    return datetime.fromtimestamp(value, tz=timezone.utc)


def _extract_behavior_hint_id(item: dict[str, Any]) -> str | None:
    hints = item.get("behaviorHints")
    if not isinstance(hints, dict):
        return None
    value = _coerce_str(hints.get("defaultVideoId")) or _coerce_str(hints.get("featuredVideoId"))
    return value or None


def _extract_first_imdb_id(*values: object) -> str | None:
    for value in values:
        imdb_id = _extract_imdb_id(value)
        if imdb_id:
            return imdb_id
    return None


def _extract_first_tmdb_id(*values: object) -> str | None:
    for value in values:
        tmdb_id = _extract_tmdb_id(value)
        if tmdb_id:
            return tmdb_id
    return None


def _extract_first_tvdb_id(*values: object) -> str | None:
    for value in values:
        tvdb_id = _extract_tvdb_id(value)
        if tvdb_id:
            return tvdb_id
    return None


def _extract_imdb_id(value: object) -> str | None:
    if not value:
        return None
    match = IMDB_ID_RE.search(str(value))
    if not match:
        return None
    return match.group(1).lower()


def _extract_tmdb_id(value: object) -> str | None:
    if not value:
        return None
    if isinstance(value, (int, float)):
        if float(value).is_integer():
            return str(int(value))
        return None
    if isinstance(value, str):
        cleaned = value.strip()
        if cleaned.isdigit():
            return cleaned
    text = str(value)
    match = TMDB_ID_RE.search(text) or TMDB_SIMPLE_RE.search(text)
    if not match:
        return None
    return match.group(1)


def _extract_tvdb_id(value: object) -> str | None:
    if not value:
        return None
    if isinstance(value, (int, float)):
        if float(value).is_integer():
            return str(int(value))
        return None
    if isinstance(value, str):
        cleaned = value.strip()
        if cleaned.isdigit():
            return cleaned
    match = TVDB_ID_RE.search(str(value))
    if not match:
        return None
    return match.group(1)


def _coerce_str(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    cleaned = value.strip()
    return cleaned or None


def _coerce_int(value: object) -> int | None:
    if value is None:
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    if isinstance(value, str):
        cleaned = value.strip()
        if cleaned.isdigit():
            try:
                return int(cleaned)
            except ValueError:
                return None
    return None


def _coerce_number(value: object) -> float | None:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        cleaned = value.strip()
        if not cleaned:
            return None
        try:
            return float(cleaned)
        except ValueError:
            return None
    return None


def _coerce_bool(value: object) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    if isinstance(value, str):
        cleaned = value.strip().lower()
        if cleaned in {"true", "1", "yes", "y"}:
            return True
        if cleaned in {"false", "0", "no", "n"}:
            return False
    return False


async def _apply_media_ids(
    db: AsyncSession,
    item: MediaItem,
    imdb_id: str | None,
    tmdb_id: str | None,
    tvdb_id: str | None,
) -> None:
    if imdb_id and not item.imdb_id:
        if await _can_assign_media_id(db, item, "imdb_id", imdb_id):
            item.imdb_id = imdb_id
    if tmdb_id and not item.tmdb_id:
        if await _can_assign_media_id(db, item, "tmdb_id", tmdb_id):
            item.tmdb_id = tmdb_id
    if tvdb_id and not item.tvdb_id:
        if await _can_assign_media_id(db, item, "tvdb_id", tvdb_id):
            item.tvdb_id = tvdb_id


async def _can_assign_media_id(
    db: AsyncSession,
    item: MediaItem,
    field: str,
    value: str,
) -> bool:
    if field == "imdb_id":
        result = await db.execute(select(MediaItem.id).where(MediaItem.imdb_id == value))
    elif field == "tmdb_id":
        result = await db.execute(
            select(MediaItem.id).where(
                MediaItem.tmdb_id == value,
                MediaItem.media_type == item.media_type,
            )
        )
    elif field == "tvdb_id":
        result = await db.execute(
            select(MediaItem.id).where(
                MediaItem.tvdb_id == value,
                MediaItem.media_type == item.media_type,
            )
        )
    else:
        return False
    existing = result.scalars().first()
    return existing is None or existing == item.id


def _chunked(values: Iterable[str], size: int) -> Iterable[list[str]]:
    batch: list[str] = []
    for value in values:
        batch.append(value)
        if len(batch) >= size:
            yield batch
            batch = []
    if batch:
        yield batch
