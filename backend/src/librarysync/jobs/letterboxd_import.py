from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from librarysync.connectors.services.letterboxd import (
    DEFAULT_LETTERBOXD_API_BASE_URL,
    LetterboxdClient,
    LetterboxdError,
    extract_member_id,
    has_required_letterboxd_fields,
)
from librarysync.connectors.services.trakt import has_required_trakt_fields
from librarysync.config import settings
from librarysync.core.integrations import load_integration_with_secrets
from librarysync.core.ratings import coerce_star_rating
from librarysync.db.models import (
    Integration,
    MediaItem,
    OutboxJob,
    WatchedItem,
    WatchEvent,
    WatchSync,
)
from librarysync.db.session import SessionLocal, init_session_factory


LOOKBACK_HOURS = 24 * 7
MAX_PAGES = 6
PER_PAGE = 20
IMDB_ID_RE = re.compile(r"(tt\d{3,10})", re.IGNORECASE)
TMDB_URL_RE = re.compile(r"/(?:movie|film|tv)/(\d+)", re.IGNORECASE)
logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class FilmSummary:
    film_id: str | None
    title: str
    year: int | None
    imdb_id: str | None
    tmdb_id: str | None
    poster_url: str | None
    raw: dict[str, Any]


async def process_letterboxd_imports_once(
    lookback_hours: int = LOOKBACK_HOURS,
    per_page: int = PER_PAGE,
    max_pages: int = MAX_PAGES,
) -> int:
    init_session_factory()
    async with SessionLocal() as db:
        result = await db.execute(
            select(Integration).where(Integration.provider == "letterboxd")
        )
        integrations = result.scalars().all()
        if not integrations:
            return 0
        total_imported = 0
        for integration in integrations:
            total_imported += await _import_for_integration(
                db, integration, lookback_hours, per_page, max_pages
            )
        return total_imported


async def _import_for_integration(
    db: AsyncSession,
    integration: Integration,
    lookback_hours: int,
    per_page: int,
    max_pages: int,
) -> int:
    integration, secret_data = await load_integration_with_secrets(
        db, integration.user_id, "letterboxd"
    )
    if not integration or not secret_data:
        return 0
    if not has_required_letterboxd_fields(secret_data):
        return 0
    api_base_url = DEFAULT_LETTERBOXD_API_BASE_URL
    if integration.config and integration.config.get("api_base_url"):
        api_base_url = str(integration.config["api_base_url"])
    cookies = _parse_cookies(secret_data.get("cookies"))
    member_id: str | None = None
    if integration.config:
        raw_member_id = integration.config.get("member_id")
        if raw_member_id is not None:
            member_id = str(raw_member_id).strip() or None
    client = LetterboxdClient(
        api_base_url=api_base_url,
        client_id=str(secret_data.get("client_id")),
        client_secret=str(secret_data.get("client_secret")),
        refresh_token=str(secret_data.get("refresh_token")),
        cookies=cookies,
    )
    now = datetime.now(timezone.utc)
    since = now - timedelta(hours=lookback_hours)
    month_filters = _month_range(since.date(), now.date())
    try:
        access_token = await client.refresh_access_token()
        if not member_id:
            try:
                me_payload = await client.fetch_me(access_token=access_token)
            except LetterboxdError as exc:
                logger.info(
                    "Letterboxd /me lookup failed for user %s: %s",
                    integration.user_id,
                    exc,
                )
            else:
                member_id = extract_member_id(me_payload)
                if member_id:
                    config = dict(integration.config or {})
                    config["member_id"] = member_id
                    integration.config = config
                    db.add(integration)
                    await db.commit()
        entries: list[dict[str, Any]] = []
        for year, month in month_filters:
            entries.extend(
                await client.fetch_recent_log_entries(
                    access_token,
                    since,
                    member_id=member_id,
                    per_page=per_page,
                    max_pages=max_pages,
                    year=year,
                    month=month,
                )
            )
    except LetterboxdError as exc:
        logger.warning(
            "Letterboxd import failed for user %s: %s", integration.user_id, exc
        )
        return 0

    imported = 0
    for entry in entries:
        try:
            if await _import_entry(db, integration.user_id, entry):
                imported += 1
        except Exception:
            logger.exception(
                "Letterboxd entry import failed for user %s", integration.user_id
            )
            await db.rollback()
    if imported:
        logger.info(
            "Imported %s Letterboxd entries for user %s",
            imported,
            integration.user_id,
        )
    return imported


