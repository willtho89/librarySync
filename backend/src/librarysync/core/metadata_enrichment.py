from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone

from sqlalchemy import and_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from librarysync.connectors.metadata.base import (
    EpisodeMetadataProvider,
    MediaCandidate,
    MetadataProvider,
)
from librarysync.core.anime import is_anime
from librarysync.core.metadata_providers import MetadataProviderService
from librarysync.core.rate_limiter import RATE_LIMITER
from librarysync.db.models import EpisodeItem, MediaItem

logger = logging.getLogger(__name__)

PREFERRED_POSTER_HOSTS = (
    "image.tmdb.org",
    "thetvdb.com",
    "artworks.thetvdb.com",
    "m.media-amazon.com",
    "images-na.ssl-images-amazon.com",
)
ANIME_PROVIDERS = ("myanimelist", "kitsu", "anilist")
POSTER_PROVIDER_ORDER = ("tvdb", "tmdb", "imdb")
TITLE_LOOKUP_PREFIXES = ("AIOStreams ", "Stremio ", "Trakt ", "SIMKL ", "Letterboxd ")
LOOKUP_MISS_KEY = "metadata_lookup_missed_at"
LOOKUP_MISS_COOLDOWN = timedelta(days=7)


@dataclass
class LookupOutcome:
    """What one enrichment run asked and heard. With a budget user, each request takes a rate-limit token."""

    budget_user_id: str | None = None
    providers: int = 0
    failed: list[str] = field(default_factory=list)
    retry_at: datetime | None = None

    async def allow(self, db: AsyncSession, provider: MetadataProvider) -> bool:
        if self.retry_at is not None:
            return False
        if self.budget_user_id is None:
            return True
        decision = await RATE_LIMITER.try_acquire(db, self.budget_user_id, provider.provider)
        if decision and not decision.allowed:
            self.retry_at = decision.retry_at or datetime.now(timezone.utc)
            return False
        return True

    def fail(self, provider: MetadataProvider, exc: Exception) -> None:
        self.failed.append(f"{provider.provider}: {exc}")


class MetadataLookupThrottled(Exception):
    """A provider budget refused a request. The outbox retries it like a 429, without using an attempt."""

    def __init__(self, retry_at: datetime) -> None:
        super().__init__(f"Metadata lookup rate-limited until {retry_at.isoformat()}")
        self.retry_after = max((retry_at - datetime.now(timezone.utc)).total_seconds(), 0.0)


class MetadataLookupFailed(Exception):
    """A provider request failed, so the lookup is retried with the outbox backoff."""


async def _allowed(db: AsyncSession, provider: MetadataProvider, outcome: LookupOutcome | None) -> bool:
    return outcome is None or await outcome.allow(db, provider)


async def enrich_watched_metadata(
    db: AsyncSession,
    user_id: str,
    media_item: MediaItem | None,
    episode_item: EpisodeItem | None,
    provider_overrides: dict[str, MetadataProvider] | None = None,
    use_overrides_only: bool = False,
    outcome: LookupOutcome | None = None,
) -> None:
    if not media_item:
        return
    if is_anime(media_item):
        await _enrich_anime_metadata(
            db,
            user_id,
            media_item,
            provider_overrides=provider_overrides,
            use_overrides_only=use_overrides_only,
            outcome=outcome,
        )
    if media_item.media_type not in {"movie", "tv"}:
        return
    if not _needs_media_enrichment(media_item, episode_item):
        return

    if await _apply_local_metadata(db, media_item):
        if not _needs_media_enrichment(media_item, episode_item):
            return

    overrides = provider_overrides or {}
    tmdb = overrides.get("tmdb")
    tvdb = overrides.get("tvdb")
    imdb = overrides.get("imdb")
    service: MetadataProviderService | None = None
    if not use_overrides_only:
        service = MetadataProviderService(db, user_id)
        if tmdb is None:
            tmdb = await service.load_provider("tmdb")
        if tvdb is None:
            tvdb = await service.load_provider("tvdb")
        if imdb is None:
            imdb = await service.load_provider("imdb")
    if not tmdb and not tvdb and not imdb:
        return
    if outcome is not None:
        outcome.providers += sum(1 for provider in (tmdb, tvdb, imdb) if provider)

    candidate_map: dict[str, MediaCandidate] = {}
    if _should_lookup_by_title(media_item):
        candidate = await _lookup_by_title(db, media_item, tmdb, tvdb, imdb, outcome)
        if candidate:
            candidate_map[candidate.provider] = candidate
            await _apply_candidate_to_media_item(db, media_item, candidate, update_poster=True)
            if episode_item and candidate.provider == "tmdb" and isinstance(tmdb, EpisodeMetadataProvider):
                await _apply_episode_metadata(tmdb, media_item, episode_item)

    if tmdb:
        candidate = await _fetch_provider_candidate(db, tmdb, media_item, "tmdb_id", outcome)
        if candidate:
            candidate_map["tmdb"] = candidate
            await _apply_candidate_to_media_item(
                db,
                media_item,
                candidate,
                update_poster=False,
            )
        if episode_item and isinstance(tmdb, EpisodeMetadataProvider):
            await _apply_episode_metadata(tmdb, media_item, episode_item)

    if tvdb:
        candidate = await _fetch_provider_candidate(db, tvdb, media_item, "tvdb_id", outcome)
        if candidate:
            candidate_map["tvdb"] = candidate
            await _apply_candidate_to_media_item(
                db,
                media_item,
                candidate,
                update_poster=False,
            )

    if imdb:
        candidate = await _fetch_provider_candidate(db, imdb, media_item, "imdb_id", outcome)
        if candidate:
            candidate_map["imdb"] = candidate
            await _apply_candidate_to_media_item(db, media_item, candidate, update_poster=False)

    if media_item.media_type in {"movie", "tv"}:
        poster_url = _select_media_poster(candidate_map, media_item.poster_url)
        if poster_url:
            media_item.poster_url = poster_url


