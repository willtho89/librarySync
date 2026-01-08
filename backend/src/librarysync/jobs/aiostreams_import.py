from __future__ import annotations

import hashlib
import logging
import re
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from typing import Any

from PTT import parse_title
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from librarysync.config import settings
from librarysync.connectors.metadata.base import MediaCandidate, MetadataProvider
from librarysync.connectors.services.aiostreams_proxy import (
    AIOStreamsClient,
    AIOStreamsError,
    has_required_aiostreams_fields,
)
from librarysync.core.integrations import load_integration_with_secrets
from librarysync.core.metadata_lookup_engine import LookupRequest, MetadataLookupEngine
from librarysync.core.metadata_providers import MetadataProviderService
from librarysync.db.models import (
    EpisodeItem,
    Integration,
    MediaItem,
)
from librarysync.jobs.import_base import ImportContext, ImportResult, ImportStrategy
from librarysync.jobs.import_pipeline import (
    BlacklistIds,
    ImportCandidate,
    ImportItems,
    process_import_candidates,
)
from librarysync.jobs.import_utils import load_existing_entry_keys

LOOKBACK_DAYS = settings.history_lookback_days
MOVIE_WATCHED_SECONDS = 3600
EPISODE_WATCHED_SECONDS = 1200
IMDB_ID_RE = re.compile(r"(tt\d{3,10})", re.IGNORECASE)
TMDB_ID_RE = re.compile(r"tmdb[:/](\d+)", re.IGNORECASE)
TVDB_ID_RE = re.compile(r"tvdb[:/](\d+)", re.IGNORECASE)
YEAR_RE = re.compile(r"(19\d{2}|20\d{2})")
LOOKUP_ENGINE = MetadataLookupEngine(detail_limit=5)
logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ParsedEntry:
    raw: dict[str, Any]
    watched_at: datetime
    last_seen: datetime
    duration_seconds: int
    media_type: str
    imdb_id: str | None
    tmdb_id: str | None
    tvdb_id: str | None
    season_number: int | None
    episode_number: int | None
    title: str | None
    year: int | None
    filename: str | None
    url: str | None
    request_id: str | None
    entry_key: str


class AIOStreamsImportStrategy(ImportStrategy):
    provider = "aiostreams"

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
        db, integration.user_id, "aiostreams"
    )
    if not integration or not secret_data:
        return ImportResult(imported=0, attempted=False)
    if not has_required_aiostreams_fields(secret_data):
        return ImportResult(imported=0, attempted=False)
    auth = _coerce_str(secret_data.get("auth"))
    if not auth:
        return ImportResult(imported=0, attempted=False)

    api_base_url = None
    if integration.config and integration.config.get("api_base_url"):
        api_base_url = str(integration.config["api_base_url"])
    if not api_base_url:
        return ImportResult(imported=0, attempted=False)
    client = AIOStreamsClient(api_base_url=api_base_url)
    username = _extract_username(integration, auth)

    try:
        stats = await client.get_stats(auth)
    except AIOStreamsError as exc:
        logger.warning(
            "AIOStreams stats fetch failed for user %s: %s",
            integration.user_id,
            exc,
        )
        return ImportResult(imported=0, attempted=True)

    entries = _collect_entries(stats, username)
    if not entries:
        return ImportResult(imported=0, attempted=True)

    since = None if lookback_days < 0 else now - timedelta(days=lookback_days)

    parsed_entries: list[ParsedEntry] = []
    entry_keys: list[str] = []
    for entry in entries:
        parsed = _parse_entry(entry)
        if not parsed:
            continue
        if since and parsed.watched_at < since:
            continue
        if not _passes_watch_threshold(parsed):
            continue
        parsed_entries.append(parsed)
        entry_keys.append(parsed.entry_key)

    if not parsed_entries:
        return ImportResult(imported=0, attempted=True)

    existing_keys = await load_existing_entry_keys(
        db,
        integration.user_id,
        "aiostreams_imported",
        entry_keys,
    )
    existing_blacklist_keys = await load_existing_entry_keys(
        db,
        integration.user_id,
        "aiostreams_blacklisted",
        entry_keys,
    )
    seen_keys: set[str] = set()
    imported = 0
    providers = await _load_lookup_providers(db, integration.user_id, parsed_entries)
    candidates: list[ImportCandidate] = []
    for parsed in parsed_entries:
        if parsed.entry_key in existing_keys or parsed.entry_key in seen_keys:
            continue
        seen_keys.add(parsed.entry_key)
        candidate = await _build_candidate(
            db,
            integration.user_id,
            parsed,
            providers,
        )
        if candidate:
            candidates.append(candidate)
    imported += await process_import_candidates(
        db,
        integration.user_id,
        "aiostreams",
        candidates,
        now=now,
        existing_entry_keys=existing_keys,
        existing_blacklist_keys=existing_blacklist_keys,
    )
    if imported:
        logger.info(
            "Imported %s AIOStreams entries for user %s",
            imported,
            integration.user_id,
        )
    return ImportResult(imported=imported, attempted=True)