async def _import_entry(db: AsyncSession, user_id: str, entry: dict[str, Any]) -> bool:
    entry_id = _extract_entry_id(entry)
    watched_at = _extract_entry_watched_at(entry)
    if watched_at is None:
        watched_at = datetime.now(timezone.utc)
    film = _extract_film_summary(entry)
    if not film:
        return False
    entry_key = _build_entry_key(entry_id, film.film_id, watched_at)
    if not entry_key:
        return False
    if await _entry_already_imported(db, user_id, entry_key):
        if await _enqueue_trakt_sync_for_existing_import(
            db, user_id, entry_key, watched_at
        ):
            await db.commit()
        return False

    media_item = await _get_or_create_media_item(db, film)
    if not media_item:
        return False
    rating = coerce_star_rating(_extract_rating(entry))
    watched = WatchedItem(
        user_id=user_id,
        media_item_id=media_item.id,
        episode_item_id=None,
        watched_at=watched_at,
        rating=rating,
        source="letterboxd",
    )
    is_rewatch = bool(_extract_rewatch(entry))
    event = WatchEvent(
        user_id=user_id,
        media_item_id=media_item.id,
        episode_item_id=None,
        event_type="letterboxd_imported",
        occurred_at=watched_at,
        raw=_build_event_raw(entry, entry_key, entry_id, film, watched_at, rating),
    )
    db.add_all([watched, event])
    await db.flush()
    watch_sync = WatchSync(
        user_id=user_id,
        watched_item_id=watched.id,
        provider="letterboxd",
        status="synced_from_letterboxd",
        is_rewatch=is_rewatch,
        external_id=entry_id,
        last_synced_at=datetime.now(timezone.utc),
    )
    db.add(watch_sync)
    await _enqueue_trakt_sync_from_import(
        db,
        user_id,
        watched,
        media_item,
        watched_at,
        is_rewatch,
        rating,
    )
    await db.commit()
    return True


async def _entry_already_imported(
    db: AsyncSession, user_id: str, entry_key: str
) -> bool:
    result = await db.execute(
        select(WatchEvent.id).where(
            WatchEvent.user_id == user_id,
            WatchEvent.event_type == "letterboxd_imported",
            WatchEvent.raw["entry_key"].as_string() == entry_key,
        )
    )
    return result.scalars().first() is not None


async def _enqueue_trakt_sync_for_existing_import(
    db: AsyncSession,
    user_id: str,
    entry_key: str,
    watched_at: datetime,
) -> bool:
    result = await db.execute(
        select(WatchEvent).where(
            WatchEvent.user_id == user_id,
            WatchEvent.event_type == "letterboxd_imported",
            WatchEvent.raw["entry_key"].as_string() == entry_key,
        )
    )
    event = result.scalars().first()
    if not event or not event.media_item_id:
        return False
    result = await db.execute(
        select(WatchedItem).where(
            WatchedItem.user_id == user_id,
            WatchedItem.media_item_id == event.media_item_id,
            WatchedItem.watched_at == watched_at,
        )
    )
    watched = result.scalars().first()
    if not watched:
        result = await db.execute(
            select(WatchedItem).where(
                WatchedItem.user_id == user_id,
                WatchedItem.media_item_id == event.media_item_id,
                func.date(WatchedItem.watched_at) == watched_at.date(),
            )
        )
        watched = result.scalars().first()
    if not watched:
        return False
    result = await db.execute(
        select(WatchSync).where(
            WatchSync.watched_item_id == watched.id,
            WatchSync.provider == "trakt",
        )
    )
    if result.scalars().first():
        return False
    result = await db.execute(
        select(MediaItem).where(MediaItem.id == event.media_item_id)
    )
    media_item = result.scalars().first()
    if not media_item:
        return False
    raw = event.raw if isinstance(event.raw, dict) else {}
    is_rewatch = bool(raw.get("rewatch"))
    return await _enqueue_trakt_sync_from_import(
        db,
        user_id,
        watched,
        media_item,
        watched.watched_at,
        is_rewatch,
        watched.rating,
    )


