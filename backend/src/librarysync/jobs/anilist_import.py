from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from librarysync.config import settings
from librarysync.connectors.services.anilist import (
    AniListClient,
    AniListError,
    has_required_anilist_fields,
)
from librarysync.core.integrations import load_integration_with_secrets
from librarysync.core.ratings import normalize_ten_point_rating
from librarysync.core.watch_pipeline import enqueue_new_item_job
from librarysync.db.models import (
    EpisodeItem,
    Integration,
    MediaItem,
    WatchedItem,
    WatchEvent,
    WatchSync,
)
from librarysync.jobs.import_base import ImportContext, ImportResult, ImportStrategy
from librarysync.jobs.import_utils import chunked, load_existing_entry_keys

LOOKBACK_DAYS = settings.history_lookback_days
PER_PAGE = 50
MAX_PAGES = 8
ENTRY_KEY_BATCH_SIZE = 200
logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class AnimeSummary:
    anilist_id: str
    myanimelist_id: str | None
    title: str
    year: int | None
    poster_url: str | None
    format: str | None
    episodes: int | None
    raw: dict[str, Any]


class AniListImportStrategy(ImportStrategy):
    provider = "anilist"

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
    if not settings.anilist_client_id or not settings.anilist_client_secret:
        return ImportResult(imported=0, attempted=False)
    integration, secret_data = await load_integration_with_secrets(
        db, integration.user_id, "anilist"
    )
    if not integration or not secret_data:
        return ImportResult(imported=0, attempted=False)
    if not has_required_anilist_fields(secret_data):
        return ImportResult(imported=0, attempted=False)
    access_token = _coerce_str(secret_data.get("access_token"))
    if not access_token:
        return ImportResult(imported=0, attempted=False)

    client = AniListClient(access_token=access_token)
    try:
        user_id, user_name = await _resolve_viewer(db, integration, client)
    except AniListError as exc:
        logger.warning(
            "AniList viewer lookup failed for user %s: %s",
            integration.user_id,
            exc,
        )
        return ImportResult(imported=0, attempted=True)

    try:
        entries = await _fetch_entries(
            client,
            user_id,
            user_name,
            lookback_days,
            per_page,
            max_pages,
        )
    except AniListError as exc:
        logger.warning(
            "AniList history fetch failed for user %s: %s",
            integration.user_id,
            exc,
        )
        return ImportResult(imported=0, attempted=True)

    imported = 0
    for batch in chunked(entries, ENTRY_KEY_BATCH_SIZE):
        entry_keys = _prefetch_entry_keys(batch)
        existing_keys = await load_existing_entry_keys(
            db,
            integration.user_id,
            "anilist_imported",
            entry_keys,
        )
        for entry in batch:
            try:
                if await _import_entry(db, integration.user_id, entry, existing_keys):
                    imported += 1
            except Exception:
                logger.exception(
                    "AniList entry import failed for user %s",
                    integration.user_id,
                )
                await db.rollback()
    if imported:
        logger.info(
            "Imported %s AniList entries for user %s",
            imported,
            integration.user_id,
        )
    return ImportResult(imported=imported, attempted=True)


async def _resolve_viewer(
    db: AsyncSession,
    integration: Integration,
    client: AniListClient,
) -> tuple[int | None, str | None]:
    config = dict(integration.config or {})
    user_id = _coerce_int(config.get("anilist_user_id"))
    user_name = _coerce_str(config.get("anilist_username"))
    if user_id or user_name:
        return user_id, user_name
    viewer = await client.get_viewer()
    user_id = _coerce_int(viewer.get("id"))
    user_name = _coerce_str(viewer.get("name"))
    if user_id or user_name:
        if user_id is not None:
            config["anilist_user_id"] = user_id
        if user_name:
            config["anilist_username"] = user_name
        integration.config = config
        db.add(integration)
        await db.commit()
    return user_id, user_name