def _needs_media_enrichment(media_item: MediaItem, episode_item: EpisodeItem | None) -> bool:
    missing_ids = not media_item.imdb_id or not media_item.tmdb_id or not media_item.tvdb_id
    poster_missing = not media_item.poster_url or not _is_preferred_poster(media_item.poster_url)
    missing_year = media_item.year is None
    episode_needs_tmdb = (
        episode_item is not None
        and media_item.media_type == "tv"
        and bool(media_item.tmdb_id)
        and not episode_item.tmdb_id
    )
    episode_needs_title = (
        episode_item is not None
        and media_item.media_type == "tv"
        and bool(media_item.tmdb_id)
        and not episode_item.title
    )
    missing_overview = not media_item.overview
    missing_genres = not media_item.genres
    missing_runtime = media_item.runtime_in_seconds is None
    missing_release_date = not media_item.release_date and media_item.media_type == "movie"
    missing_air_dates = (
        not media_item.first_air_date or not media_item.last_air_date
    ) and media_item.media_type == "tv"
    missing_raw = not media_item.raw
    return (
        missing_ids
        or poster_missing
        or missing_year
        or episode_needs_tmdb
        or episode_needs_title
        or missing_overview
        or missing_genres
        or missing_runtime
        or missing_release_date
        or missing_air_dates
        or missing_raw
    )


def _should_lookup_by_title(media_item: MediaItem) -> bool:
    if media_item.imdb_id or media_item.tmdb_id or media_item.tvdb_id:
        return False
    if not media_item.title or is_watch_state_placeholder(media_item):
        return False
    return not media_item.title.startswith(TITLE_LOOKUP_PREFIXES)


async def _lookup_by_title(
    db: AsyncSession,
    media_item: MediaItem,
    tmdb: MetadataProvider | None,
    tvdb: MetadataProvider | None,
    imdb: MetadataProvider | None,
    outcome: LookupOutcome | None = None,
) -> MediaCandidate | None:
    scope = _scope_for_media_item(media_item)
    if scope is None:
        return None
    queries = _build_lookup_queries(media_item.title, media_item.year)
    for provider in (tmdb, tvdb, imdb):
        if not provider or not provider.capabilities.supports_search:
            continue
        if not provider.supports_scope(scope):
            continue
        for query in queries:
            if not await _allowed(db, provider, outcome):
                return None
            try:
                candidates = await provider.search(query, scope)
            except Exception as exc:
                logger.warning(
                    "%s title search failed for %s: %s",
                    provider.provider,
                    media_item.id,
                    exc,
                )
                if outcome is not None:
                    outcome.fail(provider, exc)
                continue
            candidate = _select_title_candidate(candidates, media_item.title, media_item.year)
            if candidate:
                return candidate
    return None


def _build_lookup_queries(title: str, year: int | None) -> list[str]:
    cleaned = title.strip()
    if not cleaned:
        return []
    if year is None:
        return [cleaned]
    return [f"{cleaned} {year}", cleaned]


