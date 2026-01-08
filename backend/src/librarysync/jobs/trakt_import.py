from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from librarysync.config import settings
from librarysync.connectors.services.trakt import (
    TraktClient,
    TraktError,
    has_required_trakt_fields,
    is_token_expired,
    parse_expires_at,
    token_to_secret_payload,
)
from librarysync.core.integrations import load_integration_with_secrets
from librarysync.core.ratings import normalize_ten_point_rating
from librarysync.core.security import encrypt_value
from librarysync.db.models import (
    EpisodeItem,
    Integration,
    IntegrationSecret,
    MediaItem,
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
MAX_PAGES = 8
PER_PAGE = 50
ENTRY_KEY_BATCH_SIZE = 200
logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class MovieSummary:
    title: str
    year: int | None
    imdb_id: str | None
    tmdb_id: str | None
    trakt_id: str | None
    raw: dict[str, Any]


@dataclass(frozen=True)
class ShowSummary:
    title: str
    year: int | None
    imdb_id: str | None
    tmdb_id: str | None
    tvdb_id: str | None
    trakt_id: str | None
    raw: dict[str, Any]


@dataclass(frozen=True)
class EpisodeSummary:
    season_number: int
    episode_number: int
    title: str | None
    imdb_id: str | None
    tmdb_id: str | None
    tvdb_id: str | None
    trakt_id: str | None
    raw: dict[str, Any]


class TraktImportStrategy(ImportStrategy):
    provider = "trakt"

    def __init__(
        self,
        lookback_days: int = LOOKBACK_DAYS,
        per_page: int = PER_PAGE,
        max_pages: int = MAX_PAGES,
    ) -> None:
        self._lookback_days = lookback_days
        self._per_page = per_page
        self._max_pages = max_pages

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
            self._per_page,
            self._max_pages,
        )


async def _import_for_integration(
    db: AsyncSession,
    integration: Integration,
    lookback_days: int,
    per_page: int,
    max_pages: int,
) -> ImportResult:
    if not settings.trakt_client_id or not settings.trakt_client_secret:
        return ImportResult(imported=0, attempted=False)
    integration, secret_data = await load_integration_with_secrets(
        db, integration.user_id, "trakt"
    )
    if not integration or not secret_data:
        return ImportResult(imported=0, attempted=False)
    if not has_required_trakt_fields(secret_data):
        return ImportResult(imported=0, attempted=False)
    client = TraktClient(
        client_id=settings.trakt_client_id,
        client_secret=settings.trakt_client_secret,
    )
    try:
        access_token = await _ensure_trakt_access_token(
            db, integration.id, secret_data, client
        )
    except TraktError as exc:
        logger.warning(
            "Trakt token refresh failed for user %s: %s",
            integration.user_id,
            exc,
        )
        return ImportResult(imported=0, attempted=True)

    imported = 0
    now = datetime.now(timezone.utc)
    full_history = lookback_days < 0
    start_at = _select_trakt_start_at(now, lookback_days)
    max_pages = None if full_history else max_pages
    for history_type in ("movies", "episodes"):
        entries = await client.get_history(
            access_token,
            history_type=history_type,
            start_at=start_at,
            per_page=per_page,
            max_pages=max_pages,
        )
        rating_lookup: dict[str, float] = {}
        if entries:
            rating_lookup = await _load_trakt_rating_lookup(
                client,
                access_token,
                history_type,
                per_page=per_page,
                max_pages=max_pages,
                user_id=integration.user_id,
            )
        for batch in chunked(entries, ENTRY_KEY_BATCH_SIZE):
            candidates: list[ImportCandidate] = []
            for entry in batch:
                if history_type == "movies":
                    candidate = _build_movie_candidate(entry, rating_lookup)
                else:
                    candidate = _build_episode_candidate(entry, rating_lookup)
                if candidate:
                    candidates.append(candidate)
            imported += await process_import_candidates(
                db,
                integration.user_id,
                "trakt",
                candidates,
            )
    if imported:
        logger.info(
            "Imported %s Trakt entries for user %s",
            imported,
            integration.user_id,
        )
    return ImportResult(imported=imported, attempted=True)


def _select_trakt_start_at(now: datetime, lookback_days: int) -> datetime | None:
    if lookback_days < 0:
        return None
    return now - timedelta(days=lookback_days)