async def _get_or_create_media_item(
    db: AsyncSession, film: FilmSummary
) -> MediaItem | None:
    item = await _find_media_item(db, film)
    if item:
        _apply_media_updates(item, film)
        return item
    if not film.imdb_id and not film.tmdb_id and not film.film_id:
        return None
    item = MediaItem(
        media_type="movie",
        title=film.title,
        year=film.year,
        imdb_id=film.imdb_id,
        tmdb_id=film.tmdb_id,
        poster_url=film.poster_url,
        raw=_build_media_raw(film),
    )
    db.add(item)
    await db.flush()
    return item


async def _find_media_item(db: AsyncSession, film: FilmSummary) -> MediaItem | None:
    item: MediaItem | None = None
    if film.imdb_id:
        result = await db.execute(
            select(MediaItem).where(MediaItem.imdb_id == film.imdb_id)
        )
        item = result.scalars().first()
    if film.tmdb_id:
        result = await db.execute(
            select(MediaItem).where(
                MediaItem.tmdb_id == film.tmdb_id, MediaItem.media_type == "movie"
            )
        )
        tmdb_item = result.scalars().first()
        if item and tmdb_item and item.id != tmdb_item.id:
            logger.warning(
                "Letterboxd import found conflicting media items for %s/%s",
                film.imdb_id,
                film.tmdb_id,
            )
            return item
        if not item:
            item = tmdb_item
    if not item and film.film_id:
        result = await db.execute(
            select(MediaItem).where(
                MediaItem.media_type == "movie",
                MediaItem.raw["letterboxd_film_id"].as_string() == film.film_id,
            )
        )
        item = result.scalars().first()
    return item


def _apply_media_updates(item: MediaItem, film: FilmSummary) -> None:
    if film.imdb_id and not item.imdb_id:
        item.imdb_id = film.imdb_id
    if film.tmdb_id and not item.tmdb_id:
        item.tmdb_id = film.tmdb_id
    if film.year is not None and item.year is None:
        item.year = film.year
    if film.poster_url and not item.poster_url:
        item.poster_url = film.poster_url
    if film.title and item.title.startswith("Letterboxd film"):
        item.title = film.title
    item.raw = _merge_media_raw(item.raw, film)


def _merge_media_raw(existing: dict | None, film: FilmSummary) -> dict:
    raw = existing if isinstance(existing, dict) else {}
    if film.film_id and not raw.get("letterboxd_film_id"):
        raw["letterboxd_film_id"] = film.film_id
    if film.raw and not raw.get("letterboxd"):
        raw["letterboxd"] = film.raw
    return raw


def _build_media_raw(film: FilmSummary) -> dict:
    raw = {"source": "letterboxd"}
    if film.film_id:
        raw["letterboxd_film_id"] = film.film_id
    if film.raw:
        raw["letterboxd"] = film.raw
    return raw


def _build_entry_key(
    entry_id: str | None, film_id: str | None, watched_at: datetime
) -> str | None:
    if entry_id:
        return f"entry:{entry_id}"
    if film_id:
        return f"film:{film_id}:{watched_at.date().isoformat()}"
    return None


def _build_event_raw(
    entry: dict[str, Any],
    entry_key: str,
    entry_id: str | None,
    film: FilmSummary,
    watched_at: datetime,
    rating: float | None,
) -> dict[str, Any]:
    raw: dict[str, Any] = {
        "source": "letterboxd",
        "entry_key": entry_key,
        "entry_id": entry_id,
        "film_id": film.film_id,
        "imdb_id": film.imdb_id,
        "tmdb_id": film.tmdb_id,
        "watched_at": watched_at.isoformat(),
    }
    rewatch = _extract_rewatch(entry)
    if rewatch is not None:
        raw["rewatch"] = rewatch
    if rating is not None:
        raw["rating"] = rating
    sanitized = _sanitize_entry(entry)
    if sanitized:
        raw["entry"] = sanitized
    return raw


def _parse_cookies(value: object) -> dict[str, str] | None:
    if isinstance(value, dict):
        return {str(key): str(val) for key, val in value.items() if val is not None}
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            return None
        if isinstance(parsed, dict):
            return {str(key): str(val) for key, val in parsed.items() if val is not None}
    return None