def _select_title_candidate(
    candidates: list[MediaCandidate],
    title: str | None,
    year: int | None,
) -> MediaCandidate | None:
    if not candidates or not title:
        return None
    title_key = _normalize_title_key(title)
    if not title_key:
        return None
    title_matches = [
        candidate for candidate in candidates if candidate.title and _normalize_title_key(candidate.title) == title_key
    ]
    if year is not None:
        year_matches = [candidate for candidate in title_matches if candidate.year == year]
        if year_matches:
            return year_matches[0]
    if title_matches:
        return title_matches[0]
    return None


def _needs_anime_enrichment(media_item: MediaItem) -> bool:
    return any(
        value is None
        for value in (
            media_item.anilist_id,
            media_item.kitsu_id,
            media_item.myanimelist_id,
        )
    )


async def _enrich_anime_metadata(
    db: AsyncSession,
    user_id: str,
    media_item: MediaItem,
    *,
    provider_overrides: dict[str, MetadataProvider] | None = None,
    use_overrides_only: bool = False,
    outcome: LookupOutcome | None = None,
) -> None:
    placeholder = is_watch_state_placeholder(media_item)
    if not placeholder and not _needs_anime_enrichment(media_item):
        return
    if not media_item.title:
        return

    providers: list[MetadataProvider] = []
    overrides = provider_overrides or {}
    for provider_name in ANIME_PROVIDERS:
        provider = overrides.get(provider_name)
        if provider:
            providers.append(provider)
    if not use_overrides_only:
        service = MetadataProviderService(db, user_id)
        for provider_name in ANIME_PROVIDERS:
            if provider_name in overrides:
                continue
            provider = await service.load_provider(provider_name)
            if provider:
                providers.append(provider)
    if not providers:
        return
    if outcome is not None:
        outcome.providers += len(providers)
    for provider in providers:
        known_id = _anime_id(media_item, provider.provider)
        if known_id and not placeholder:
            continue
        if known_id:
            # A placeholder's known id names the title exactly, so its details replace the meta id.
            candidate = await _fetch_anime_details(db, provider, media_item, known_id, outcome)
        else:
            candidate = await _find_anime_candidate(db, provider, media_item, outcome)
        if not candidate:
            continue
        await _apply_anime_candidate(db, media_item, candidate)


def _anime_id(media_item: MediaItem, provider: str) -> str | None:
    if provider == "anilist":
        return media_item.anilist_id
    if provider == "kitsu":
        return media_item.kitsu_id
    if provider == "myanimelist":
        return media_item.myanimelist_id
    return None


async def _fetch_anime_details(
    db: AsyncSession,
    provider: MetadataProvider,
    media_item: MediaItem,
    provider_id: str,
    outcome: LookupOutcome | None,
) -> MediaCandidate | None:
    if not await _allowed(db, provider, outcome):
        return None
    try:
        return await provider.get_details(provider_id, "anime")
    except Exception as exc:
        logger.warning("%s anime details failed for %s: %s", provider.provider, media_item.id, exc)
        if outcome is not None:
            outcome.fail(provider, exc)
        return None


async def _find_anime_candidate(
    db: AsyncSession,
    provider: MetadataProvider,
    media_item: MediaItem,
    outcome: LookupOutcome | None = None,
) -> MediaCandidate | None:
    scope = "anime"
    if provider.provider == "anilist" and media_item.myanimelist_id and await _allowed(db, provider, outcome):
        try:
            candidates = await provider.find_by_external_id(f"mal:{media_item.myanimelist_id}", scope)
        except Exception as exc:
            logger.warning(
                "%s anime MAL lookup failed for %s: %s",
                provider.provider,
                media_item.id,
                exc,
            )
            if outcome is not None:
                outcome.fail(provider, exc)
        else:
            if candidates:
                return candidates[0]
    if provider.capabilities.supports_external_id and media_item.imdb_id and await _allowed(db, provider, outcome):
        imdb_id = media_item.imdb_id.lower()
        try:
            candidates = await provider.find_by_external_id(imdb_id, scope)
        except Exception as exc:
            logger.warning("%s anime lookup failed for %s: %s", provider.provider, media_item.id, exc)
            if outcome is not None:
                outcome.fail(provider, exc)
        else:
            selected = _select_anime_candidate(candidates, media_item)
            if selected:
                return selected
    # A placeholder is named after its meta id, which no title search can match.
    if is_watch_state_placeholder(media_item) or not await _allowed(db, provider, outcome):
        return None
    try:
        candidates = await provider.search(media_item.title, scope)
    except Exception as exc:
        logger.warning("%s anime search failed for %s: %s", provider.provider, media_item.id, exc)
        if outcome is not None:
            outcome.fail(provider, exc)
        return None
    return _select_anime_candidate(candidates, media_item)