async def _build_candidate(
    db: AsyncSession,
    user_id: str,
    entry: ParsedEntry,
    providers: list[MetadataProvider] | None = None,
) -> ImportCandidate | None:
    entry = await _resolve_entry_metadata(db, user_id, entry, providers)
    if entry.media_type == "movie":

        async def _build_items(db: AsyncSession) -> ImportItems:
            media_item = await _get_or_create_movie_item(db, entry)
            return ImportItems(media_item=media_item, episode_item=None, show_item=None)

        return ImportCandidate(
            entry_key=entry.entry_key,
            watched_at=entry.watched_at,
            media_type="movie",
            raw=_build_event_raw(entry),
            rating=None,
            external_id=entry.request_id,
            blacklist_ids=None,
            blacklist_enabled=False,
            is_rewatch=False,
            build_items=_build_items,
        )

    async def _build_items(db: AsyncSession) -> ImportItems:
        show_item = await _get_or_create_show_item(db, entry)
        if not show_item:
            return ImportItems(media_item=None, episode_item=None, show_item=None)
        episode_item: EpisodeItem | None = None
        if entry.season_number is not None and entry.episode_number is not None:
            episode_item = await _get_or_create_episode_item(db, show_item, entry)
        return ImportItems(
            media_item=None,
            episode_item=episode_item,
            show_item=show_item,
        )

    return ImportCandidate(
        entry_key=entry.entry_key,
        watched_at=entry.watched_at,
        media_type="episode",
        raw=_build_event_raw(entry),
        rating=None,
        external_id=entry.request_id,
        blacklist_ids=BlacklistIds(
            imdb_id=entry.imdb_id,
            tmdb_id=entry.tmdb_id,
            tvdb_id=entry.tvdb_id,
            tvmaze_id=None,
        ),
        blacklist_enabled=True,
        is_rewatch=False,
        build_items=_build_items,
    )


def _collect_entries(
    stats: dict[str, Any], username: str | None
) -> list[dict[str, Any]]:
    users = stats.get("users")
    if not isinstance(users, dict):
        return []
    selected_user = None
    if username and username in users:
        selected_user = users.get(username)
    if selected_user is None and users:
        selected_user = next(iter(users.values()), None)
    if not isinstance(selected_user, dict):
        return []
    active = selected_user.get("active")
    history = selected_user.get("history")
    entries: list[dict[str, Any]] = []
    for source in (history, active):
        if not isinstance(source, list):
            continue
        for entry in source:
            if isinstance(entry, dict):
                entries.append(entry)
    return entries


def _parse_entry(entry: dict[str, Any]) -> ParsedEntry | None:
    watched_at = _parse_datetime(entry.get("timestamp"))
    last_seen = _parse_datetime(entry.get("lastSeen"))
    if not watched_at or not last_seen:
        return None
    duration_seconds = int(max(0.0, (last_seen - watched_at).total_seconds()))
    url = _coerce_str(entry.get("url"))
    filename = _coerce_str(entry.get("filename"))
    imdb_id = _extract_imdb_id(url, filename)
    tmdb_id = _extract_tmdb_id(url, filename)
    tvdb_id = _extract_tvdb_id(url, filename)
    season_number, episode_number, title_hint, year_hint = _extract_filename_details(
        filename
    )
    if not title_hint and url:
        title_hint = _extract_title_from_url(url)
    media_type = (
        "tv"
        if season_number is not None and episode_number is not None
        else "movie"
    )
    title = title_hint or _fallback_title(
        media_type, imdb_id, tmdb_id, tvdb_id, filename
    )
    entry_key = _build_entry_key(
        media_type,
        imdb_id,
        tmdb_id,
        tvdb_id,
        season_number,
        episode_number,
        title_hint,
        year_hint,
        filename,
        url,
        watched_at,
    )
    if not entry_key:
        return None
    request_id = _extract_request_id(entry.get("requestIds"))
    return ParsedEntry(
        raw=entry,
        watched_at=watched_at,
        last_seen=last_seen,
        duration_seconds=duration_seconds,
        media_type=media_type,
        imdb_id=imdb_id,
        tmdb_id=tmdb_id,
        tvdb_id=tvdb_id,
        season_number=season_number,
        episode_number=episode_number,
        title=title,
        year=year_hint,
        filename=filename,
        url=url,
        request_id=request_id,
        entry_key=entry_key,
    )