async def _fetch_entries(
    client: AniListClient,
    user_id: int | None,
    user_name: str | None,
    lookback_days: int,
    per_page: int,
    max_pages: int,
) -> list[dict[str, Any]]:
    now = datetime.now(timezone.utc)
    since = None if lookback_days < 0 else now - timedelta(days=lookback_days)
    limit_pages = None if lookback_days < 0 else max_pages
    statuses = ["COMPLETED", "CURRENT", "REPEATING"]
    entries: list[dict[str, Any]] = []
    seen: set[str] = set()
    errors: list[AniListError] = []
    for status in statuses:
        try:
            status_entries = await client.list_media_entries(
                user_id=user_id,
                user_name=user_name,
                status=status,
                per_page=per_page,
                max_pages=limit_pages,
                sort="UPDATED_TIME_DESC",
            )
        except AniListError as exc:
            errors.append(exc)
            logger.warning(
                "AniList history fetch failed for status %s (user %s): %s",
                status,
                user_id,
                exc,
            )
            continue
        for entry in status_entries:
            entry_id = _coerce_str(entry.get("id"))
            if entry_id and entry_id in seen:
                continue
            if entry_id:
                seen.add(entry_id)
            entries.append(entry)
    if not entries:
        if errors:
            raise errors[0]
        logger.info("AniList import fetched 0 entries (statuses: %s)", ", ".join(statuses))
        return []
    if not since:
        entries.sort(key=_sort_updated_at, reverse=True)
        logger.info(
            "AniList import fetched %s entries (statuses: %s)",
            len(entries),
            ", ".join(statuses),
        )
        return entries
    filtered: list[dict[str, Any]] = []
    for entry in entries:
        updated_at = _parse_timestamp(entry.get("updatedAt"))
        if updated_at and updated_at < since:
            continue
        watched_at = _select_watched_at(entry)
        if watched_at and watched_at < since:
            continue
        filtered.append(entry)
    filtered.sort(key=_sort_updated_at, reverse=True)
    logger.info(
        "AniList import fetched %s entries (%s after lookback filter)",
        len(entries),
        len(filtered),
    )
    return filtered


async def _import_entry(
    db: AsyncSession,
    user_id: str,
    entry: dict[str, Any],
    existing_entry_keys: set[str] | None = None,
) -> bool:
    entry_id = _coerce_str(entry.get("id"))
    summary = _extract_anime_summary(entry)
    if not summary:
        return False
    watched_at = _select_watched_at(entry) or datetime.now(timezone.utc)
    progress = _select_episode_progress(entry)
    if _should_import_episodes(summary.format, progress):
        return await _import_episode_entries(
            db,
            user_id,
            entry,
            summary,
            watched_at,
            progress,
            existing_entry_keys,
        )

    entry_key = _build_media_entry_key(entry_id, summary.anilist_id, watched_at)
    if not entry_key:
        return False
    if existing_entry_keys is not None:
        if entry_key in existing_entry_keys:
            return False
    elif await _entry_already_imported(db, user_id, entry_key):
        return False

    media_item = await _get_or_create_media_item(db, summary)
    if not media_item:
        return False

    rating = normalize_ten_point_rating(entry.get("score"))
    watched = WatchedItem(
        user_id=user_id,
        media_item_id=media_item.id,
        episode_item_id=None,
        watched_at=watched_at,
        rating=rating,
        source="anilist",
    )
    event = WatchEvent(
        user_id=user_id,
        media_item_id=media_item.id,
        episode_item_id=None,
        event_type="anilist_imported",
        occurred_at=watched_at,
        raw=_build_event_raw(entry_key, entry_id, watched_at, summary, entry, rating),
    )
    db.add_all([watched, event])
    await db.flush()
    watch_sync = WatchSync(
        user_id=user_id,
        watched_item_id=watched.id,
        provider="anilist",
        status="synced_from_anilist",
        is_rewatch=False,
        external_id=entry_id,
        last_synced_at=datetime.now(timezone.utc),
    )
    db.add(watch_sync)
    await enqueue_new_item_job(
        db,
        user_id,
        watched.id,
        is_rewatch=False,
        source="anilist_import",
    )
    await db.commit()
    return True


