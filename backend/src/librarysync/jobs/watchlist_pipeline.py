from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Iterable, Literal

from sqlalchemy.ext.asyncio import AsyncSession

from librarysync.core.watchlist import upsert_watchlist_item
from librarysync.core.watchlist_sources import (
    reconcile_watchlist_source,
    upsert_watchlist_source_item,
)
from librarysync.db.models import WatchEvent, WatchlistSource
from librarysync.jobs.import_utils import load_existing_entry_keys

MediaType = Literal["movie", "tv"]


@dataclass(frozen=True)
class WatchlistCandidate:
    entry_key: str | None
    media_type: MediaType
    ids: dict[str, str]
    title: str | None
    year: int | None
    poster_url: str | None
    raw: dict[str, Any] | None
    source: str
    external_item_id: str | None = None


async def process_watchlist_candidates(
    db: AsyncSession,
    user_id: str,
    provider: str,
    source: WatchlistSource,
    candidates: Iterable[WatchlistCandidate],
    *,
    now: datetime | None = None,
    commit: bool = True,
    reconcile: bool = True,
) -> int:
    candidate_list = [candidate for candidate in candidates if candidate.ids]
    if not candidate_list:
        return 0
    if now is None:
        now = datetime.now(timezone.utc)
    candidate_entries: list[tuple[WatchlistCandidate, str]] = []
    entry_keys: list[str] = []
    for candidate in candidate_list:
        entry_key = candidate.entry_key or _build_fallback_key(candidate)
        candidate_entries.append((candidate, entry_key))
        entry_keys.append(entry_key)
    existing_entry_keys = await load_existing_entry_keys(
        db,
        user_id,
        f"{provider}_watchlist_imported",
        entry_keys,
    )
    seen: set[str] = set()
    imported = 0
    seen_item_ids: list[str] = []
    for candidate, entry_key in candidate_entries:
        if entry_key in seen:
            continue
        seen.add(entry_key)
        item, status = await upsert_watchlist_item(
            db,
            user_id,
            candidate.media_type,
            candidate.ids,
            candidate.title,
            candidate.year,
            candidate.poster_url,
            candidate.source,
            now=now,
            event_raw={"provider": provider},
        )
        if not item:
            continue
        await upsert_watchlist_source_item(
            db,
            source,
            item,
            external_item_id=candidate.external_item_id,
            now=now,
        )
        seen_item_ids.append(item.id)
        if status in {"created", "restored"} and entry_key not in existing_entry_keys:
            imported += 1
            event = WatchEvent(
                user_id=user_id,
                media_item_id=item.media_item_id,
                event_type=f"{provider}_watchlist_imported",
                entry_key=entry_key,
                occurred_at=now,
                raw=candidate.raw,
            )
            db.add(event)
    if reconcile:
        await reconcile_watchlist_source(db, source, now=now, seen_item_ids=seen_item_ids)
    if commit and not reconcile:
        await db.commit()
    return imported


def _build_fallback_key(candidate: WatchlistCandidate) -> str:
    for key in (
        "imdb_id",
        "tmdb_id",
        "tvdb_id",
        "tvmaze_id",
        "kitsu_id",
        "myanimelist_id",
        "anilist_id",
        "letterboxd_film_id",
    ):
        value = candidate.ids.get(key)
        if value:
            return f"{candidate.media_type}:{key}:{value}"
    return f"{candidate.media_type}:{candidate.title or 'unknown'}"
