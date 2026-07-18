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
    WatchedItem,
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
SEASON_EPISODE_RE = re.compile(r"[Ss](\d{1,2})[ ._-]?[Ee](\d{1,3})")
SEASON_EPISODE_ALT_RE = re.compile(r"(\d{1,2})x(\d{1,3})", re.IGNORECASE)
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
    debug_prefix = f"AIOStreams user {integration.user_id}"
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
    logger.info("%s import starting (username=%s)", debug_prefix, username or "auto")

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
    logger.info("%s stats returned %s entries", debug_prefix, len(entries))
    if not entries:
        return ImportResult(imported=0, attempted=True)

    since = None if lookback_days < 0 else now - timedelta(days=lookback_days)

    parsed_entries: list[ParsedEntry] = []
    entry_keys: list[str] = []
    dropped_unparsed = 0
    dropped_lookback = 0
    dropped_threshold = 0
    for entry in entries:
        parsed = _parse_entry(entry)
        if not parsed:
            dropped_unparsed += 1
            continue
        if since and parsed.watched_at < since:
            dropped_lookback += 1
            continue
        if not _passes_watch_threshold(parsed):
            dropped_threshold += 1
            continue
        parsed_entries.append(parsed)
        entry_keys.append(parsed.entry_key)

    logger.info(
        "%s parsed=%s dropped_unparsed=%s dropped_lookback=%s dropped_threshold=%s",
        debug_prefix,
        len(parsed_entries),
        dropped_unparsed,
        dropped_lookback,
        dropped_threshold,
    )
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
    skipped_existing = 0
    skipped_seen = 0
    skipped_build = 0
    imported = 0
    providers = await _load_lookup_providers(db, integration.user_id, parsed_entries)
    candidates: list[ImportCandidate] = []
    for parsed in parsed_entries:
        if parsed.entry_key in existing_keys or parsed.entry_key in seen_keys:
            if parsed.entry_key in existing_keys:
                skipped_existing += 1
            else:
                skipped_seen += 1
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
        else:
            skipped_build += 1
    logger.info(
        "%s candidates=%s skipped_existing=%s skipped_seen=%s skipped_build=%s",
        debug_prefix,
        len(candidates),
        skipped_existing,
        skipped_seen,
        skipped_build,
    )
    imported += await process_import_candidates(
        db,
        integration.user_id,
        "aiostreams",
        candidates,
        now=now,
        existing_entry_keys=existing_keys,
        existing_blacklist_keys=existing_blacklist_keys,
    )
    logger.info("%s imported=%s", debug_prefix, imported)
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
    if isinstance(users, dict):
        selected_user = None
        if username and username in users:
            selected_user = users.get(username)
        if selected_user is None and users:
            selected_user = next(iter(users.values()), None)
        if not isinstance(selected_user, dict):
            return []
    elif isinstance(stats, dict):
        selected_user = stats
    else:
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
            candidate = await _select_candidate_for_entry(db, user_id, entry, candidates)
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


async def _check_series_continuity(
    db: AsyncSession,
    user_id: str,
    entry: ParsedEntry,
    candidates: list[MediaCandidate],
) -> MediaCandidate | None:
    """
    Check if the user has watched an earlier episode of a show matching one of the candidates.
    This helps disambiguate when there are multiple shows with the same or similar titles.
    
    For example, if watching "Fallout S02E08" and user previously watched "Fallout S02E07",
    prefer the same show's metadata rather than selecting a different "Fallout" (e.g., an anime).
    """
    if not entry.title or entry.season_number is None or entry.episode_number is None:
        return None
    
    # Build a normalized title key for matching
    title_key = _normalize_title_key(entry.title)
    if not title_key:
        return None
    
    # Query for shows the user has watched episodes of, matching the title
    # We look for MediaItems that:
    # 1. Are TV shows
    # 2. Have a matching normalized title
    # 3. Have episode watches by this user
    from sqlalchemy import and_, func
    
    # First, find all TV shows the user has watched
    # Use a subquery to get the max season and episode per show
    # Note: This may return max_season from one episode and max_episode from another,
    # but it's acceptable for our continuity check as we're looking for a general pattern
    result = await db.execute(
        select(
            MediaItem,
            func.max(EpisodeItem.season_number).label("max_season"),
            func.max(EpisodeItem.episode_number).label("max_episode"),
        )
        .join(EpisodeItem, EpisodeItem.show_media_item_id == MediaItem.id)
        .join(WatchedItem, and_(
            WatchedItem.episode_item_id == EpisodeItem.id,
            WatchedItem.user_id == user_id
        ))
        .where(MediaItem.media_type == "tv")
        .group_by(MediaItem.id)
    )
    
    watched_shows = result.all()
    
    # For each watched show, check if it matches one of our candidates
    for show_item, max_season, max_episode in watched_shows:
        # Check if the show title matches the entry title (fuzzy match)
        show_title_key = _normalize_title_key(show_item.title or "")
        if not show_title_key or show_title_key != title_key:
            continue
        
        # Skip if we don't have valid season/episode data
        if max_season is None or max_episode is None:
            continue
        
        # Check if this is a continuation (same season and later episode, or later season)
        if entry.season_number is not None:
            is_continuation = (
                (entry.season_number == max_season and entry.episode_number > max_episode) or
                (entry.season_number > max_season)
            )
            
            # Allow same episode or earlier if not too far back (could be rewatching)
            is_nearby = (
                entry.season_number == max_season and 
                abs(entry.episode_number - max_episode) <= 3
            )
            
            if not is_continuation and not is_nearby:
                continue
        
        # Now check if this show matches one of our candidates
        for candidate in candidates:
            # Match by IMDb ID (most reliable)
            if show_item.imdb_id and candidate.imdb_id:
                if show_item.imdb_id.lower() == candidate.imdb_id.lower():
                    return candidate
            
            # Match by TMDB ID
            if show_item.tmdb_id and candidate.provider == "tmdb" and candidate.provider_id:
                if str(show_item.tmdb_id) == str(candidate.provider_id):
                    return candidate
            
            # Match by TVDB ID
            if show_item.tvdb_id and candidate.provider == "tvdb" and candidate.provider_id:
                if str(show_item.tvdb_id) == str(candidate.provider_id):
                    return candidate
    
    return None