def _prefetch_entry_keys(entries: list[dict[str, Any]]) -> list[str]:
    keys: list[str] = []
    for entry in entries:
        entry_id = _coerce_str(entry.get("id"))
        media = entry.get("media")
        media_dict = media if isinstance(media, dict) else {}
        anilist_id = _coerce_str(media_dict.get("id"))
        progress = _select_episode_progress(entry)
        if _should_import_episodes(_coerce_str(media_dict.get("format")), progress):
            if not anilist_id:
                continue
            for episode_number in range(1, progress + 1):
                key = _build_episode_entry_key(entry_id, anilist_id, episode_number)
                if key:
                    keys.append(key)
            continue
        watched_at = _select_watched_at(entry) or datetime.now(timezone.utc)
        key = _build_media_entry_key(entry_id, anilist_id, watched_at)
        if key:
            keys.append(key)
    return keys


async def _import_episode_entries(
    db: AsyncSession,
    user_id: str,
    entry: dict[str, Any],
    summary: AnimeSummary,
    watched_at: datetime,
    progress: int,
    existing_entry_keys: set[str] | None = None,
) -> bool:
    if progress <= 0:
        return False
    media_item = await _get_or_create_media_item(db, summary)
    if not media_item:
        return False
    entry_id = _coerce_str(entry.get("id"))
    imported = False
    for episode_number in range(1, progress + 1):
        entry_key = _build_episode_entry_key(entry_id, summary.anilist_id, episode_number)
        if not entry_key:
            continue
        if existing_entry_keys is not None:
            if entry_key in existing_entry_keys:
                continue
        elif await _entry_already_imported(db, user_id, entry_key):
            continue
        episode_item = await _get_or_create_episode_item(
            db,
            media_item.id,
            episode_number,
            entry_id,
        )
        if not episode_item:
            continue
        watched = WatchedItem(
            user_id=user_id,
            media_item_id=None,
            episode_item_id=episode_item.id,
            watched_at=watched_at,
            rating=None,
            source="anilist",
        )
        event = WatchEvent(
            user_id=user_id,
            media_item_id=None,
            episode_item_id=episode_item.id,
            event_type="anilist_imported",
            occurred_at=watched_at,
            raw=_build_episode_event_raw(
                entry_key,
                entry_id,
                watched_at,
                summary,
                episode_number,
                entry,
            ),
        )
        db.add_all([watched, event])
        await db.flush()
        watch_sync = WatchSync(
            user_id=user_id,
            watched_item_id=watched.id,
            provider="anilist",
            status="synced_from_anilist",
            is_rewatch=False,
            external_id=entry_id,
            last_synced_at=datetime.now(timezone.utc),
        )
        db.add(watch_sync)
        await enqueue_new_item_job(
            db,
            user_id,
            watched.id,
            is_rewatch=False,
            source="anilist_import",
        )
        imported = True
    if imported:
        await db.commit()
    return imported


async def _get_or_create_episode_item(
    db: AsyncSession,
    show_media_item_id: str,
    episode_number: int,
    entry_id: str | None,
) -> EpisodeItem | None:
    result = await db.execute(
        select(EpisodeItem).where(
            EpisodeItem.show_media_item_id == show_media_item_id,
            EpisodeItem.season_number == 1,
            EpisodeItem.episode_number == episode_number,
        )
    )
    item = result.scalars().first()
    if item:
        _apply_episode_updates(item, episode_number, entry_id)
        return item
    item = EpisodeItem(
        show_media_item_id=show_media_item_id,
        season_number=1,
        episode_number=episode_number,
        title=_default_episode_title(episode_number),
        tmdb_id=None,
        tvdb_id=None,
        imdb_id=None,
        raw=_build_episode_raw(entry_id),
    )
    db.add(item)
    await db.flush()
    return item


