from __future__ import annotations

import logging

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from librarysync.connectors.metadata.base import MediaCandidate
from librarysync.connectors.metadata.tmdb import TmdbMetadataProvider
from librarysync.connectors.metadata.tvdb import TvdbMetadataProvider
from librarysync.core.integrations import load_integration_with_secrets
from librarysync.db.models import EpisodeItem, MediaItem, User

logger = logging.getLogger(__name__)

PREFERRED_POSTER_HOSTS = (
    "image.tmdb.org",
    "thetvdb.com",
    "artworks.thetvdb.com",
)


async def enrich_watched_metadata(
    db: AsyncSession,
    user_id: str,
    media_item: MediaItem | None,
    episode_item: EpisodeItem | None,
) -> None:
    if not media_item:
        return
    if media_item.media_type not in {"movie", "tv"}:
        return
    if not _needs_media_enrichment(media_item, episode_item):
        return

    tmdb = await _load_tmdb_provider(db, user_id)
    tvdb = await _load_tvdb_provider(db, user_id)
    if not tmdb and not tvdb:
        return

    if tmdb:
        candidate = await _fetch_tmdb_candidate(tmdb, media_item)
        if candidate:
            await _apply_candidate_to_media_item(db, media_item, candidate)
        if episode_item:
            await _apply_tmdb_episode(tmdb, media_item, episode_item)

    if tvdb:
        candidate = await _fetch_tvdb_candidate(tvdb, media_item)
        if candidate:
            await _apply_candidate_to_media_item(db, media_item, candidate)


def _needs_media_enrichment(
    media_item: MediaItem, episode_item: EpisodeItem | None
) -> bool:
    missing_ids = not media_item.imdb_id or not media_item.tmdb_id or not media_item.tvdb_id
    poster_missing = not media_item.poster_url or not _is_preferred_poster(
        media_item.poster_url
    )
    episode_needs_tmdb = (
        episode_item is not None
        and media_item.media_type == "tv"
        and bool(media_item.tmdb_id)
        and not episode_item.tmdb_id
    )
    return missing_ids or poster_missing or episode_needs_tmdb


async def _fetch_tmdb_candidate(
    provider: TmdbMetadataProvider, media_item: MediaItem
) -> MediaCandidate | None:
    scope = _scope_for_media_item(media_item)
    if scope is None:
        return None
    if media_item.tmdb_id:
        try:
            return await provider.get_details(media_item.tmdb_id, scope)
        except Exception as exc:
            logger.warning("TMDB details failed for %s: %s", media_item.id, exc)
            return None
    if media_item.imdb_id:
        imdb_id = media_item.imdb_id.lower()
        try:
            candidates = await provider.find_by_external_id(imdb_id, scope)
        except Exception as exc:
            logger.warning("TMDB lookup failed for %s: %s", media_item.id, exc)
            return None
        return _select_candidate(candidates, scope)
    return None


async def _fetch_tvdb_candidate(
    provider: TvdbMetadataProvider, media_item: MediaItem
) -> MediaCandidate | None:
    scope = _scope_for_media_item(media_item)
    if scope is None:
        return None
    if media_item.tvdb_id:
        try:
            return await provider.get_details(media_item.tvdb_id, scope)
        except Exception as exc:
            logger.warning("TVDB details failed for %s: %s", media_item.id, exc)
            return None
    if media_item.imdb_id:
        imdb_id = media_item.imdb_id.lower()
        try:
            candidates = await provider.find_by_external_id(imdb_id, scope)
        except Exception as exc:
            logger.warning("TVDB lookup failed for %s: %s", media_item.id, exc)
            return None
        return _select_candidate(candidates, scope)
    return None


