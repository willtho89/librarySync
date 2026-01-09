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
from librarysync.connectors.services.letterboxd import (
    DEFAULT_LETTERBOXD_API_BASE_URL,
    LetterboxdClient,
    LetterboxdError,
    extract_member_id,
    extract_member_name,
    has_required_letterboxd_fields,
)
from librarysync.connectors.services.letterboxd import (
    is_token_expired as is_letterboxd_token_expired,
)
from librarysync.connectors.services.letterboxd import (
    parse_expires_at as parse_letterboxd_expires_at,
)
from librarysync.connectors.services.letterboxd import (
    token_to_secret_payload as letterboxd_token_to_secret_payload,
)
from librarysync.core.integrations import load_integration_with_secrets
from librarysync.core.ratings import coerce_star_rating
from librarysync.core.security import encrypt_value
from librarysync.core.watchlist_links import parse_letterboxd_list_urls
from librarysync.core.watchlist_sources import (
    LEGACY_LIST_SOURCE_TYPE,
    PERSONAL_SOURCE_TYPE,
    URL_SOURCE_TYPE,
    ensure_personal_watchlist_source,
    list_watchlist_sources,
    reconcile_watchlist_source,
)
from librarysync.db.models import (
    Integration,
    IntegrationSecret,
    MediaItem,
    WatchlistSource,
)
from librarysync.jobs.import_base import ImportContext, ImportResult, ImportStrategy
from librarysync.jobs.import_pipeline import (
    ImportCandidate,
    ImportItems,
    process_import_candidates,
)
from librarysync.jobs.import_utils import chunked
from librarysync.jobs.watchlist_pipeline import (
    WatchlistCandidate,
    process_watchlist_candidates,
)

LOOKBACK_DAYS = settings.history_lookback_days
MAX_PAGES = 6
PER_PAGE = 20
FULL_HISTORY_START = datetime(1900, 1, 1, tzinfo=timezone.utc)
FULL_HISTORY_EMPTY_MONTHS = 3
IMDB_ID_RE = re.compile(r"(tt\d{3,10})", re.IGNORECASE)
TMDB_URL_RE = re.compile(r"/(?:movie|film|tv)/(\d+)", re.IGNORECASE)
ENTRY_KEY_BATCH_SIZE = 200
WATCHLIST_PER_PAGE = 50
WATCHLIST_MAX_PAGES = 10
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


class LetterboxdImportStrategy(ImportStrategy):
    provider = "letterboxd"

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
            context.now,
        )


async def _import_for_integration(
    db: AsyncSession,
    integration: Integration,
    lookback_days: int,
    per_page: int,
    max_pages: int,
    now: datetime,
) -> ImportResult:
    integration, secret_data = await load_integration_with_secrets(
        db, integration.user_id, "letterboxd"
    )
    if not integration or not secret_data:
        return ImportResult(imported=0, attempted=False)
    if not has_required_letterboxd_fields(secret_data):
        return ImportResult(imported=0, attempted=False)
    try:
        client, access_token, member_id = await _build_letterboxd_context(
            db, integration, secret_data
        )
        full_history = lookback_days < 0
        since = _select_letterboxd_since(now, lookback_days)
        entries = await client.get_history(
            access_token,
            since=since,
            member_id=member_id,
            per_page=per_page,
            max_pages=max_pages,
            reverse=full_history,
            stop_after_empty_months=FULL_HISTORY_EMPTY_MONTHS if full_history else None,
        )
    except LetterboxdError as exc:
        logger.warning(
            "Letterboxd import failed for user %s: %s", integration.user_id, exc
        )
        return ImportResult(imported=0, attempted=True)

    imported = 0
    for batch in chunked(entries, ENTRY_KEY_BATCH_SIZE):
        candidates: list[ImportCandidate] = []
        for entry in batch:
            try:
                candidate = _build_candidate(entry, now)
                if candidate:
                    candidates.append(candidate)
            except Exception:
                logger.exception(
                    "Letterboxd entry import failed for user %s", integration.user_id
                )
        if not candidates:
            continue
        imported += await process_import_candidates(
            db,
            integration.user_id,
            "letterboxd",
            candidates,
            now=now,
        )
    if imported:
        logger.info(
            "Imported %s Letterboxd entries for user %s",
            imported,
            integration.user_id,
        )
    watchlist_imported = await _import_watchlist_for_integration(
        db,
        integration,
        client,
        access_token,
        member_id,
        now,
    )
    if watchlist_imported:
        logger.info(
            "Imported %s Letterboxd watchlist items for user %s",
            watchlist_imported,
            integration.user_id,
        )
    return ImportResult(imported=imported + watchlist_imported, attempted=True)