async def _entry_already_imported(
    db: AsyncSession, user_id: str, entry_key: str
) -> bool:
    result = await db.execute(
        select(WatchEvent.id).where(
            WatchEvent.user_id == user_id,
            WatchEvent.event_type == "anilist_imported",
            WatchEvent.raw["entry_key"].as_string() == entry_key,
        )
    )
    return result.scalars().first() is not None


async def _get_or_create_media_item(
    db: AsyncSession, summary: AnimeSummary
) -> MediaItem | None:
    item = await _find_media_item(db, summary.anilist_id, summary.myanimelist_id)
    if item:
        _apply_media_updates(item, summary)
        return item
    item = MediaItem(
        media_type="anime",
        title=summary.title or "AniList anime",
        year=summary.year,
        anilist_id=summary.anilist_id,
        myanimelist_id=summary.myanimelist_id,
        poster_url=summary.poster_url,
        raw=_build_media_raw(summary),
    )
    db.add(item)
    await db.flush()
    return item


async def _find_media_item(
    db: AsyncSession,
    anilist_id: str | None,
    myanimelist_id: str | None,
) -> MediaItem | None:
    item: MediaItem | None = None
    if anilist_id:
        result = await db.execute(
            select(MediaItem).where(
                MediaItem.media_type == "anime",
                MediaItem.anilist_id == anilist_id,
            )
        )
        item = result.scalars().first()
    if myanimelist_id:
        result = await db.execute(
            select(MediaItem).where(
                MediaItem.media_type == "anime",
                MediaItem.myanimelist_id == myanimelist_id,
            )
        )
        mal_item = result.scalars().first()
        if item and mal_item and item.id != mal_item.id:
            return item
        if not item:
            item = mal_item
    return item


def _apply_media_updates(item: MediaItem, summary: AnimeSummary) -> None:
    if summary.anilist_id and not item.anilist_id:
        item.anilist_id = summary.anilist_id
    if summary.myanimelist_id and not item.myanimelist_id:
        item.myanimelist_id = summary.myanimelist_id
    if summary.year is not None and item.year is None:
        item.year = summary.year
    if summary.poster_url and not item.poster_url:
        item.poster_url = summary.poster_url
    if summary.title and item.title.startswith("AniList"):
        item.title = summary.title
    item.raw = _merge_media_raw(item.raw, summary)


def _build_media_raw(summary: AnimeSummary) -> dict[str, Any]:
    raw: dict[str, Any] = {
        "source": "anilist",
        "type": "anime",
        "anilist_id": summary.anilist_id,
    }
    if summary.myanimelist_id:
        raw["mal_id"] = summary.myanimelist_id
    if summary.format:
        raw["format"] = summary.format
    if summary.episodes is not None:
        raw["episodes"] = summary.episodes
    if summary.raw:
        raw["anilist"] = summary.raw
    return raw


def _merge_media_raw(existing: dict | None, summary: AnimeSummary) -> dict[str, Any]:
    raw = existing if isinstance(existing, dict) else {}
    if summary.anilist_id and not raw.get("anilist_id"):
        raw["anilist_id"] = summary.anilist_id
    if summary.myanimelist_id and not raw.get("mal_id"):
        raw["mal_id"] = summary.myanimelist_id
    if summary.format and not raw.get("format"):
        raw["format"] = summary.format
    if summary.episodes is not None and raw.get("episodes") is None:
        raw["episodes"] = summary.episodes
    if summary.raw and not raw.get("anilist"):
        raw["anilist"] = summary.raw
    if raw.get("type") is None:
        raw["type"] = "anime"
    if raw.get("source") is None:
        raw["source"] = "anilist"
    return raw