async def _load_lookup_providers(
    db: AsyncSession, user_id: str, entries: list[ParsedEntry]
) -> list[MetadataProvider] | None:
    needs_lookup = any(
        not entry.imdb_id
        and not entry.tmdb_id
        and not entry.tvdb_id
        and entry.title
        for entry in entries
    )
    if not needs_lookup:
        return None
    service = MetadataProviderService(db, user_id)
    providers = await service.load_enabled_providers()
    return providers or None


async def _resolve_entry_metadata(
    db: AsyncSession,
    user_id: str,
    entry: ParsedEntry,
    providers: list[MetadataProvider] | None = None,
) -> ParsedEntry:
    if entry.imdb_id or entry.tmdb_id or entry.tvdb_id:
        return entry
    if not entry.title:
        return entry
    local_match = await _find_media_item(
        db,
        entry.media_type,
        None,
        None,
        None,
        entry.title,
        entry.year,
    )
    if local_match:
        return _merge_entry_media_item(entry, local_match)
    candidate = await _lookup_metadata_candidate(db, user_id, entry, providers)
    if not candidate:
        return entry
    return _merge_entry_candidate(entry, candidate)


async def _lookup_metadata_candidate(
    db: AsyncSession,
    user_id: str,
    entry: ParsedEntry,
    providers: list[MetadataProvider] | None = None,
) -> MediaCandidate | None:
    if not entry.title:
        return None
    if providers is None:
        service = MetadataProviderService(db, user_id)
        providers = await service.load_enabled_providers()
    if not providers:
        return None
    scope = entry.media_type if entry.media_type in {"movie", "tv"} else "all"
    queries = _build_lookup_queries(entry.title, entry.year)
    for provider in providers:
        if not provider.supports_scope(scope):
            continue
        for query in queries:
            request = LookupRequest(query=query, query_type="title", scope=scope)
            try:
                candidates = await LOOKUP_ENGINE.lookup(provider, request)
            except Exception as exc:
                logger.debug(
                    "AIOStreams metadata lookup failed for %s: %s",
                    provider.provider,
                    exc,
                    exc_info=True,
                )
                continue
            candidate = _select_candidate_for_entry(entry, candidates)
            if not candidate:
                continue
            if _candidate_has_useful_id(candidate):
                return candidate
    return None


def _candidate_has_useful_id(candidate: MediaCandidate) -> bool:
    if candidate.imdb_id:
        return True
    if candidate.provider in {"tmdb", "tvdb", "imdb"} and candidate.provider_id:
        return True
    return False


def _build_lookup_queries(title: str, year: int | None) -> list[str]:
    cleaned = title.strip()
    if not cleaned:
        return []
    if year is None:
        return [cleaned]
    return [f"{cleaned} {year}", cleaned]


def _select_candidate_for_entry(
    entry: ParsedEntry, candidates: list[MediaCandidate]
) -> MediaCandidate | None:
    if not candidates:
        return None
    scoped = [
        candidate
        for candidate in candidates
        if candidate.media_type == entry.media_type
    ]
    if not scoped:
        scoped = candidates
    title_key = _normalize_title_key(entry.title or "")
    title_matches: list[MediaCandidate] = []
    if title_key:
        for candidate in scoped:
            if _normalize_title_key(candidate.title) == title_key:
                title_matches.append(candidate)
    if entry.year is not None:
        year_matches = [candidate for candidate in title_matches if candidate.year == entry.year]
        if year_matches:
            return year_matches[0]
    if title_matches:
        return title_matches[0]
    if entry.year is not None:
        year_matches = [candidate for candidate in scoped if candidate.year == entry.year]
        if year_matches:
            return year_matches[0]
    return scoped[0]


