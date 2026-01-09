from __future__ import annotations

import logging
import re
from datetime import date, datetime

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from librarysync.connectors.metadata.base import (
    EpisodeMetadataProvider,
    MediaCandidate,
    MetadataProvider,
)
from librarysync.core.anime import is_anime
from librarysync.core.metadata_providers import MetadataProviderService
from librarysync.db.models import EpisodeItem, MediaItem

logger = logging.getLogger(__name__)

PREFERRED_POSTER_HOSTS = (
    "image.tmdb.org",
    "thetvdb.com",
    "artworks.thetvdb.com",
)
ANIME_PROVIDERS = ("myanimelist", "kitsu", "anilist")


async def enrich_watched_metadata(
    db: AsyncSession,
    user_id: str,
    media_item: MediaItem | None,
    episode_item: EpisodeItem | None,
) -> None:
    if not media_item:
        return
    if is_anime(media_item):
        await _enrich_anime_metadata(db, user_id, media_item)
    if media_item.media_type not in {"movie", "tv"}:
        return
    if not _needs_media_enrichment(media_item, episode_item):
        return

    if await _apply_local_metadata(db, media_item):
        if not _needs_media_enrichment(media_item, episode_item):
            return

    service = MetadataProviderService(db, user_id)
    tmdb = await service.load_provider("tmdb")
    tvdb = await service.load_provider("tvdb")
    if not tmdb and not tvdb:
        return

    if tmdb:
        candidate = await _fetch_provider_candidate(tmdb, media_item, "tmdb_id")
        if candidate:
            await _apply_candidate_to_media_item(db, media_item, candidate)
        if episode_item and isinstance(tmdb, EpisodeMetadataProvider):
            await _apply_episode_metadata(tmdb, media_item, episode_item)

    if tvdb:
        candidate = await _fetch_provider_candidate(tvdb, media_item, "tvdb_id")
        if candidate:
            await _apply_candidate_to_media_item(db, media_item, candidate)


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
    return missing_ids or poster_missing or missing_year or episode_needs_tmdb


def _needs_anime_enrichment(media_item: MediaItem) -> bool:
    return any(
        value is None
        for value in (
            media_item.anilist_id,
            media_item.kitsu_id,
            media_item.myanimelist_id,
        )
    )


async def _enrich_anime_metadata(db: AsyncSession, user_id: str, media_item: MediaItem) -> None:
    if not _needs_anime_enrichment(media_item):
        return
    if not media_item.title:
        return
    service = MetadataProviderService(db, user_id)
    providers: list[MetadataProvider] = []
    for provider_name in ANIME_PROVIDERS:
        provider = await service.load_provider(provider_name)
        if provider:
            providers.append(provider)
    if not providers:
        return
    for provider in providers:
        if _anime_id_present(media_item, provider.provider):
            continue
        candidate = await _find_anime_candidate(provider, media_item)
        if not candidate:
            continue
        await _apply_anime_candidate(db, media_item, candidate)


def _anime_id_present(media_item: MediaItem, provider: str) -> bool:
    if provider == "anilist":
        return bool(media_item.anilist_id)
    if provider == "kitsu":
        return bool(media_item.kitsu_id)
    if provider == "myanimelist":
        return bool(media_item.myanimelist_id)
    return False


async def _find_anime_candidate(
    provider: MetadataProvider, media_item: MediaItem
) -> MediaCandidate | None:
    scope = "anime"
    if provider.provider == "anilist" and media_item.myanimelist_id:
        try:
            candidates = await provider.find_by_external_id(
                f"mal:{media_item.myanimelist_id}", scope
            )
        except Exception as exc:
            logger.warning(
                "%s anime MAL lookup failed for %s: %s",
                provider.provider,
                media_item.id,
                exc,
            )
        else:
            if candidates:
                return candidates[0]
    if provider.capabilities.supports_external_id and media_item.imdb_id:
        imdb_id = media_item.imdb_id.lower()
        try:
            candidates = await provider.find_by_external_id(imdb_id, scope)
        except Exception as exc:
            logger.warning(
                "%s anime lookup failed for %s: %s", provider.provider, media_item.id, exc
            )
        else:
            selected = _select_anime_candidate(candidates, media_item)
            if selected:
                return selected
    try:
        candidates = await provider.search(media_item.title, scope)
    except Exception as exc:
        logger.warning("%s anime search failed for %s: %s", provider.provider, media_item.id, exc)
        return None
    return _select_anime_candidate(candidates, media_item)