def _build_media_entry_key(
    entry_id: str | None,
    anilist_id: str | None,
    watched_at: datetime,
) -> str | None:
    if entry_id:
        return f"entry:{entry_id}"
    if anilist_id:
        return f"anime:{anilist_id}:{watched_at.date().isoformat()}"
    return None


def _build_episode_entry_key(
    entry_id: str | None,
    anilist_id: str | None,
    episode_number: int,
) -> str | None:
    if entry_id:
        return f"entry:{entry_id}:ep:{episode_number}"
    if anilist_id:
        return f"anime:{anilist_id}:ep:{episode_number}"
    return None


def _build_event_raw(
    entry_key: str,
    entry_id: str | None,
    watched_at: datetime,
    summary: AnimeSummary,
    entry: dict[str, Any],
    rating: float | None,
) -> dict[str, Any]:
    raw: dict[str, Any] = {
        "source": "anilist",
        "entry_key": entry_key,
        "entry_id": entry_id,
        "watched_at": watched_at.isoformat(),
        "ids": {
            "anilist": summary.anilist_id,
            "mal": summary.myanimelist_id,
        },
        "format": summary.format,
        "episodes": summary.episodes,
    }
    status = _coerce_str(entry.get("status"))
    if status:
        raw["status"] = status
    progress = _coerce_int(entry.get("progress"))
    if progress is not None:
        raw["progress"] = progress
    if rating is not None:
        raw["rating"] = rating
    entry_payload = _sanitize_entry(entry)
    if entry_payload:
        raw["anilist"] = entry_payload
    return raw


def _build_episode_event_raw(
    entry_key: str,
    entry_id: str | None,
    watched_at: datetime,
    summary: AnimeSummary,
    episode_number: int,
    entry: dict[str, Any],
) -> dict[str, Any]:
    raw = _build_event_raw(entry_key, entry_id, watched_at, summary, entry, None)
    raw["episode"] = {
        "season": 1,
        "number": episode_number,
    }
    return raw


def _extract_anime_summary(entry: dict[str, Any]) -> AnimeSummary | None:
    media = entry.get("media")
    if not isinstance(media, dict):
        return None
    anilist_id = _coerce_str(media.get("id"))
    if not anilist_id:
        return None
    mal_id = _coerce_str(media.get("idMal"))
    title = _normalize_title(media.get("title")) or "AniList anime"
    year = _extract_year(media.get("startDate"))
    poster_url = _poster_url(media.get("coverImage"))
    raw = _sanitize_media_payload(media)
    return AnimeSummary(
        anilist_id=anilist_id,
        myanimelist_id=mal_id,
        title=title,
        year=year,
        poster_url=poster_url,
        format=_coerce_str(media.get("format")),
        episodes=_coerce_int(media.get("episodes")),
        raw=raw,
    )


def _sanitize_media_payload(media: dict[str, Any]) -> dict[str, Any]:
    payload = {
        "id": media.get("id"),
        "idMal": media.get("idMal"),
        "format": media.get("format"),
        "episodes": media.get("episodes"),
        "title": media.get("title"),
        "startDate": media.get("startDate"),
        "coverImage": media.get("coverImage"),
    }
    return {key: value for key, value in payload.items() if value is not None}


def _sanitize_entry(entry: dict[str, Any]) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "id": entry.get("id"),
        "status": entry.get("status"),
        "score": entry.get("score"),
        "progress": entry.get("progress"),
        "updatedAt": entry.get("updatedAt"),
        "startedAt": entry.get("startedAt"),
        "completedAt": entry.get("completedAt"),
    }
    media = entry.get("media")
    if isinstance(media, dict):
        payload["media"] = _sanitize_media_payload(media)
    return {key: value for key, value in payload.items() if value is not None}