def _merge_entry_media_item(entry: ParsedEntry, item: MediaItem) -> ParsedEntry:
    imdb_id = entry.imdb_id or item.imdb_id
    tmdb_id = entry.tmdb_id or item.tmdb_id
    tvdb_id = entry.tvdb_id or item.tvdb_id
    title = entry.title or item.title
    year = entry.year if entry.year is not None else item.year
    return replace(
        entry,
        imdb_id=_normalize_imdb_id(imdb_id),
        tmdb_id=_normalize_optional_id(tmdb_id),
        tvdb_id=_normalize_optional_id(tvdb_id),
        title=title,
        year=year,
    )


def _merge_entry_candidate(entry: ParsedEntry, candidate: MediaCandidate) -> ParsedEntry:
    imdb_id = entry.imdb_id
    tmdb_id = entry.tmdb_id
    tvdb_id = entry.tvdb_id
    if not imdb_id:
        imdb_id = candidate.imdb_id or None
    if not tmdb_id and candidate.provider == "tmdb" and candidate.provider_id:
        tmdb_id = str(candidate.provider_id)
    if not tvdb_id and candidate.provider == "tvdb" and candidate.provider_id:
        tvdb_id = str(candidate.provider_id)
    if not imdb_id and candidate.provider == "imdb" and candidate.provider_id:
        imdb_id = str(candidate.provider_id)
    title = entry.title or candidate.title
    if entry.title and entry.title.startswith("AIOStreams "):
        title = candidate.title or entry.title
    year = entry.year if entry.year is not None else candidate.year
    return replace(
        entry,
        imdb_id=_normalize_imdb_id(imdb_id),
        tmdb_id=_normalize_optional_id(tmdb_id),
        tvdb_id=_normalize_optional_id(tvdb_id),
        title=title,
        year=year,
    )


def _extract_title_from_url(url: str) -> str | None:
    if not url:
        return None
    cleaned = re.sub(r"[._]+", " ", url)
    match = YEAR_RE.search(cleaned)
    if match:
        cleaned = cleaned[: match.start()]
    cleaned = re.sub(r"https?://", "", cleaned)
    cleaned = re.sub(r"[^A-Za-z0-9 ]+", " ", cleaned)
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    return cleaned or None


def _passes_watch_threshold(entry: ParsedEntry) -> bool:
    if entry.media_type == "movie":
        return entry.duration_seconds >= MOVIE_WATCHED_SECONDS
    return entry.duration_seconds >= EPISODE_WATCHED_SECONDS


def _extract_username(integration: Integration, auth: str) -> str | None:
    if integration.config and integration.config.get("username"):
        return str(integration.config["username"])
    if ":" in auth:
        return auth.split(":", 1)[0].strip() or None
    return None


def _extract_request_id(value: object) -> str | None:
    if isinstance(value, list) and value:
        first = value[0]
        if isinstance(first, (str, int)):
            return str(first)
    return None


def _extract_imdb_id(*values: str | None) -> str | None:
    for value in values:
        if not value:
            continue
        match = IMDB_ID_RE.search(value)
        if match:
            return match.group(1).lower()
    return None


def _extract_tmdb_id(*values: str | None) -> str | None:
    for value in values:
        if not value:
            continue
        match = TMDB_ID_RE.search(value)
        if match:
            return match.group(1)
    return None


def _extract_tvdb_id(*values: str | None) -> str | None:
    for value in values:
        if not value:
            continue
        match = TVDB_ID_RE.search(value)
        if match:
            return match.group(1)
    return None


def _extract_filename_details(
    filename: str | None,
) -> tuple[int | None, int | None, str | None, int | None]:
    if not filename:
        return None, None, None, None
    base = _strip_extension(filename)
    parsed = _parse_parsett_title(base)
    if parsed:
        season_number, episode_number = _extract_parsett_season_episode(parsed)
        title = _clean_title(_coerce_str(parsed.get("title")) or "")
        year = _coerce_int(parsed.get("year"))
        return season_number, episode_number, title, year
    return None, None, None, None


def _strip_extension(filename: str) -> str:
    if "." not in filename:
        return filename
    return filename.rsplit(".", 1)[0]