def _select_anime_candidate(
    candidates: list[MediaCandidate], media_item: MediaItem
) -> MediaCandidate | None:
    if not candidates:
        return None
    target_key = _normalize_title_key(media_item.title)
    if not target_key:
        return None
    matches = [
        candidate for candidate in candidates if _normalize_title_key(candidate.title) == target_key
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
    if (not media_item.title or media_item.title.startswith("Stremio ")) and candidate.title:
        media_item.title = candidate.title
        updated = True
    if media_item.year is None and candidate.year is not None:
        media_item.year = candidate.year
        updated = True
    return updated


async def _find_local_metadata_candidate(
    db: AsyncSession, media_item: MediaItem
) -> MediaItem | None:
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
    provider: MetadataProvider, media_item: MediaItem, provider_id_field: str
) -> MediaCandidate | None:
    scope = _scope_for_media_item(media_item)
    if scope is None:
        return None
    provider_id = getattr(media_item, provider_id_field)
    if provider_id:
        try:
            return await provider.get_details(provider_id, scope)
        except Exception as exc:
            logger.warning("%s details failed for %s: %s", provider.provider, media_item.id, exc)
            return None
    if media_item.imdb_id:
        imdb_id = media_item.imdb_id.lower()
        try:
            candidates = await provider.find_by_external_id(imdb_id, scope)
        except Exception as exc:
            logger.warning("%s lookup failed for %s: %s", provider.provider, media_item.id, exc)
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
    if candidate.poster_url and _should_update_poster(media_item.poster_url, candidate.provider):
        media_item.poster_url = candidate.poster_url
    if media_item.year is None and candidate.year is not None:
        media_item.year = candidate.year

    if not media_item.release_date and candidate.release_date:
        media_item.release_date = _parse_date(candidate.release_date)
    if not media_item.first_air_date and candidate.first_air_date:
        media_item.first_air_date = _parse_date(candidate.first_air_date)
    if not media_item.last_air_date and candidate.last_air_date:
        media_item.last_air_date = _parse_date(candidate.last_air_date)


async def _apply_anime_candidate(
    db: AsyncSession, media_item: MediaItem, candidate: MediaCandidate
) -> None:
    ids = _extract_anime_candidate_ids(candidate)
    await _set_media_id(db, media_item, "anilist_id", ids.get("anilist_id"))
    await _set_media_id(db, media_item, "kitsu_id", ids.get("kitsu_id"))
    await _set_media_id(db, media_item, "myanimelist_id", ids.get("myanimelist_id"))
    if candidate.poster_url and not media_item.poster_url:
        media_item.poster_url = candidate.poster_url
    if media_item.year is None and candidate.year is not None:
        media_item.year = candidate.year


async def _apply_episode_metadata(
    provider: EpisodeMetadataProvider,
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
        episodes = await provider.list_episodes(media_item.tmdb_id, episode_item.season_number)
    except Exception as exc:
        logger.warning("%s episode lookup failed for %s: %s", provider.provider, media_item.id, exc)
        return
    for summary in episodes:
        if summary.episode_number != episode_item.episode_number:
            continue
        if summary.provider_id and not episode_item.tmdb_id:
            episode_item.tmdb_id = summary.provider_id
        if summary.title and not episode_item.title:
            episode_item.title = summary.title
        if summary.air_date and not episode_item.air_date:
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
    elif field == "kitsu_id":
        result = await db.execute(
            select(MediaItem.id).where(
                MediaItem.kitsu_id == value, MediaItem.media_type == media_item.media_type
            )
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
            select(MediaItem.id).where(
                MediaItem.anilist_id == value, MediaItem.media_type == media_item.media_type
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


def _parse_date(value: str | None) -> date | None:
    if not value:
        return None
    try:
        return datetime.strptime(value, "%Y-%m-%d").date()
    except ValueError:
        return None