async def _load_trakt_rating_lookup(
    client: TraktClient,
    access_token: str,
    history_type: str,
    per_page: int,
    max_pages: int,
    user_id: str,
) -> dict[str, float]:
    rating_type_map = {"movies": ("movies", "movie"), "episodes": ("episodes", "episode")}
    mapped = rating_type_map.get(history_type)
    if not mapped:
        return {}
    rating_type, item_key = mapped
    try:
        entries = await client.get_ratings(
            access_token,
            rating_type=rating_type,
            per_page=per_page,
            max_pages=max_pages,
        )
    except TraktError as exc:
        logger.warning("Trakt ratings fetch failed for user %s: %s", user_id, exc)
        return {}
    return _build_trakt_rating_lookup(entries, item_key)


def _build_trakt_rating_lookup(
    entries: list[dict[str, Any]],
    item_key: str,
) -> dict[str, float]:
    lookup: dict[str, float] = {}
    for entry in entries:
        rating = normalize_ten_point_rating(entry.get("rating"))
        if rating is None:
            continue
        item = entry.get(item_key)
        if not isinstance(item, dict):
            continue
        ids = _coerce_ids(item.get("ids"))
        _insert_rating_lookup(lookup, "imdb", _normalize_imdb_id(ids.get("imdb")), rating)
        _insert_rating_lookup(lookup, "tmdb", _coerce_str(ids.get("tmdb")), rating)
        _insert_rating_lookup(lookup, "trakt", _coerce_str(ids.get("trakt")), rating)
    return lookup


def _insert_rating_lookup(
    lookup: dict[str, float], label: str, value: str | None, rating: float
) -> None:
    if value:
        lookup[f"{label}:{value}"] = rating


def _lookup_rating(
    lookup: dict[str, float],
    imdb_id: str | None,
    tmdb_id: str | None,
    trakt_id: str | None,
) -> float | None:
    for label, value in (("imdb", imdb_id), ("tmdb", tmdb_id), ("trakt", trakt_id)):
        if value:
            rating = lookup.get(f"{label}:{value}")
            if rating is not None:
                return rating
    return None


def _extract_entry_rating(
    entry: dict[str, Any],
    lookup: dict[str, float],
    imdb_id: str | None,
    tmdb_id: str | None,
    trakt_id: str | None,
) -> float | None:
    rating = normalize_ten_point_rating(entry.get("rating"))
    if rating is not None:
        return rating
    return _lookup_rating(lookup, imdb_id, tmdb_id, trakt_id)


def _build_movie_candidate(
    entry: dict[str, Any],
    rating_lookup: dict[str, float],
) -> ImportCandidate | None:
    history_id = _coerce_str(entry.get("id"))
    watched_at = _parse_datetime(entry.get("watched_at")) or datetime.now(timezone.utc)
    movie = _extract_movie_summary(entry)
    if not movie:
        return None
    rating = _extract_entry_rating(
        entry, rating_lookup, movie.imdb_id, movie.tmdb_id, movie.trakt_id
    )
    entry_key = _build_entry_key(
        history_id, movie.imdb_id, movie.tmdb_id, watched_at, "movie"
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


def _build_episode_candidate(
    entry: dict[str, Any],
    rating_lookup: dict[str, float],
) -> ImportCandidate | None:
    history_id = _coerce_str(entry.get("id"))
    watched_at = _parse_datetime(entry.get("watched_at")) or datetime.now(timezone.utc)
    show = _extract_show_summary(entry)
    episode = _extract_episode_summary(entry)
    if not show or not episode:
        return None
    rating = _extract_entry_rating(
        entry, rating_lookup, episode.imdb_id, episode.tmdb_id, episode.trakt_id
    )
    entry_key = _build_entry_key(
        history_id,
        episode.imdb_id or show.imdb_id,
        episode.tmdb_id or show.tmdb_id,
        watched_at,
        "episode",
    )
    if not entry_key:
        return None

    async def _build_items(db: AsyncSession) -> ImportItems:
        show_item = await _get_or_create_show_item(db, show)
        if not show_item:
            return ImportItems(media_item=None, episode_item=None, show_item=None)
        episode_item = await _get_or_create_episode_item(db, show_item, episode)
        return ImportItems(
            media_item=None,
            episode_item=episode_item,
            show_item=show_item,
        )

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
        blacklist_enabled=True,
        is_rewatch=False,
        build_items=_build_items,
    )