def _extract_parsett_season_episode(parsed: dict[str, Any]) -> tuple[int | None, int | None]:
    season_number = _first_int(parsed.get("seasons"))
    episode_number = _first_int(parsed.get("episodes"))
    if season_number is None or episode_number is None:
        episode_code = _coerce_str(parsed.get("episode_code"))
        if episode_code:
            episode_parsed = _parse_parsett_title(episode_code)
            if episode_parsed:
                season_number = season_number or _first_int(episode_parsed.get("seasons"))
                episode_number = episode_number or _first_int(episode_parsed.get("episodes"))
    return season_number, episode_number


def _first_int(value: object) -> int | None:
    if isinstance(value, list) and value:
        return _coerce_int(value[0])
    return _coerce_int(value)


def _parse_parsett_title(value: str) -> dict[str, Any] | None:
    try:
        parsed = parse_title(value)
    except Exception:
        logger.debug("Parsett parse failed for %s", value, exc_info=True)
        return None
    if not isinstance(parsed, dict):
        return None
    return parsed


def _clean_title(value: str) -> str | None:
    cleaned = re.sub(r"\[[^\]]*\]|\([^\)]*\)", " ", value)
    cleaned = re.sub(r"[._]+", " ", cleaned)
    cleaned = re.sub(r"[^A-Za-z0-9 ]+", " ", cleaned)
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    return cleaned or None


def _fallback_title(
    media_type: str,
    imdb_id: str | None,
    tmdb_id: str | None,
    tvdb_id: str | None,
    filename: str | None,
) -> str:
    if filename:
        cleaned = _clean_title(_strip_extension(filename))
        if cleaned:
            return cleaned
    if imdb_id:
        return f"AIOStreams {media_type} {imdb_id}"
    if tmdb_id:
        return f"AIOStreams {media_type} tmdb:{tmdb_id}"
    if tvdb_id:
        return f"AIOStreams {media_type} tvdb:{tvdb_id}"
    return f"AIOStreams {media_type}"


def _build_entry_key(
    media_type: str,
    imdb_id: str | None,
    tmdb_id: str | None,
    tvdb_id: str | None,
    season_number: int | None,
    episode_number: int | None,
    title: str | None,
    year: int | None,
    filename: str | None,
    url: str | None,
    watched_at: datetime,
) -> str | None:
    base_parts: list[str] = [media_type]
    if imdb_id:
        base_parts.append(f"imdb:{imdb_id}")
    elif tmdb_id:
        base_parts.append(f"tmdb:{tmdb_id}")
    elif tvdb_id:
        base_parts.append(f"tvdb:{tvdb_id}")
    elif title:
        normalized = _normalize_key(title)
        if year:
            normalized = f"{normalized}:{year}"
        base_parts.append(f"title:{normalized}")
    else:
        seed = filename or url
        if not seed:
            return None
        digest = hashlib.sha1(seed.encode("utf-8")).hexdigest()[:12]
        base_parts.append(f"hash:{digest}")
    if media_type == "tv":
        if season_number is None or episode_number is None:
            return None
        base_parts.append(f"s{season_number}e{episode_number}")
    base_parts.append(watched_at.isoformat())
    return ":".join(base_parts)


def _normalize_key(value: str) -> str:
    cleaned = re.sub(r"[^a-z0-9]+", "-", value.strip().lower())
    cleaned = cleaned.strip("-")
    return cleaned or "unknown"


def _normalize_title_key(value: str) -> str:
    cleaned = re.sub(r"[^a-z0-9]+", "", value.strip().lower())
    return cleaned


def _normalize_imdb_id(value: str | None) -> str | None:
    if not value:
        return None
    cleaned = value.strip().lower()
    if cleaned.startswith("tt") and cleaned[2:].isdigit():
        return cleaned
    return None


def _normalize_optional_id(value: str | None) -> str | None:
    if not value:
        return None
    cleaned = str(value).strip()
    return cleaned or None


def _build_event_raw(entry: ParsedEntry) -> dict[str, Any]:
    raw: dict[str, Any] = {
        "source": "aiostreams",
        "entry_key": entry.entry_key,
        "watched_at": entry.watched_at.isoformat(),
        "last_seen": entry.last_seen.isoformat(),
        "duration_seconds": entry.duration_seconds,
        "media_type": entry.media_type,
        "imdb_id": entry.imdb_id,
        "tmdb_id": entry.tmdb_id,
        "tvdb_id": entry.tvdb_id,
        "season_number": entry.season_number,
        "episode_number": entry.episode_number,
    }
    if entry.url:
        raw["url"] = entry.url
    if entry.filename:
        raw["filename"] = entry.filename
    if entry.request_id:
        raw["request_id"] = entry.request_id
    return raw