async def _select_candidate_for_entry(
    db: AsyncSession,
    user_id: str,
    entry: ParsedEntry,
    candidates: list[MediaCandidate],
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
    
    # For TV shows, check if user has watched a previous episode from one of the candidates
    if entry.media_type == "tv" and entry.season_number is not None and entry.episode_number is not None:  #noqa: E501
        continuity_candidate = await _check_series_continuity(
            db, user_id, entry, scoped
        )
        if continuity_candidate:
            logger.debug(
                "Using series continuity: selected %s (imdb=%s) for %s S%02dE%02d",
                continuity_candidate.title,
                continuity_candidate.imdb_id,
                entry.title,
                entry.season_number,
                entry.episode_number,
            )
            return continuity_candidate
    
    title_key = _normalize_title_key(entry.title or "")
    title_matches: list[MediaCandidate] = []
    if title_key:
        for candidate in scoped:
            if candidate.title and _normalize_title_key(candidate.title) == title_key:
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
        if season_number is not None and episode_number is not None:
            return season_number, episode_number, title, year
        fallback_season, fallback_episode, fallback_title = _extract_season_episode_from_text(base)
        if fallback_season is not None and fallback_episode is not None:
            return (
                fallback_season,
                fallback_episode,
                fallback_title or title,
                year,
            )
        return season_number, episode_number, title, year
    season_number, episode_number, title = _extract_season_episode_from_text(base)
    if season_number is None or episode_number is None:
        return None, None, None, None
    return season_number, episode_number, title, None


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


def _extract_season_episode_from_text(
    value: str,
) -> tuple[int | None, int | None, str | None]:
    for regex in (SEASON_EPISODE_RE, SEASON_EPISODE_ALT_RE):
        match = regex.search(value)
        if not match:
            continue
        season_number = _coerce_int(match.group(1))
        episode_number = _coerce_int(match.group(2))
        title = _clean_title(value[: match.start()])
        return season_number, episode_number, title
    return None, None, None


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
        await _apply_media_updates(db, item, entry)
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
        await _apply_media_updates(db, item, entry)
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


async def _apply_media_updates(
    db: AsyncSession,
    item: MediaItem,
    entry: ParsedEntry,
) -> None:
    await _maybe_set_media_id(db, item, "imdb_id", entry.imdb_id)
    await _maybe_set_media_id(db, item, "tmdb_id", entry.tmdb_id)
    await _maybe_set_media_id(db, item, "tvdb_id", entry.tvdb_id)
    if entry.year is not None and item.year is None:
        item.year = entry.year
    if entry.title and (not item.title or item.title.startswith("AIOStreams ")):
        item.title = entry.title
    if item.raw is None:
        item.raw = _build_media_raw(item.media_type)


async def _maybe_set_media_id(
    db: AsyncSession,
    item: MediaItem,
    field: str,
    value: str | None,
) -> None:
    if not value or getattr(item, field):
        return
    if await _can_assign_media_id(db, item, field, value):
        setattr(item, field, value)
    else:
        logger.warning(
            "Skipping %s=%s for media item %s due to conflict",
            field,
            value,
            item.id,
        )


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