async def _get_or_create_movie_item(
    db: AsyncSession, movie: MovieSummary
) -> MediaItem | None:
    item = await _find_media_item(
        db, movie.imdb_id, movie.tmdb_id, movie.trakt_id, "movie"
    )
    if item:
        _apply_movie_updates(item, movie)
        return item
    if not movie.imdb_id and not movie.tmdb_id and not movie.trakt_id:
        return None
    item = MediaItem(
        media_type="movie",
        title=movie.title,
        year=movie.year,
        imdb_id=movie.imdb_id,
        tmdb_id=movie.tmdb_id,
        poster_url=None,
        raw=_build_media_raw(movie.trakt_id, movie.raw, "movie"),
    )
    db.add(item)
    await db.flush()
    return item


async def _get_or_create_show_item(
    db: AsyncSession, show: ShowSummary
) -> MediaItem | None:
    item = await _find_media_item(
        db, show.imdb_id, show.tmdb_id, show.trakt_id, "tv"
    )
    if item:
        _apply_show_updates(item, show)
        return item
    if not show.imdb_id and not show.tmdb_id and not show.trakt_id and not show.tvdb_id:
        return None
    item = MediaItem(
        media_type="tv",
        title=show.title,
        year=show.year,
        imdb_id=show.imdb_id,
        tmdb_id=show.tmdb_id,
        tvdb_id=show.tvdb_id,
        poster_url=None,
        raw=_build_media_raw(show.trakt_id, show.raw, "show"),
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
        raw=_build_episode_raw(episode.trakt_id, episode.raw),
    )
    db.add(item)
    await db.flush()
    return item