async def _apply_candidate_to_media_item(
    db: AsyncSession, media_item: MediaItem, candidate: MediaCandidate
) -> None:
    ids = _extract_candidate_ids(candidate)
    await _set_media_id(db, media_item, "imdb_id", ids.get("imdb_id"), normalize=True)
    await _set_media_id(db, media_item, "tmdb_id", ids.get("tmdb_id"))
    await _set_media_id(db, media_item, "tvdb_id", ids.get("tvdb_id"))
    if candidate.poster_url and _should_update_poster(
        media_item.poster_url, candidate.provider
    ):
        media_item.poster_url = candidate.poster_url


async def _apply_tmdb_episode(
    provider: TmdbMetadataProvider,
    media_item: MediaItem,
    episode_item: EpisodeItem,
) -> None:
    if media_item.media_type != "tv":
        return
    if not media_item.tmdb_id:
        return
    if episode_item.tmdb_id and episode_item.title:
        return
    try:
        episodes = await provider.list_episodes(
            media_item.tmdb_id, episode_item.season_number
        )
    except Exception as exc:
        logger.warning("TMDB episode lookup failed for %s: %s", media_item.id, exc)
        return
    for summary in episodes:
        if summary.episode_number != episode_item.episode_number:
            continue
        if summary.provider_id and not episode_item.tmdb_id:
            episode_item.tmdb_id = summary.provider_id
        if summary.title and not episode_item.title:
            episode_item.title = summary.title
        break


def _select_candidate(
    candidates: list[MediaCandidate], scope: str
) -> MediaCandidate | None:
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
        result = await db.execute(
            select(MediaItem.id).where(MediaItem.imdb_id == value)
        )
    elif field == "tmdb_id":
        result = await db.execute(
            select(MediaItem.id).where(
                MediaItem.tmdb_id == value, MediaItem.media_type == media_item.media_type
            )
        )
    elif field == "tvdb_id":
        result = await db.execute(
            select(MediaItem.id).where(
                MediaItem.tvdb_id == value, MediaItem.media_type == media_item.media_type
            )
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
    return provider in {"tmdb", "tvdb"}


def _is_preferred_poster(url: str | None) -> bool:
    if not url:
        return False
    lowered = url.lower()
    return any(host in lowered for host in PREFERRED_POSTER_HOSTS)


def _extract_candidate_ids(candidate: MediaCandidate) -> dict[str, str]:
    ids: dict[str, str] = {}
    if candidate.provider == "tmdb" and candidate.provider_id:
        ids["tmdb_id"] = candidate.provider_id
    if candidate.provider == "tvdb" and candidate.provider_id:
        ids["tvdb_id"] = candidate.provider_id
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


async def _load_tmdb_provider(
    db: AsyncSession, user_id: str
) -> TmdbMetadataProvider | None:
    integration, secret_data = await load_integration_with_secrets(db, user_id, "tmdb")
    if not integration or not integration.config or not integration.config.get("enabled"):
        return None
    if not secret_data:
        return None
    api_key = secret_data.get("api_key")
    if not api_key:
        return None
    include_adult = await _load_include_adult(db, user_id)
    language = integration.config.get("language")
    region = integration.config.get("region")
    try:
        return TmdbMetadataProvider(
            api_key=str(api_key),
            language=language,
            region=region,
            include_adult=include_adult,
        )
    except ValueError:
        return None


async def _load_tvdb_provider(
    db: AsyncSession, user_id: str
) -> TvdbMetadataProvider | None:
    integration, secret_data = await load_integration_with_secrets(db, user_id, "tvdb")
    if not integration or not integration.config or not integration.config.get("enabled"):
        return None
    if not secret_data:
        return None
    api_key = secret_data.get("api_key")
    if not api_key:
        return None
    pin = secret_data.get("pin")
    language = integration.config.get("language")
    try:
        return TvdbMetadataProvider(api_key=str(api_key), pin=pin, language=language)
    except ValueError:
        return None


async def _load_include_adult(db: AsyncSession, user_id: str) -> bool:
    result = await db.execute(
        select(User.include_adult_in_search).where(User.id == user_id)
    )
    return bool(result.scalar_one_or_none())