async def _import_watchlist_for_integration(
    db: AsyncSession,
    integration: Integration,
    client: LetterboxdClient,
    access_token: str,
    member_id: str | None,
    now: datetime,
    sources: list[WatchlistSource] | None = None,
) -> int:
    await ensure_personal_watchlist_source(
        db,
        user_id=integration.user_id,
        provider="letterboxd",
        name="Letterboxd watchlist",
    )
    if sources is None:
        sources = await list_watchlist_sources(
            db, integration.user_id, provider="letterboxd"
        )
    if not sources:
        return 0
    imported = 0
    candidates: list[WatchlistCandidate] = []
    for source in sources:
        if source.source_type == PERSONAL_SOURCE_TYPE:
            if not member_id:
                logger.warning(
                    "Letterboxd watchlist skipped (missing member id) for user %s",
                    integration.user_id,
                )
                continue
            entries = await client.get_watchlist(
                access_token,
                member_id=member_id,
                per_page=WATCHLIST_PER_PAGE,
                max_pages=WATCHLIST_MAX_PAGES,
            )
            for entry in entries:
                candidate = _build_watchlist_candidate(entry)
                if candidate:
                    candidates.append(candidate)
            if candidates:
                imported += await process_watchlist_candidates(
                    db,
                    integration.user_id,
                    "letterboxd",
                    source,
                    candidates,
                    now=now,
                )
                candidates = []
            elif not entries:
                await reconcile_watchlist_source(
                    db,
                    source,
                    now=now,
                    seen_item_ids=[],
                )
            continue
        if source.source_type not in {URL_SOURCE_TYPE, LEGACY_LIST_SOURCE_TYPE} or not source.url:
            continue
        list_refs = parse_letterboxd_list_urls([source.url])
        if not list_refs:
            logger.warning(
                "Letterboxd watchlist URL skipped (invalid) for user %s: %s",
                integration.user_id,
                source.url,
            )
            continue
        list_ref = list_refs[0]
        try:
            list_entries = await client.get_list_entries(
                access_token,
                list_ref.username,
                list_ref.slug,
                per_page=WATCHLIST_PER_PAGE,
                max_pages=WATCHLIST_MAX_PAGES,
            )
        except LetterboxdError as exc:
            logger.warning(
                "Letterboxd list fetch failed for user %s (%s): %s",
                integration.user_id,
                list_ref.url,
                exc,
            )
            continue
        if not list_entries:
            await reconcile_watchlist_source(
                db,
                source,
                now=now,
                seen_item_ids=[],
            )
            continue
        list_context = {"name": list_ref.name, "url": list_ref.url, "type": "list"}
        for entry in list_entries:
            candidate = _build_watchlist_candidate(entry, list_context=list_context)
            if candidate:
                candidates.append(candidate)
        if candidates:
            imported += await process_watchlist_candidates(
                db,
                integration.user_id,
                "letterboxd",
                source,
                candidates,
                now=now,
            )
            candidates = []
    return imported


async def import_watchlist_source(
    db: AsyncSession,
    source: WatchlistSource,
    *,
    now: datetime | None = None,
) -> int:
    if source.provider != "letterboxd":
        raise ValueError("Watchlist source is not a Letterboxd list")
    integration, secret_data = await load_integration_with_secrets(
        db, source.user_id, "letterboxd"
    )
    if not integration or integration.status == "disconnected":
        raise ValueError("Letterboxd integration is not connected")
    if not secret_data or not has_required_letterboxd_fields(secret_data):
        raise ValueError("Letterboxd credentials are incomplete")
    if now is None:
        now = datetime.now(timezone.utc)
    client, access_token, member_id = await _build_letterboxd_context(
        db, integration, secret_data
    )
    return await _import_watchlist_for_integration(
        db,
        integration,
        client,
        access_token,
        member_id,
        now,
        sources=[source],
    )