def _extract_entry_id(entry: dict[str, Any]) -> str | None:
    for key in ("id", "entryId", "diaryEntryId", "logEntryId"):
        value = entry.get(key)
        if isinstance(value, (str, int)):
            return str(value)
    nested = entry.get("entry")
    if isinstance(nested, dict):
        for key in ("id", "entryId", "logEntryId"):
            value = nested.get(key)
            if isinstance(value, (str, int)):
                return str(value)
    return None


def _extract_entry_watched_at(entry: dict[str, Any]) -> datetime | None:
    for candidate in (entry, entry.get("entry")):
        if not isinstance(candidate, dict):
            continue
        for key in (
            "diaryDate",
            "watchedDate",
            "logDate",
            "watchedAt",
            "loggedAt",
            "createdAt",
            "updatedAt",
            "timestamp",
            "date",
        ):
            parsed = _parse_datetime(candidate.get(key))
            if parsed:
                return parsed
        diary = candidate.get("diaryDetails") or candidate.get("diary")
        if isinstance(diary, dict):
            for key in ("diaryDate", "date", "watchedDate"):
                parsed = _parse_datetime(diary.get(key))
                if parsed:
                    return parsed
    return None


def _extract_film_summary(entry: dict[str, Any]) -> FilmSummary | None:
    film_payload = _extract_film_payload(entry)
    film_id = _coerce_str(
        _first_match(film_payload, ("id", "filmId", "film_id"))
        or _first_match(entry, ("filmId", "film_id"))
    )
    title = _coerce_str(_first_match(film_payload, ("name", "title", "filmName")))
    imdb_id = _extract_imdb_id(film_payload) or _extract_imdb_id(entry)
    tmdb_id = _extract_tmdb_id(film_payload) or _extract_tmdb_id(entry)
    year = _coerce_int(_first_match(film_payload, ("releaseYear", "year")))
    poster_url = _extract_poster_url(film_payload)
    raw = _sanitize_film_payload(film_payload)
    if not title:
        if film_id:
            title = f"Letterboxd film {film_id}"
        else:
            title = "Letterboxd film"
    if not imdb_id and not tmdb_id and not film_id:
        return None
    return FilmSummary(
        film_id=film_id,
        title=title,
        year=year,
        imdb_id=imdb_id,
        tmdb_id=tmdb_id,
        poster_url=poster_url,
        raw=raw,
    )


def _extract_film_payload(entry: dict[str, Any]) -> dict[str, Any]:
    for key in ("film", "movie", "item", "filmDetails"):
        value = entry.get(key)
        if isinstance(value, dict):
            return value
    return {}


def _extract_imdb_id(payload: dict[str, Any]) -> str | None:
    for key in ("imdbId", "imdb_id", "imdbID"):
        value = payload.get(key)
        imdb_id = _normalize_imdb_id(value)
        if imdb_id:
            return imdb_id
    links = payload.get("links") or payload.get("externalLinks") or payload.get("external")
    if isinstance(links, dict):
        for key in ("imdb", "imdbId", "imdb_id"):
            imdb_id = _normalize_imdb_id(links.get(key))
            if imdb_id:
                return imdb_id
        for entry in links.values():
            imdb_id = _normalize_imdb_id(entry)
            if imdb_id:
                return imdb_id
    external_ids = payload.get("externalIds") or payload.get("external_ids")
    if isinstance(external_ids, list):
        for entry in external_ids:
            if not isinstance(entry, dict):
                continue
            imdb_id = _normalize_imdb_id(entry.get("id") or entry.get("value"))
            if imdb_id:
                return imdb_id
    return None


def _extract_tmdb_id(payload: dict[str, Any]) -> str | None:
    for key in ("tmdbId", "tmdb_id"):
        tmdb_id = _normalize_tmdb_id(payload.get(key))
        if tmdb_id:
            return tmdb_id
    links = payload.get("links") or payload.get("externalLinks") or payload.get("external")
    if isinstance(links, dict):
        for key in ("tmdb", "tmdbId", "tmdb_id"):
            tmdb_id = _normalize_tmdb_id(links.get(key))
            if tmdb_id:
                return tmdb_id
        for entry in links.values():
            tmdb_id = _normalize_tmdb_id(entry)
            if tmdb_id:
                return tmdb_id
    return None


