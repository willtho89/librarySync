from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable, Iterable, Literal

from sqlalchemy.ext.asyncio import AsyncSession

from librarysync.core.blacklist import (
    find_blacklisted_show,
    normalize_id,
    normalize_imdb_id,
)
from librarysync.core.watch_pipeline import enqueue_new_item_job
from librarysync.db.models import (
    EpisodeItem,
    MediaItem,
    WatchedItem,
    WatchEvent,
    WatchSync,
)
from librarysync.jobs.import_utils import load_existing_entry_keys

logger = logging.getLogger(__name__)

MediaType = Literal["movie", "show", "episode"]


@dataclass(frozen=True)
class BlacklistIds:
    imdb_id: str | None = None
    tmdb_id: str | None = None
    tvdb_id: str | None = None
    tvmaze_id: str | None = None


@dataclass(frozen=True)
class BlacklistCacheKey:
    imdb_id: str | None
    tmdb_id: str | None
    tvdb_id: str | None
    tvmaze_id: str | None


@dataclass(frozen=True)
class ImportItems:
    media_item: MediaItem | None
    episode_item: EpisodeItem | None
    show_item: MediaItem | None


BuildItems = Callable[[AsyncSession], Awaitable[ImportItems]]


@dataclass(frozen=True)
class ImportCandidate:
    entry_key: str
    watched_at: datetime
    media_type: MediaType
    raw: dict[str, Any]
    rating: float | None
    external_id: str | None
    blacklist_ids: BlacklistIds | None
    blacklist_enabled: bool
    is_rewatch: bool
    build_items: BuildItems


async def process_import_candidates(
    db: AsyncSession,
    user_id: str,
    provider: str,
    candidates: Iterable[ImportCandidate],
    *,
    now: datetime | None = None,
    commit: bool = True,
    existing_entry_keys: set[str] | None = None,
    existing_blacklist_keys: set[str] | None = None,
) -> int:
    candidate_list = [candidate for candidate in candidates if candidate.entry_key]
    if not candidate_list:
        return 0
    if now is None:
        now = datetime.now(timezone.utc)
    entry_keys = [candidate.entry_key for candidate in candidate_list]
    if existing_entry_keys is None:
        existing_entry_keys = await load_existing_entry_keys(
            db,
            user_id,
            f"{provider}_imported",
            entry_keys,
        )
    if existing_blacklist_keys is None and any(
        candidate.blacklist_enabled for candidate in candidate_list
    ):
        existing_blacklist_keys = await load_existing_entry_keys(
            db,
            user_id,
            f"{provider}_blacklisted",
            entry_keys,
        )
    if not commit:
        return await _process_candidates_in_transaction(
            db,
            user_id,
            provider,
            candidate_list,
            existing_entry_keys,
            existing_blacklist_keys,
            now,
        )
    if not db.in_transaction():
        await db.begin()
    try:
        imported = await _process_candidates_in_transaction(
            db,
            user_id,
            provider,
            candidate_list,
            existing_entry_keys,
            existing_blacklist_keys,
            now,
        )
    except Exception:
        await db.rollback()
        raise
    await db.commit()
    return imported


async def _process_candidates_in_transaction(
    db: AsyncSession,
    user_id: str,
    provider: str,
    candidate_list: list[ImportCandidate],
    existing_entry_keys: set[str],
    existing_blacklist_keys: set[str] | None,
    now: datetime,
) -> int:
    seen: set[str] = set()
    imported = 0
    blacklist_cache: dict[BlacklistCacheKey, Any] = {}
    for candidate in candidate_list:
        entry_key = candidate.entry_key
        if not entry_key:
            continue
        if entry_key in seen or entry_key in existing_entry_keys:
            continue
        seen.add(entry_key)
        try:
            async with db.begin_nested():
                imported += await _process_candidate(
                    db,
                    user_id,
                    provider,
                    candidate,
                    existing_blacklist_keys,
                    blacklist_cache,
                    now,
                )
        except Exception:
            logger.exception("%s entry import failed for user %s", provider, user_id)
    return imported