async def _build_letterboxd_context(
    db: AsyncSession,
    integration: Integration,
    secret_data: dict[str, object],
) -> tuple[LetterboxdClient, str, str | None]:
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
    access_token = await _ensure_letterboxd_access_token(
        db, integration.id, secret_data, client
    )
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
            member_name = extract_member_name(me_payload)
            if member_id:
                config = dict(integration.config or {})
                config["member_id"] = member_id
                if member_name:
                    config["member_name"] = member_name
                integration.config = config
                db.add(integration)
                await db.commit()
    return client, access_token, member_id


async def _ensure_letterboxd_access_token(
    db: AsyncSession,
    integration_id: str,
    secret_data: dict[str, object],
    client: LetterboxdClient,
) -> str:
    access_token = secret_data.get("access_token")
    expires_at = parse_letterboxd_expires_at(secret_data.get("expires_at"))
    if isinstance(access_token, str) and access_token and not is_letterboxd_token_expired(
        expires_at
    ):
        return access_token
    token = await client.refresh_access_token_payload()
    updated = dict(secret_data)
    updated.update(letterboxd_token_to_secret_payload(token))
    await _save_integration_secret(db, integration_id, updated)
    await db.commit()
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


def _select_letterboxd_since(now: datetime, lookback_days: int) -> datetime:
    if lookback_days < 0:
        return FULL_HISTORY_START
    return now - timedelta(days=lookback_days)


def _build_candidate(entry: dict[str, Any], now: datetime) -> ImportCandidate | None:
    entry_id = _extract_entry_id(entry)
    watched_at = _extract_entry_watched_at(entry) or now
    film = _extract_film_summary(entry)
    if not film:
        return None
    entry_key = _build_entry_key(entry_id, film.film_id, watched_at)
    if not entry_key:
        return None
    rating = coerce_star_rating(_extract_rating(entry))
    is_rewatch = bool(_extract_rewatch(entry))

    async def _build_items(db: AsyncSession) -> ImportItems:
        media_item = await _get_or_create_media_item(db, film)
        return ImportItems(media_item=media_item, episode_item=None, show_item=None)

    return ImportCandidate(
        entry_key=entry_key,
        watched_at=watched_at,
        media_type="movie",
        raw=_build_event_raw(entry, entry_key, entry_id, film, watched_at, rating),
        rating=rating,
        external_id=entry_id,
        blacklist_ids=None,
        blacklist_enabled=False,
        is_rewatch=is_rewatch,
        build_items=_build_items,
    )


def _build_watchlist_candidate(
    entry: dict[str, Any],
    *,
    list_context: dict[str, Any] | None = None,
) -> WatchlistCandidate | None:
    entry_id = _extract_watchlist_entry_id(entry)
    film = _extract_film_summary(entry)
    if not film:
        return None
    entry_key = _build_watchlist_entry_key(entry_id, film)
    ids: dict[str, str] = {}
    if film.imdb_id:
        ids["imdb_id"] = film.imdb_id
    if film.tmdb_id:
        ids["tmdb_id"] = film.tmdb_id
    if film.film_id:
        ids["letterboxd_film_id"] = film.film_id
    raw: dict[str, Any] = {"source": "letterboxd"}
    if entry_id:
        raw["entry_id"] = entry_id
    if film.raw:
        raw["film"] = film.raw
    if list_context:
        raw["list"] = list_context
    return WatchlistCandidate(
        entry_key=entry_key,
        media_type="movie",
        ids=ids,
        title=film.title,
        year=film.year,
        poster_url=film.poster_url,
        raw=raw,
        source="letterboxd",
        external_item_id=entry_id,
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


def _build_watchlist_entry_key(entry_id: str | None, film: FilmSummary) -> str | None:
    if entry_id:
        return f"watchlist:{entry_id}"
    if film.film_id:
        return f"watchlist:film:{film.film_id}"
    if film.imdb_id:
        return f"watchlist:imdb:{film.imdb_id}"
    if film.tmdb_id:
        return f"watchlist:tmdb:{film.tmdb_id}"
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


def _extract_watchlist_entry_id(entry: dict[str, Any]) -> str | None:
    entry_id = _extract_entry_id(entry)
    if entry_id:
        return entry_id
    for key in ("watchlistId", "listItemId", "list_item_id", "itemId"):
        value = entry.get(key)
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
    if isinstance(entry, dict) and (entry.get("name") or entry.get("title")):
        return entry
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
    if isinstance(links, list):
        for entry in links:
            if not isinstance(entry, dict):
                continue
            if entry.get("type") != "imdb":
                continue
            imdb_id = _normalize_imdb_id(entry.get("id") or entry.get("url"))
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