async def _find_media_item(
    db: AsyncSession,
    imdb_id: str | None,
    tmdb_id: str | None,
    trakt_id: str | None,
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
    if not item and trakt_id:
        result = await db.execute(
            select(MediaItem).where(
                MediaItem.media_type == media_type,
                MediaItem.raw["trakt_id"].as_string() == trakt_id,
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
    if not item and episode.trakt_id:
        result = await db.execute(
            select(EpisodeItem).where(
                EpisodeItem.raw["trakt_id"].as_string() == episode.trakt_id
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
    if movie.title and item.title.startswith("Trakt movie"):
        item.title = movie.title
    item.raw = _merge_media_raw(item.raw, movie.trakt_id, movie.raw)


def _apply_show_updates(item: MediaItem, show: ShowSummary) -> None:
    if show.imdb_id and not item.imdb_id:
        item.imdb_id = show.imdb_id
    if show.tmdb_id and not item.tmdb_id:
        item.tmdb_id = show.tmdb_id
    if show.tvdb_id and not item.tvdb_id:
        item.tvdb_id = show.tvdb_id
    if show.year is not None and item.year is None:
        item.year = show.year
    if show.title and item.title.startswith("Trakt show"):
        item.title = show.title
    item.raw = _merge_media_raw(item.raw, show.trakt_id, show.raw)


def _apply_episode_updates(item: EpisodeItem, episode: EpisodeSummary) -> None:
    if episode.imdb_id and not item.imdb_id:
        item.imdb_id = episode.imdb_id
    if episode.tmdb_id and not item.tmdb_id:
        item.tmdb_id = episode.tmdb_id
    if episode.tvdb_id and not item.tvdb_id:
        item.tvdb_id = episode.tvdb_id
    if episode.title and not item.title:
        item.title = episode.title
    item.raw = _merge_episode_raw(item.raw, episode.trakt_id, episode.raw)


def _build_media_raw(
    trakt_id: str | None, raw_payload: dict[str, Any], label: str
) -> dict[str, Any]:
    raw = {"source": "trakt", "type": label}
    if trakt_id:
        raw["trakt_id"] = trakt_id
    if raw_payload:
        raw["trakt"] = raw_payload
    return raw


def _merge_media_raw(
    existing: dict | None, trakt_id: str | None, raw_payload: dict[str, Any]
) -> dict:
    raw = existing if isinstance(existing, dict) else {}
    if trakt_id and not raw.get("trakt_id"):
        raw["trakt_id"] = trakt_id
    if raw_payload and not raw.get("trakt"):
        raw["trakt"] = raw_payload
    return raw


def _build_episode_raw(trakt_id: str | None, raw_payload: dict[str, Any]) -> dict:
    raw = {"source": "trakt", "type": "episode"}
    if trakt_id:
        raw["trakt_id"] = trakt_id
    if raw_payload:
        raw["trakt"] = raw_payload
    return raw


def _merge_episode_raw(
    existing: dict | None, trakt_id: str | None, raw_payload: dict[str, Any]
) -> dict:
    raw = existing if isinstance(existing, dict) else {}
    if trakt_id and not raw.get("trakt_id"):
        raw["trakt_id"] = trakt_id
    if raw_payload and not raw.get("trakt"):
        raw["trakt"] = raw_payload
    return raw


def _build_entry_key(
    history_id: str | None,
    imdb_id: str | None,
    tmdb_id: str | None,
    watched_at: datetime,
    prefix: str,
) -> str | None:
    if history_id:
        return f"history:{history_id}"
    if imdb_id:
        return f"{prefix}:imdb:{imdb_id}:{watched_at.date().isoformat()}"
    if tmdb_id:
        return f"{prefix}:tmdb:{tmdb_id}:{watched_at.date().isoformat()}"
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
        "source": "trakt",
        "entry_key": entry_key,
        "history_id": history_id,
        "watched_at": watched_at.isoformat(),
    }
    raw["ids"] = {
        "imdb": show_or_movie.imdb_id,
        "tmdb": show_or_movie.tmdb_id,
        "trakt": show_or_movie.trakt_id,
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
                "trakt": episode.trakt_id,
            },
        }
    if rating is not None:
        raw["rating"] = rating
    return raw


def _extract_movie_summary(entry: dict[str, Any]) -> MovieSummary | None:
    movie = entry.get("movie")
    if not isinstance(movie, dict):
        return None
    ids = _coerce_ids(movie.get("ids"))
    title = _coerce_str(movie.get("title")) or "Trakt movie"
    year = _coerce_int(movie.get("year"))
    return MovieSummary(
        title=title,
        year=year,
        imdb_id=_normalize_imdb_id(ids.get("imdb")),
        tmdb_id=_coerce_str(ids.get("tmdb")),
        trakt_id=_coerce_str(ids.get("trakt")),
        raw=_sanitize_trakt_payload(movie),
    )


def _extract_show_summary(entry: dict[str, Any]) -> ShowSummary | None:
    show = entry.get("show")
    if not isinstance(show, dict):
        return None
    ids = _coerce_ids(show.get("ids"))
    title = _coerce_str(show.get("title")) or "Trakt show"
    year = _coerce_int(show.get("year"))
    return ShowSummary(
        title=title,
        year=year,
        imdb_id=_normalize_imdb_id(ids.get("imdb")),
        tmdb_id=_coerce_str(ids.get("tmdb")),
        tvdb_id=_coerce_str(ids.get("tvdb")),
        trakt_id=_coerce_str(ids.get("trakt")),
        raw=_sanitize_trakt_payload(show),
    )


def _extract_episode_summary(entry: dict[str, Any]) -> EpisodeSummary | None:
    episode = entry.get("episode")
    if not isinstance(episode, dict):
        return None
    ids = _coerce_ids(episode.get("ids"))
    season = _coerce_int(episode.get("season"))
    number = _coerce_int(episode.get("number"))
    if season is None or number is None:
        return None
    return EpisodeSummary(
        season_number=season,
        episode_number=number,
        title=_coerce_str(episode.get("title")),
        imdb_id=_normalize_imdb_id(ids.get("imdb")),
        tmdb_id=_coerce_str(ids.get("tmdb")),
        tvdb_id=_coerce_str(ids.get("tvdb")),
        trakt_id=_coerce_str(ids.get("trakt")),
        raw=_sanitize_trakt_payload(episode),
    )


def _sanitize_trakt_payload(payload: dict[str, Any]) -> dict[str, Any]:
    keep: dict[str, Any] = {}
    for key in ("title", "year", "season", "number"):
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
    if isinstance(value, str):
        cleaned = value.strip()
        if cleaned.isdigit():
            return int(cleaned)
    return None


def _parse_datetime(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    cleaned = value.strip()
    if not cleaned:
        return None
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


async def _ensure_trakt_access_token(
    db: AsyncSession,
    integration_id: str,
    secret_data: dict[str, object],
    client: TraktClient,
) -> str:
    access_token = secret_data.get("access_token")
    refresh_token = secret_data.get("refresh_token")
    if not isinstance(access_token, str) or not access_token:
        raise TraktError("Trakt access token is missing", status_code=401)
    if not isinstance(refresh_token, str) or not refresh_token:
        raise TraktError("Trakt refresh token is missing", status_code=401)
    expires_at = parse_expires_at(secret_data.get("expires_at"))
    if not is_token_expired(expires_at):
        return access_token
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