async def _get_or_create_movie_item(
    db: AsyncSession,
    entry: ParsedEntry,
) -> MediaItem | None:
    item = await _find_media_item(
        db,
        "movie",
        entry.imdb_id,
        entry.tmdb_id,
        entry.tvdb_id,
        entry.title,
        entry.year,
    )
    if item:
        _apply_media_updates(item, entry)
        return item
    if not entry.imdb_id and not entry.tmdb_id and not entry.tvdb_id and not entry.title:
        return None
    item = MediaItem(
        media_type="movie",
        title=entry.title
        or _fallback_title("movie", entry.imdb_id, entry.tmdb_id, entry.tvdb_id, None),
        year=entry.year,
        imdb_id=entry.imdb_id,
        tmdb_id=entry.tmdb_id,
        tvdb_id=entry.tvdb_id,
        poster_url=None,
        raw=_build_media_raw("movie"),
    )
    db.add(item)
    await db.flush()
    return item


async def _get_or_create_show_item(
    db: AsyncSession,
    entry: ParsedEntry,
) -> MediaItem | None:
    item = await _find_media_item(
        db,
        "tv",
        entry.imdb_id,
        entry.tmdb_id,
        entry.tvdb_id,
        entry.title,
        entry.year,
    )
    if item:
        _apply_media_updates(item, entry)
        return item
    if not entry.imdb_id and not entry.tmdb_id and not entry.tvdb_id and not entry.title:
        return None
    item = MediaItem(
        media_type="tv",
        title=entry.title
        or _fallback_title("tv", entry.imdb_id, entry.tmdb_id, entry.tvdb_id, None),
        year=entry.year,
        imdb_id=entry.imdb_id,
        tmdb_id=entry.tmdb_id,
        tvdb_id=entry.tvdb_id,
        poster_url=None,
        raw=_build_media_raw("tv"),
    )
    db.add(item)
    await db.flush()
    return item


async def _get_or_create_episode_item(
    db: AsyncSession,
    show_item: MediaItem,
    entry: ParsedEntry,
) -> EpisodeItem | None:
    result = await db.execute(
        select(EpisodeItem).where(
            EpisodeItem.show_media_item_id == show_item.id,
            EpisodeItem.season_number == entry.season_number,
            EpisodeItem.episode_number == entry.episode_number,
        )
    )
    item = result.scalars().first()
    if item:
        if item.raw is None:
            item.raw = _build_episode_raw()
        return item
    if entry.season_number is None or entry.episode_number is None:
        return None
    item = EpisodeItem(
        show_media_item_id=show_item.id,
        season_number=entry.season_number,
        episode_number=entry.episode_number,
        title=None,
        raw=_build_episode_raw(),
    )
    db.add(item)
    await db.flush()
    return item


async def _find_media_item(
    db: AsyncSession,
    media_type: str,
    imdb_id: str | None,
    tmdb_id: str | None,
    tvdb_id: str | None,
    title: str | None,
    year: int | None,
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
    if not item and title:
        query = select(MediaItem).where(
            MediaItem.media_type == media_type,
            MediaItem.title == title,
        )
        if year is not None:
            query = query.where(MediaItem.year == year)
        result = await db.execute(query)
        item = result.scalars().first()
    return item


def _apply_media_updates(item: MediaItem, entry: ParsedEntry) -> None:
    if entry.imdb_id and not item.imdb_id:
        item.imdb_id = entry.imdb_id
    if entry.tmdb_id and not item.tmdb_id:
        item.tmdb_id = entry.tmdb_id
    if entry.tvdb_id and not item.tvdb_id:
        item.tvdb_id = entry.tvdb_id
    if entry.year is not None and item.year is None:
        item.year = entry.year
    if entry.title and (not item.title or item.title.startswith("AIOStreams ")):
        item.title = entry.title
    if item.raw is None:
        item.raw = _build_media_raw(item.media_type)


def _build_media_raw(media_type: str) -> dict[str, Any]:
    return {"source": "aiostreams", "media_type": media_type}


def _build_episode_raw() -> dict[str, Any]:
    return {"source": "aiostreams"}


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
            return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
        except ValueError:
            return None
    return None


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
    if isinstance(value, float):
        return int(value)
    if isinstance(value, str):
        cleaned = value.strip()
        if cleaned.isdigit():
            return int(cleaned)
    return None