def _extract_poster_url(payload: dict[str, Any]) -> str | None:
    for key in ("posterUrl", "poster_url", "poster", "image", "imageUrl"):
        value = payload.get(key)
        url = _coerce_url(value)
        if url:
            return url
    poster = payload.get("poster")
    if isinstance(poster, dict):
        url = _coerce_url(poster.get("url"))
        if url:
            return url
        sizes = poster.get("sizes")
        if isinstance(sizes, dict):
            for key in ("large", "medium", "small"):
                url = _coerce_url(sizes.get(key))
                if url:
                    return url
        if isinstance(sizes, list):
            for entry in sizes:
                if not isinstance(entry, dict):
                    continue
                url = _coerce_url(entry.get("url") or entry.get("src"))
                if url:
                    return url
    return None


def _extract_rewatch(entry: dict[str, Any]) -> bool | None:
    for candidate in (entry, entry.get("entry")):
        if not isinstance(candidate, dict):
            continue
        value = candidate.get("rewatch")
        if isinstance(value, bool):
            return value
        diary = candidate.get("diaryDetails") or candidate.get("diary")
        if isinstance(diary, dict) and isinstance(diary.get("rewatch"), bool):
            return bool(diary.get("rewatch"))
    return None


def _extract_rating(entry: dict[str, Any]) -> float | None:
    for candidate in (entry, entry.get("entry")):
        if not isinstance(candidate, dict):
            continue
        value = candidate.get("rating")
        rating = _coerce_float(value)
        if rating is not None:
            return rating
    return None


def _sanitize_entry(entry: dict[str, Any]) -> dict[str, Any] | None:
    if not isinstance(entry, dict):
        return None
    keep: dict[str, Any] = {}
    for key in (
        "id",
        "entryId",
        "diaryEntryId",
        "logEntryId",
        "filmId",
        "diaryDate",
        "watchedDate",
        "date",
        "createdAt",
        "updatedAt",
        "rewatch",
        "rating",
    ):
        if key in entry:
            keep[key] = entry[key]
    film_payload = _extract_film_payload(entry)
    if film_payload:
        keep["film"] = _sanitize_film_payload(film_payload)
    return keep or None


def _sanitize_film_payload(payload: dict[str, Any]) -> dict[str, Any]:
    keep: dict[str, Any] = {}
    for key in ("id", "name", "title", "releaseYear", "year", "tmdbId", "imdbId"):
        if key in payload:
            keep[key] = payload[key]
    links = payload.get("links")
    if isinstance(links, dict):
        keep["links"] = {
            key: value
            for key, value in links.items()
            if isinstance(value, (str, int))
        }
    return keep


def _first_match(payload: dict[str, Any], keys: tuple[str, ...]) -> Any:
    for key in keys:
        if key in payload:
            return payload.get(key)
    return None


def _coerce_str(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, str):
        cleaned = value.strip()
        return cleaned or None
    if isinstance(value, (int, float)):
        return str(value)
    return None


def _coerce_int(value: Any) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _coerce_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _coerce_url(value: Any) -> str | None:
    if isinstance(value, str):
        cleaned = value.strip()
        return cleaned or None
    return None


def _normalize_imdb_id(value: Any) -> str | None:
    cleaned = _coerce_str(value)
    if not cleaned:
        return None
    match = IMDB_ID_RE.search(cleaned)
    if match:
        return match.group(1).lower()
    if cleaned.lower().startswith("tt") and cleaned[2:].isdigit():
        return cleaned.lower()
    return None


def _normalize_tmdb_id(value: Any) -> str | None:
    cleaned = _coerce_str(value)
    if not cleaned:
        return None
    if cleaned.lower().startswith("tmdb:"):
        cleaned = cleaned.split(":", 1)[1].strip()
    if cleaned.isdigit():
        return cleaned
    match = TMDB_URL_RE.search(cleaned)
    if match:
        return match.group(1)
    return None