def _select_anime_candidate(candidates: list[MediaCandidate], media_item: MediaItem) -> MediaCandidate | None:
    if not candidates:
        return None
    target_key = _normalize_title_key(media_item.title)
    if not target_key:
        return None
    matches = [
        candidate for candidate in candidates if candidate.title and _normalize_title_key(candidate.title) == target_key
    ]
    if not matches:
        return None
    if media_item.year is not None:
        year_matches = [candidate for candidate in matches if candidate.year == media_item.year]
        if len(year_matches) == 1:
            return year_matches[0]
        if len(year_matches) > 1:
            return None
        unknown_year = [candidate for candidate in matches if candidate.year is None]
        if len(unknown_year) == 1:
            return unknown_year[0]
        return None
    if len(matches) == 1:
        return matches[0]
    return None


def _normalize_title_key(title: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", title.lower()).strip()


async def _apply_local_metadata(db: AsyncSession, media_item: MediaItem) -> bool:
    candidate = await _find_local_metadata_candidate(db, media_item)
    if not candidate:
        return False
    updated = False
    before_ids = (media_item.imdb_id, media_item.tmdb_id, media_item.tvdb_id)
    if candidate.imdb_id:
        await _set_media_id(db, media_item, "imdb_id", candidate.imdb_id, normalize=True)
    if candidate.tmdb_id:
        await _set_media_id(db, media_item, "tmdb_id", candidate.tmdb_id)
    if candidate.tvdb_id:
        await _set_media_id(db, media_item, "tvdb_id", candidate.tvdb_id)
    if before_ids != (media_item.imdb_id, media_item.tmdb_id, media_item.tvdb_id):
        updated = True
    if not media_item.poster_url and candidate.poster_url:
        media_item.poster_url = candidate.poster_url
        updated = True
    replaceable = not media_item.title or media_item.title.startswith("Stremio ")
    if (replaceable or is_watch_state_placeholder(media_item)) and candidate.title:
        media_item.title = candidate.title
        updated = True
    if media_item.year is None and candidate.year is not None:
        media_item.year = candidate.year
        updated = True
    return updated


async def _find_local_metadata_candidate(db: AsyncSession, media_item: MediaItem) -> MediaItem | None:
    if media_item.imdb_id:
        result = await db.execute(
            select(MediaItem).where(
                MediaItem.imdb_id == media_item.imdb_id,
                MediaItem.id != media_item.id,
            )
        )
        candidate = result.scalars().first()
        if candidate:
            return candidate
    if media_item.tmdb_id:
        result = await db.execute(
            select(MediaItem).where(
                MediaItem.tmdb_id == media_item.tmdb_id,
                MediaItem.media_type == media_item.media_type,
                MediaItem.id != media_item.id,
            )
        )
        candidate = result.scalars().first()
        if candidate:
            return candidate
    if media_item.tvdb_id:
        result = await db.execute(
            select(MediaItem).where(
                MediaItem.tvdb_id == media_item.tvdb_id,
                MediaItem.media_type == media_item.media_type,
                MediaItem.id != media_item.id,
            )
        )
        candidate = result.scalars().first()
        if candidate:
            return candidate
    if media_item.title and media_item.year is not None:
        result = await db.execute(
            select(MediaItem).where(
                MediaItem.media_type == media_item.media_type,
                MediaItem.title == media_item.title,
                MediaItem.year == media_item.year,
                MediaItem.id != media_item.id,
                or_(
                    MediaItem.imdb_id.is_not(None),
                    MediaItem.tmdb_id.is_not(None),
                    MediaItem.tvdb_id.is_not(None),
                ),
            )
        )
        return result.scalars().first()
    return None


async def _fetch_provider_candidate(
    db: AsyncSession,
    provider: MetadataProvider,
    media_item: MediaItem,
    provider_id_field: str,
    outcome: LookupOutcome | None = None,
) -> MediaCandidate | None:
    scope = _scope_for_media_item(media_item)
    if scope is None:
        return None
    provider_id = getattr(media_item, provider_id_field)
    if provider_id:
        if not await _allowed(db, provider, outcome):
            return None
        try:
            return await provider.get_details(provider_id, scope)
        except Exception as exc:
            logger.warning("%s details failed for %s: %s", provider.provider, media_item.id, exc)
            if outcome is not None:
                outcome.fail(provider, exc)
            return None
    external_ids = _external_ids_for_provider(provider, media_item)
    for external_id in external_ids:
        if not await _allowed(db, provider, outcome):
            return None
        try:
            candidates = await provider.find_by_external_id(external_id, scope)
        except Exception as exc:
            logger.warning("%s lookup failed for %s: %s", provider.provider, media_item.id, exc)
            if outcome is not None:
                outcome.fail(provider, exc)
            return None
        candidate = _select_candidate(candidates, scope)
        if candidate:
            return candidate
    return None


def _external_ids_for_provider(provider: MetadataProvider, media_item: MediaItem) -> list[str]:
    if media_item.imdb_id:
        return [media_item.imdb_id.lower()]
    if provider.provider == "tvdb" and media_item.tmdb_id:
        return [media_item.tmdb_id]
    return []


async def _apply_candidate_ids(
    db: AsyncSession,
    media_item: MediaItem,
    ids: dict[str, str],
) -> None:
    await _set_media_id(db, media_item, "imdb_id", ids.get("imdb_id"), normalize=True)
    await _set_media_id(db, media_item, "tmdb_id", ids.get("tmdb_id"))
    await _set_media_id(db, media_item, "tvdb_id", ids.get("tvdb_id"))
    await _set_media_id(db, media_item, "tvmaze_id", ids.get("tvmaze_id"))
    await _set_media_id(db, media_item, "kitsu_id", ids.get("kitsu_id"))
    await _set_media_id(db, media_item, "myanimelist_id", ids.get("myanimelist_id"))
    await _set_media_id(db, media_item, "anilist_id", ids.get("anilist_id"))


def is_watch_state_placeholder(media_item: MediaItem) -> bool:
    """A title created by a Watch State event and still named after its meta id."""
    raw = media_item.raw if isinstance(media_item.raw, dict) else {}
    return raw.get("source") == "aiostreams_watch_state" and media_item.title == raw.get("stremio_id")


def watch_state_placeholder_clause():
    return and_(
        MediaItem.raw["source"].as_string() == "aiostreams_watch_state",
        MediaItem.title == MediaItem.raw["stremio_id"].as_string(),
    )


def needs_placeholder_lookup(media_item: MediaItem, now: datetime | None = None) -> bool:
    """A placeholder with no confirmed miss in the last LOOKUP_MISS_COOLDOWN."""
    if not is_watch_state_placeholder(media_item):
        return False
    missed_at = media_item.raw.get(LOOKUP_MISS_KEY)
    if not missed_at:
        return True
    try:
        missed = datetime.fromisoformat(missed_at)
    except (TypeError, ValueError):
        return True
    return (now or datetime.now(timezone.utc)) - missed >= LOOKUP_MISS_COOLDOWN


def no_recent_lookup_miss_clause(now: datetime):
    missed_at = MediaItem.raw[LOOKUP_MISS_KEY].as_string()
    return or_(missed_at.is_(None), missed_at < (now - LOOKUP_MISS_COOLDOWN).isoformat())


def settle_placeholder_lookup(media_item: MediaItem, outcome: LookupOutcome, now: datetime) -> str:
    """Record what a placeholder lookup achieved. Only a resolved title counts as refreshed."""
    if not is_watch_state_placeholder(media_item):
        media_item.metadata_refreshed_at = now
        return "refreshed"
    if outcome.retry_at is not None:
        return "throttled"
    if outcome.failed:
        return "failed"
    if not outcome.providers:
        return "unconfigured"
    # Providers answered without a match: wait LOOKUP_MISS_COOLDOWN before asking again.
    media_item.raw = {**media_item.raw, LOOKUP_MISS_KEY: now.isoformat()}
    return "missed"


async def _apply_candidate_to_media_item(
    db: AsyncSession,
    media_item: MediaItem,
    candidate: MediaCandidate,
    update_poster: bool = True,
) -> None:
    ids = _extract_candidate_ids(candidate)
    await _apply_candidate_ids(db, media_item, ids)
    if candidate.title and is_watch_state_placeholder(media_item):
        media_item.title = candidate.title
    if update_poster and candidate.poster_url and _should_update_poster(media_item.poster_url, candidate.provider):
        media_item.poster_url = candidate.poster_url
    if media_item.year is None and candidate.year is not None:
        media_item.year = candidate.year

    if not media_item.release_date and candidate.release_date:
        media_item.release_date = _parse_date(candidate.release_date)
    if not media_item.first_air_date and candidate.first_air_date:
        media_item.first_air_date = _parse_date(candidate.first_air_date)
    if not media_item.last_air_date and candidate.last_air_date:
        media_item.last_air_date = _parse_date(candidate.last_air_date)


async def apply_refresh_candidate(
    db: AsyncSession,
    media_item: MediaItem,
    candidate: MediaCandidate,
    *,
    overwrite: bool = True,
) -> None:
    ids = _extract_candidate_ids(candidate)
    await _apply_candidate_ids(db, media_item, ids)
    if overwrite:
        if candidate.title:
            media_item.title = candidate.title
        if candidate.year is not None:
            media_item.year = candidate.year
        if candidate.poster_url:
            media_item.poster_url = candidate.poster_url
        if candidate.release_date:
            parsed = _parse_date(candidate.release_date)
            if parsed:
                media_item.release_date = parsed
        if candidate.first_air_date:
            parsed = _parse_date(candidate.first_air_date)
            if parsed:
                media_item.first_air_date = parsed
        if candidate.last_air_date:
            parsed = _parse_date(candidate.last_air_date)
            if parsed:
                media_item.last_air_date = parsed
        if candidate.runtime_in_seconds is not None:
            media_item.runtime_in_seconds = candidate.runtime_in_seconds
        if candidate.genres:
            media_item.genres = candidate.genres
        if candidate.overview:
            media_item.overview = candidate.overview
        if candidate.raw:
            media_item.raw = candidate.raw
        return

    if candidate.title and not media_item.title:
        media_item.title = candidate.title
    if candidate.year is not None and media_item.year is None:
        media_item.year = candidate.year
    if candidate.poster_url and not media_item.poster_url:
        media_item.poster_url = candidate.poster_url
    if candidate.release_date and not media_item.release_date:
        parsed = _parse_date(candidate.release_date)
        if parsed:
            media_item.release_date = parsed
    if candidate.first_air_date and not media_item.first_air_date:
        parsed = _parse_date(candidate.first_air_date)
        if parsed:
            media_item.first_air_date = parsed
    if candidate.last_air_date and not media_item.last_air_date:
        parsed = _parse_date(candidate.last_air_date)
        if parsed:
            media_item.last_air_date = parsed
    if candidate.runtime_in_seconds is not None and media_item.runtime_in_seconds is None:
        media_item.runtime_in_seconds = candidate.runtime_in_seconds
    if candidate.genres and not media_item.genres:
        media_item.genres = candidate.genres
    if candidate.overview and not media_item.overview:
        media_item.overview = candidate.overview
    if candidate.raw and not media_item.raw:
        media_item.raw = candidate.raw


async def refresh_episode_metadata(
    db: AsyncSession,
    user_id: str,
    media_item: MediaItem,
    episode_item: EpisodeItem,
    provider_overrides: dict[str, MetadataProvider] | None = None,
) -> bool:
    """Force-refresh a single episode's metadata, overwriting title/air_date if provider returns them.

    Returns True if any update was made.
    """
    if media_item.media_type != "tv" or not media_item.tmdb_id:
        return False
    overrides = provider_overrides or {}
    tmdb = overrides.get("tmdb")
    if tmdb is None:
        service = MetadataProviderService(db, user_id)
        tmdb = await service.load_provider("tmdb")
    if not tmdb or not isinstance(tmdb, EpisodeMetadataProvider):
        return False
    title_before = episode_item.title
    air_date_before = episode_item.air_date
    await _apply_episode_metadata(tmdb, media_item, episode_item, force=True)
    return episode_item.title != title_before or episode_item.air_date != air_date_before


async def _apply_anime_candidate(db: AsyncSession, media_item: MediaItem, candidate: MediaCandidate) -> None:
    ids = _extract_anime_candidate_ids(candidate)
    await _apply_candidate_ids(db, media_item, ids)
    if candidate.title and is_watch_state_placeholder(media_item):
        media_item.title = candidate.title
    if candidate.poster_url and not media_item.poster_url:
        media_item.poster_url = candidate.poster_url
    if media_item.year is None and candidate.year is not None:
        media_item.year = candidate.year


async def _apply_episode_metadata(
    provider: EpisodeMetadataProvider,
    media_item: MediaItem,
    episode_item: EpisodeItem,
    *,
    force: bool = False,
) -> None:
    if media_item.media_type != "tv":
        return
    if not media_item.tmdb_id:
        return
    if not force and episode_item.tmdb_id and episode_item.title:
        return
    try:
        episodes = await provider.list_episodes(media_item.tmdb_id, episode_item.season_number)
    except Exception as exc:
        logger.warning("%s episode lookup failed for %s: %s", provider.provider, media_item.id, exc)
        return
    for summary in episodes:
        if summary.episode_number != episode_item.episode_number:
            continue
        if summary.provider_id and not episode_item.tmdb_id:
            episode_item.tmdb_id = summary.provider_id
        if summary.title and (force or not episode_item.title):
            episode_item.title = summary.title
        if summary.air_date and (force or not episode_item.air_date):
            episode_item.air_date = _parse_date(summary.air_date)
        break


def _select_candidate(candidates: list[MediaCandidate], scope: str) -> MediaCandidate | None:
    valid = [candidate for candidate in candidates if candidate.provider_id]
    if not valid:
        return None
    for candidate in valid:
        if candidate.media_type == scope:
            return candidate
    return valid[0]


def _scope_for_media_item(media_item: MediaItem) -> str | None:
    if media_item.media_type == "movie":
        return "movie"
    if media_item.media_type == "tv":
        return "tv"
    return None


async def _set_media_id(
    db: AsyncSession,
    media_item: MediaItem,
    field: str,
    value: str | None,
    normalize: bool = False,
) -> None:
    if not value:
        return
    if getattr(media_item, field):
        return
    normalized = value.strip()
    if normalize:
        normalized = normalized.lower()
    if not normalized:
        return
    if not await _can_assign_id(db, media_item, field, normalized):
        logger.warning(
            "Skipping %s=%s for media item %s due to conflict",
            field,
            normalized,
            media_item.id,
        )
        return
    setattr(media_item, field, normalized)


async def _can_assign_id(
    db: AsyncSession,
    media_item: MediaItem,
    field: str,
    value: str,
) -> bool:
    if field == "imdb_id":
        result = await db.execute(select(MediaItem.id).where(MediaItem.imdb_id == value))
    elif field == "tmdb_id":
        result = await db.execute(
            select(MediaItem.id).where(MediaItem.tmdb_id == value, MediaItem.media_type == media_item.media_type)
        )
    elif field == "tvdb_id":
        result = await db.execute(
            select(MediaItem.id).where(MediaItem.tvdb_id == value, MediaItem.media_type == media_item.media_type)
        )
    elif field == "tvmaze_id":
        result = await db.execute(
            select(MediaItem.id).where(MediaItem.tvmaze_id == value, MediaItem.media_type == media_item.media_type)
        )
    elif field == "kitsu_id":
        result = await db.execute(
            select(MediaItem.id).where(MediaItem.kitsu_id == value, MediaItem.media_type == media_item.media_type)
        )
    elif field == "myanimelist_id":
        result = await db.execute(
            select(MediaItem.id).where(
                MediaItem.myanimelist_id == value,
                MediaItem.media_type == media_item.media_type,
            )
        )
    elif field == "anilist_id":
        result = await db.execute(
            select(MediaItem.id).where(MediaItem.anilist_id == value, MediaItem.media_type == media_item.media_type)
        )
    else:
        return True
    existing = result.scalar_one_or_none()
    return existing is None or existing == media_item.id


def _should_update_poster(current: str | None, provider: str) -> bool:
    if not current:
        return True
    if _is_preferred_poster(current):
        return False
    return provider in {"tmdb", "tvdb", "imdb"}


def _is_preferred_poster(url: str | None) -> bool:
    if not url:
        return False
    lowered = url.lower()
    return any(host in lowered for host in PREFERRED_POSTER_HOSTS)


def _select_media_poster(
    candidates: dict[str, MediaCandidate],
    current: str | None,
) -> str | None:
    for provider in POSTER_PROVIDER_ORDER:
        candidate = candidates.get(provider)
        if candidate and candidate.poster_url:
            return candidate.poster_url
    return current


def _extract_anime_candidate_ids(candidate: MediaCandidate) -> dict[str, str]:
    ids: dict[str, str] = {}
    if candidate.provider == "anilist" and candidate.provider_id:
        ids["anilist_id"] = candidate.provider_id
    if candidate.provider == "kitsu" and candidate.provider_id:
        ids["kitsu_id"] = candidate.provider_id
    if candidate.provider == "myanimelist" and candidate.provider_id:
        ids["myanimelist_id"] = candidate.provider_id

    raw = candidate.raw if isinstance(candidate.raw, dict) else {}
    anilist_id = _extract_raw_id(raw, ("anilist_id", "anilistId", "anilistID"))
    if anilist_id:
        ids.setdefault("anilist_id", anilist_id)
    kitsu_id = _extract_raw_id(raw, ("kitsu_id", "kitsuId", "kitsuID"))
    if kitsu_id:
        ids.setdefault("kitsu_id", kitsu_id)
    myanimelist_id = _extract_raw_id(raw, ("myanimelist_id", "myanimelistId", "myanimelistID"))
    if myanimelist_id:
        ids.setdefault("myanimelist_id", myanimelist_id)
    mal_id = _extract_raw_id(raw, ("mal_id", "malId", "malID"))
    if mal_id:
        ids.setdefault("myanimelist_id", mal_id)
    return ids


def _extract_candidate_ids(candidate: MediaCandidate) -> dict[str, str]:
    ids: dict[str, str] = {}
    if candidate.provider == "tmdb" and candidate.provider_id:
        ids["tmdb_id"] = candidate.provider_id
    if candidate.provider == "publicmetadb" and candidate.provider_id:
        ids["tmdb_id"] = candidate.provider_id
    if candidate.provider == "tvdb" and candidate.provider_id:
        ids["tvdb_id"] = candidate.provider_id
    if candidate.provider == "tvmaze" and candidate.provider_id:
        ids["tvmaze_id"] = candidate.provider_id
    if candidate.provider == "kitsu" and candidate.provider_id:
        ids["kitsu_id"] = candidate.provider_id
    if candidate.provider == "myanimelist" and candidate.provider_id:
        ids["myanimelist_id"] = candidate.provider_id
    if candidate.provider == "anilist" and candidate.provider_id:
        ids["anilist_id"] = candidate.provider_id
    if candidate.provider == "imdb" and candidate.provider_id:
        ids["imdb_id"] = candidate.provider_id
    if candidate.imdb_id:
        ids.setdefault("imdb_id", candidate.imdb_id)

    raw = candidate.raw if isinstance(candidate.raw, dict) else {}
    tmdb_id = _extract_raw_id(raw, ("tmdb_id", "tmdbId", "tmdbID"))
    if tmdb_id:
        ids.setdefault("tmdb_id", tmdb_id)
    tvdb_id = _extract_raw_id(raw, ("tvdb_id", "tvdbId", "tvdbID"))
    if tvdb_id:
        ids.setdefault("tvdb_id", tvdb_id)
    tvmaze_id = _extract_raw_id(raw, ("tvmaze_id", "tvmazeId", "tvmazeID"))
    if tvmaze_id:
        ids.setdefault("tvmaze_id", tvmaze_id)
    kitsu_id = _extract_raw_id(raw, ("kitsu_id", "kitsuId", "kitsuID"))
    if kitsu_id:
        ids.setdefault("kitsu_id", kitsu_id)
    myanimelist_id = _extract_raw_id(
        raw,
        ("myanimelist_id", "myanimelistId", "myanimelistID", "mal_id", "malId", "malID"),
    )
    if myanimelist_id:
        ids.setdefault("myanimelist_id", myanimelist_id)
    anilist_id = _extract_raw_id(raw, ("anilist_id", "anilistId", "anilistID"))
    if anilist_id:
        ids.setdefault("anilist_id", anilist_id)
    imdb_id = _extract_raw_id(raw, ("imdb_id", "imdbId", "imdbID"))
    if imdb_id:
        ids.setdefault("imdb_id", imdb_id)
    if "tmdb_id" not in ids:
        tmdb_id = _extract_tmdb_from_remote_ids(raw)
        if tmdb_id:
            ids["tmdb_id"] = tmdb_id
    return ids


def _extract_raw_id(raw: dict, keys: tuple[str, ...]) -> str | None:
    for key in keys:
        value = raw.get(key)
        if value:
            return str(value)
    nested = raw.get("ids")
    if isinstance(nested, dict):
        for key in keys:
            value = nested.get(key)
            if value:
                return str(value)
    return None


def _extract_tmdb_from_remote_ids(raw: dict) -> str | None:
    remote_ids = raw.get("remoteIds") or raw.get("remote_ids") or []
    if not isinstance(remote_ids, list):
        return None
    for entry in remote_ids:
        if not isinstance(entry, dict):
            continue
        source_name = str(entry.get("sourceName") or "").lower()
        entry_type = entry.get("type")
        entry_source = str(entry.get("source") or "").lower()
        if "themoviedb" in source_name or source_name == "tmdb":
            tmdb_value = entry.get("id") or entry.get("value")
            if tmdb_value:
                return str(tmdb_value)
        if entry_source == "tmdb":
            tmdb_value = entry.get("id") or entry.get("value")
            if tmdb_value:
                return str(tmdb_value)
        if str(entry_type).lower() == "tmdb" or entry_type == 10:
            tmdb_value = entry.get("id") or entry.get("value")
            if tmdb_value:
                return str(tmdb_value)
    return None


def _parse_date(value: str | None) -> date | None:
    if not value:
        return None
    try:
        return date.fromisoformat(value)
    except ValueError:
        return None