async def _process_candidate(
    db: AsyncSession,
    user_id: str,
    provider: str,
    candidate: ImportCandidate,
    existing_blacklist_keys: set[str] | None,
    blacklist_cache: dict[BlacklistCacheKey, Any],
    now: datetime,
) -> int:
    items = await candidate.build_items(db)
    media_item_id, episode_item_id, _ = _select_event_items(candidate, items)
    if candidate.media_type == "episode":
        if episode_item_id is None:
            return 0
    else:
        if media_item_id is None:
            return 0

    if candidate.blacklist_enabled:
        blacklist_match = await _resolve_blacklist_match(
            db,
            user_id,
            candidate,
            items,
            blacklist_cache,
        )
        if blacklist_match is not None:
            if existing_blacklist_keys is not None:
                if candidate.entry_key in existing_blacklist_keys:
                    return 0
            raw = dict(candidate.raw)
            raw["blacklisted"] = True
            raw["blacklist_id"] = blacklist_match.id
            event = WatchEvent(
                user_id=user_id,
                media_item_id=media_item_id,
                episode_item_id=episode_item_id,
                event_type=f"{provider}_blacklisted",
                entry_key=candidate.entry_key,
                occurred_at=candidate.watched_at,
                raw=raw,
            )
            db.add(event)
            await db.flush()
            return 0

    watched = WatchedItem(
        user_id=user_id,
        media_item_id=media_item_id,
        episode_item_id=episode_item_id,
        watched_at=candidate.watched_at,
        rating=candidate.rating,
        source=provider,
    )
    event = WatchEvent(
        user_id=user_id,
        media_item_id=media_item_id,
        episode_item_id=episode_item_id,
        event_type=f"{provider}_imported",
        entry_key=candidate.entry_key,
        occurred_at=candidate.watched_at,
        raw=candidate.raw,
    )
    db.add_all([watched, event])
    await db.flush()
    watch_sync = WatchSync(
        user_id=user_id,
        watched_item_id=watched.id,
        provider=provider,
        status=f"synced_from_{provider}",
        is_rewatch=candidate.is_rewatch,
        external_id=candidate.external_id,
        last_synced_at=now,
    )
    db.add(watch_sync)
    await enqueue_new_item_job(
        db,
        user_id,
        watched.id,
        is_rewatch=candidate.is_rewatch,
        source=f"{provider}_import",
    )
    return 1


def _select_event_items(
    candidate: ImportCandidate, items: ImportItems
) -> tuple[str | None, str | None, MediaItem | None]:
    if candidate.media_type == "episode":
        return None, items.episode_item.id if items.episode_item else None, items.show_item
    if candidate.media_type == "show":
        return items.media_item.id if items.media_item else None, None, items.media_item
    return items.media_item.id if items.media_item else None, None, items.show_item


async def _resolve_blacklist_match(
    db: AsyncSession,
    user_id: str,
    candidate: ImportCandidate,
    items: ImportItems,
    cache: dict[BlacklistCacheKey, Any],
) -> Any:
    ids = _resolve_blacklist_ids(candidate, items)
    if not ids:
        return None
    normalized = _normalize_blacklist_ids(ids)
    if not (
        normalized.imdb_id
        or normalized.tmdb_id
        or normalized.tvdb_id
        or normalized.tvmaze_id
    ):
        return None
    cache_key = BlacklistCacheKey(
        imdb_id=normalized.imdb_id,
        tmdb_id=normalized.tmdb_id,
        tvdb_id=normalized.tvdb_id,
        tvmaze_id=normalized.tvmaze_id,
    )
    if cache_key in cache:
        return cache[cache_key]
    match = await find_blacklisted_show(
        db,
        user_id,
        imdb_id=normalized.imdb_id,
        tmdb_id=normalized.tmdb_id,
        tvdb_id=normalized.tvdb_id,
        tvmaze_id=normalized.tvmaze_id,
    )
    cache[cache_key] = match
    return match


def _resolve_blacklist_ids(
    candidate: ImportCandidate, items: ImportItems
) -> BlacklistIds | None:
    if not candidate.blacklist_ids and not items.show_item and not items.media_item:
        return None
    ids = candidate.blacklist_ids or BlacklistIds()
    item = items.show_item or items.media_item
    if not item:
        return ids
    return BlacklistIds(
        imdb_id=item.imdb_id or ids.imdb_id,
        tmdb_id=item.tmdb_id or ids.tmdb_id,
        tvdb_id=item.tvdb_id or ids.tvdb_id,
        tvmaze_id=item.tvmaze_id or ids.tvmaze_id,
    )


def _normalize_blacklist_ids(ids: BlacklistIds) -> BlacklistIds:
    return BlacklistIds(
        imdb_id=normalize_imdb_id(ids.imdb_id),
        tmdb_id=normalize_id(ids.tmdb_id),
        tvdb_id=normalize_id(ids.tvdb_id),
        tvmaze_id=normalize_id(ids.tvmaze_id),
    )