def _parse_datetime(value: Any) -> datetime | None:
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
    try:
        parsed_date = datetime.fromisoformat(cleaned.split("T")[0])
    except ValueError:
        return None
    return parsed_date.replace(tzinfo=timezone.utc)


def _month_range(start: date, end: date) -> list[tuple[int, int]]:
    if start > end:
        start, end = end, start
    months: list[tuple[int, int]] = []
    year = start.year
    month = start.month
    while (year, month) <= (end.year, end.month):
        months.append((year, month))
        if month == 12:
            year += 1
            month = 1
        else:
            month += 1
    return months


async def _enqueue_trakt_sync_from_import(
    db: AsyncSession,
    user_id: str,
    watched: WatchedItem,
    media_item: MediaItem,
    watched_at: datetime,
    is_rewatch: bool,
    rating: float | None,
) -> bool:
    if not settings.trakt_client_id or not settings.trakt_client_secret:
        return False
    payload = _build_trakt_payload(media_item, watched_at, rating)
    if not payload:
        return False
    integration, secret_data = await load_integration_with_secrets(
        db, user_id, "trakt"
    )
    if not integration or not secret_data:
        return False
    if not has_required_trakt_fields(secret_data):
        return False
    result = await db.execute(
        select(WatchSync).where(
            WatchSync.watched_item_id == watched.id,
            WatchSync.provider == "trakt",
        )
    )
    if result.scalars().first():
        return False

    same_day_duplicate = await _has_same_day_watch(
        db,
        user_id,
        media_item.id,
        watched_at,
        watched.id,
    )
    now = datetime.now(timezone.utc)
    watch_status = "pending"
    if same_day_duplicate and rating is None:
        watch_status = "assumed_tracked"

    watch_sync = WatchSync(
        user_id=user_id,
        watched_item_id=watched.id,
        provider="trakt",
        status=watch_status,
        is_rewatch=is_rewatch,
    )
    if watch_status == "assumed_tracked":
        watch_sync.last_synced_at = now
    db.add(watch_sync)
    await db.flush()

    payload["watch_sync_id"] = watch_sync.id
    payload["watched_item_id"] = watched.id
    if watch_status != "assumed_tracked" and not same_day_duplicate:
        job = OutboxJob(
            user_id=user_id,
            target_provider="trakt",
            job_type="push_watched",
            payload=payload,
            status="pending",
        )
        db.add(job)
    if rating is not None:
        rating_payload = dict(payload)
        rating_payload["rating"] = rating
        rating_job = OutboxJob(
            user_id=user_id,
            target_provider="trakt",
            job_type="push_rating",
            payload=rating_payload,
            status="pending",
        )
        db.add(rating_job)
    return True


async def _has_same_day_watch(
    db: AsyncSession,
    user_id: str,
    media_item_id: str,
    watched_at: datetime,
    exclude_watched_id: str | None = None,
) -> bool:
    target_date = watched_at.date()
    query = select(WatchedItem.id).where(
        WatchedItem.user_id == user_id,
        WatchedItem.media_item_id == media_item_id,
        func.date(WatchedItem.watched_at) == target_date,
    )
    if exclude_watched_id:
        query = query.where(WatchedItem.id != exclude_watched_id)
    query = query.limit(1)
    result = await db.execute(query)
    return result.scalars().first() is not None


def _build_trakt_payload(
    media_item: MediaItem,
    watched_at: datetime,
    rating: float | None,
) -> dict[str, object] | None:
    if media_item.media_type != "movie":
        return None
    movie_ids = _collect_trakt_ids(
        media_item.imdb_id, media_item.tmdb_id, media_item.tvdb_id
    )
    if not movie_ids:
        return None
    payload: dict[str, object] = {
        "media_type": "movie",
        "movie_ids": movie_ids,
        "watched_at": watched_at.isoformat(),
    }
    if rating is not None:
        payload["rating"] = rating
    return payload


def _collect_trakt_ids(
    imdb_id: str | None, tmdb_id: str | None, tvdb_id: str | None
) -> dict[str, object]:
    ids: dict[str, object] = {}
    if imdb_id:
        ids["imdb"] = imdb_id.lower()
    if tmdb_id:
        ids["tmdb"] = tmdb_id
    if tvdb_id:
        ids["tvdb"] = tvdb_id
    return ids