def _select_watched_at(entry: dict[str, Any]) -> datetime | None:
    completed = _parse_fuzzy_date(entry.get("completedAt"))
    if completed:
        return completed
    updated_at = _parse_timestamp(entry.get("updatedAt"))
    if updated_at:
        return updated_at
    started_at = _parse_fuzzy_date(entry.get("startedAt"))
    if started_at:
        return started_at
    return None


def _parse_fuzzy_date(value: Any) -> datetime | None:
    if not isinstance(value, dict):
        return None
    year = _coerce_int(value.get("year"))
    month = _coerce_int(value.get("month"))
    day = _coerce_int(value.get("day"))
    if not year or not month or not day:
        return None
    try:
        return datetime(year, month, day, tzinfo=timezone.utc)
    except ValueError:
        return None


def _parse_timestamp(value: Any) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        try:
            return datetime.fromtimestamp(float(value), tz=timezone.utc)
        except (ValueError, OSError):
            return None
    if isinstance(value, str):
        cleaned = value.strip()
        if not cleaned:
            return None
        if cleaned.isdigit():
            try:
                return datetime.fromtimestamp(float(cleaned), tz=timezone.utc)
            except (ValueError, OSError):
                return None
        try:
            if cleaned.endswith("Z"):
                cleaned = f"{cleaned[:-1]}+00:00"
            parsed = datetime.fromisoformat(cleaned)
            return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
        except ValueError:
            return None
    return None


def _extract_year(value: Any) -> int | None:
    if not isinstance(value, dict):
        return None
    year = value.get("year")
    if year is None:
        return None
    return _coerce_int(year)


def _poster_url(value: Any) -> str | None:
    if not isinstance(value, dict):
        return None
    return value.get("extraLarge") or value.get("large") or value.get("medium")


def _normalize_title(value: Any) -> str | None:
    if not isinstance(value, dict):
        return None
    return value.get("english") or value.get("romaji") or value.get("native")


def _select_episode_progress(entry: dict[str, Any]) -> int:
    progress = _coerce_int(entry.get("progress")) or 0
    total = 0
    media = entry.get("media")
    if isinstance(media, dict):
        total = _coerce_int(media.get("episodes")) or 0
    if progress > 0:
        if total and progress > total:
            return total
        return progress
    status = _coerce_str(entry.get("status"))
    if status != "COMPLETED":
        return 0
    return total


def _should_import_episodes(format_value: str | None, progress: int) -> bool:
    if not progress or progress <= 0:
        return False
    if not format_value:
        return True
    if format_value.upper() in {"MOVIE", "MUSIC"}:
        return False
    return True


def _build_episode_raw(entry_id: str | None) -> dict[str, Any]:
    raw: dict[str, Any] = {"source": "anilist", "type": "episode"}
    if entry_id:
        raw["entry_id"] = entry_id
    return raw


def _apply_episode_updates(
    item: EpisodeItem, episode_number: int, entry_id: str | None
) -> None:
    raw = item.raw if isinstance(item.raw, dict) else {}
    if entry_id and not raw.get("entry_id"):
        raw["entry_id"] = entry_id
    if raw.get("source") is None:
        raw["source"] = "anilist"
    if raw.get("type") is None:
        raw["type"] = "episode"
    item.raw = raw
    if not item.title:
        item.title = _default_episode_title(episode_number)


def _sort_updated_at(entry: dict[str, Any]) -> datetime:
    updated_at = _parse_timestamp(entry.get("updatedAt"))
    if updated_at:
        return updated_at
    return datetime.min.replace(tzinfo=timezone.utc)


def _default_episode_title(episode_number: int) -> str:
    return f"Episode {episode_number}"


def _coerce_str(value: Any) -> str | None:
    if isinstance(value, str):
        cleaned = value.strip()
        return cleaned or None
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return None


def _coerce_int(value: Any) -> int | None:
    if isinstance(value, int):
        return value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    if isinstance(value, str):
        cleaned = value.strip()
        if cleaned.isdigit():
            try:
                return int(cleaned)
            except ValueError:
                return None
    return None
