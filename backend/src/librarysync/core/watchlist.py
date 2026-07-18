import logging
from dataclasses import dataclass
from datetime import date, datetime, timezone
from typing import Any

from sqlalchemy import and_, case, func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from librarysync.connectors.metadata.base import EpisodeMetadataProvider
from librarysync.core.catalog_ordering import build_show_progress_subquery
from librarysync.core.metadata_providers import MetadataProviderService
from librarysync.core.rate_limiter import RATE_LIMITER
from librarysync.db.models import (
    EpisodeItem,
    MediaItem,
    WatchedItem,
    WatchEvent,
    WatchlistItem,
)

logger = logging.getLogger(__name__)

WATCHLIST_IMPORT_KEY = "watchlist_import"
LEGACY_WATCHLIST_STATUS_MAP = {
    "active": "added",
    "waiting": "watched",
}
SHOW_STATUS_VALUES = {"added", "in_progress", "watched", "not_released"}


def _is_future_date(value: date | None, now_date: date) -> bool:
    return value is not None and value > now_date


def normalize_watchlist_status(status: str | None) -> str | None:
    if not status:
        return status
    return LEGACY_WATCHLIST_STATUS_MAP.get(status, status)


def normalize_watchlist_statuses(statuses: list[str]) -> list[str]:
    normalized: list[str] = []
    for status in statuses:
        mapped = normalize_watchlist_status(status)
        if not mapped or mapped in normalized:
            continue
        normalized.append(mapped)
    return normalized


@dataclass(frozen=True)
class ShowStatusContext:
    progress_subq: Any
    earliest_air_subq: Any
    total_released: Any
    watched_count: Any
    earliest_air_date: Any
    status_expr: Any


def build_show_status_context(user_id: str, now_date: date) -> ShowStatusContext:
    progress_subq = build_show_progress_subquery(user_id, now_date)
    earliest_subq = (
        select(
            EpisodeItem.show_media_item_id.label("media_item_id"),
            func.min(EpisodeItem.air_date).label("earliest_air_date"),
        )
        .where(EpisodeItem.season_number > 0)
        .group_by(EpisodeItem.show_media_item_id)
        .subquery()
    )
    total_released = func.coalesce(progress_subq.c.total_released, 0)
    watched_count = func.coalesce(progress_subq.c.watched_count, 0)
    earliest_air_date = earliest_subq.c.earliest_air_date

    not_released_clause = or_(
        and_(MediaItem.first_air_date.is_(None), earliest_air_date.is_(None)),
        MediaItem.first_air_date > now_date,
        earliest_air_date > now_date,
    )
    status_expr = case(
        (total_released <= 0, case((not_released_clause, "not_released"), else_="added")),
        (watched_count <= 0, "added"),
        (watched_count < total_released, "in_progress"),
        else_="watched",
    )
    return ShowStatusContext(
        progress_subq=progress_subq,
        earliest_air_subq=earliest_subq,
        total_released=total_released,
        watched_count=watched_count,
        earliest_air_date=earliest_air_date,
        status_expr=status_expr,
    )


def apply_show_status_filter(
    query: Any,
    *,
    user_id: str,
    now_date: date,
    statuses: list[str],
    apply_filter: bool = True,
) -> tuple[Any, list[Any]]:
    """
    Apply computed status filtering for TV shows.

    Joins progress and air date subqueries, then builds status clauses based on:
    - Computed statuses (added, in_progress, watched, not_released)
    - Raw database statuses (all others)

    Returns the modified query and list of status clauses.
    """
    status_ctx = build_show_status_context(user_id, now_date)
    query = query.outerjoin(
        status_ctx.progress_subq,
        status_ctx.progress_subq.c.media_item_id == MediaItem.id,
    )
    query = query.outerjoin(
        status_ctx.earliest_air_subq,
        status_ctx.earliest_air_subq.c.media_item_id == MediaItem.id,
    )

    computed_statuses = [s for s in statuses if s in SHOW_STATUS_VALUES]
    raw_statuses = [s for s in statuses if s not in SHOW_STATUS_VALUES]

    status_clauses = []
    if computed_statuses:
        status_clauses.append(status_ctx.status_expr.in_(computed_statuses))
    if raw_statuses:
        status_clauses.append(WatchlistItem.status.in_(raw_statuses))

    if apply_filter and status_clauses:
        query = query.where(or_(*status_clauses))

    return query, status_clauses


def apply_combined_status_filter(
    query: Any,
    *,
    user_id: str,
    now_date: date,
    normalized_statuses: list[str],
    status_filter_values: list[str],
    media_type: str | None,
) -> Any:
    """
    Apply status filtering for watchlist queries that may include both movies and shows.

    Handles three scenarios:
    - Movies only: use raw status values
    - Shows only: use computed status with show context
    - Mixed: combine both filters with proper media_type conditions
    """
    if media_type == "movie":
        return query.where(WatchlistItem.status.in_(status_filter_values))

    show_clauses = []
    if media_type in {None, "tv", "anime"}:
        query, show_clauses = apply_show_status_filter(
            query,
            user_id=user_id,
            now_date=now_date,
            statuses=normalized_statuses,
            apply_filter=False,
        )

    if media_type in {"tv", "anime"}:
        if show_clauses:
            query = query.where(or_(*show_clauses))
    else:
        # Mixed media types: apply appropriate filter for each type
        movie_filter = WatchlistItem.status.in_(status_filter_values)
        if show_clauses:
            query = query.where(
                or_(
                    and_(MediaItem.media_type.in_(["tv", "anime"]), or_(*show_clauses)),
                    and_(MediaItem.media_type == "movie", movie_filter),
                )
            )
        else:
            query = query.where(movie_filter)

    return query


def determine_movie_watchlist_status(
    media_item: MediaItem,
    *,
    has_watched: bool,
    now_date: date,
) -> str:
    if has_watched:
        return "watched"
    if media_item.release_date is not None:
        if _is_future_date(media_item.release_date, now_date):
            return "not_released"
    elif media_item.year is not None and media_item.year <= now_date.year:
        return "added"
    elif media_item.release_date is None:
        return "not_released"
    return "added"


def determine_show_watchlist_status(
    *,
    total_released: int,
    watched_count: int,
    first_air_date: date | None,
    earliest_air_date: date | None,
    now_date: date,
) -> str:
    if total_released <= 0:
        if (
            first_air_date is None
            and earliest_air_date is None
            or _is_future_date(first_air_date, now_date)
            or _is_future_date(earliest_air_date, now_date)
        ):
            return "not_released"
        return "added"
    if watched_count <= 0:
        return "added"
    if watched_count < total_released:
        return "in_progress"
    return "watched"


@dataclass(frozen=True)
class WatchlistImportConfig:
    enabled: bool
    include_personal: bool
    list_urls: list[str]


def parse_watchlist_import_config(config: dict | None) -> WatchlistImportConfig:
    raw = config.get(WATCHLIST_IMPORT_KEY) if isinstance(config, dict) else None
    enabled = _coerce_bool(raw, "enabled", default=False)
    include_personal = _coerce_bool(raw, "include_personal", default=True)
    list_urls = _coerce_list(raw, ("lists", "list_urls"))
    return WatchlistImportConfig(
        enabled=enabled,
        include_personal=include_personal,
        list_urls=list_urls,
    )


def _parse_air_date(value: str | None) -> date | None:
    if not value:
        return None
    try:
        return datetime.strptime(value, "%Y-%m-%d").date()
    except ValueError:
        return None


async def _persist_episode_list_for_media_item(
    db: AsyncSession,
    media_item: MediaItem,
    provider: str,
    provider_item_id: str,
    season_number: int,
    episodes: list,
) -> bool:
    if not episodes:
        return False

    result = await db.execute(
        select(EpisodeItem).where(
            EpisodeItem.show_media_item_id == media_item.id,
            EpisodeItem.season_number == season_number,
        )
    )
    existing = result.scalars().all()
    by_number = {item.episode_number: item for item in existing}
    by_tmdb = {item.tmdb_id: item for item in existing if item.tmdb_id}

    dirty = False
    for episode in episodes:
        episode_number = episode.episode_number
        if episode_number is None:
            continue
        tmdb_id = episode.provider_id if provider == "tmdb" else None
        episode_item = None
        if tmdb_id:
            episode_item = by_tmdb.get(tmdb_id)
        if not episode_item:
            episode_item = by_number.get(episode_number)

        air_date = _parse_air_date(episode.air_date)
        raw = {
            "source": "metadata",
            "provider": provider,
            "provider_item_id": provider_item_id,
        }
        if episode.still_url:
            raw["still_url"] = episode.still_url

        if not episode_item:
            episode_item = EpisodeItem(
                show_media_item_id=media_item.id,
                season_number=season_number,
                episode_number=episode_number,
                title=episode.title,
                air_date=air_date,
                tmdb_id=tmdb_id,
                raw=raw,
            )
            db.add(episode_item)
            dirty = True
            continue

        if episode.title and not episode_item.title:
            episode_item.title = episode.title
            dirty = True
        if air_date and episode_item.air_date != air_date:
            episode_item.air_date = air_date
            dirty = True
        if tmdb_id and not episode_item.tmdb_id:
            episode_item.tmdb_id = tmdb_id
            dirty = True
        if episode.still_url:
            existing_raw = episode_item.raw if isinstance(episode_item.raw, dict) else {}
            if "still_url" not in existing_raw:
                existing_raw["still_url"] = episode.still_url
                episode_item.raw = existing_raw
                dirty = True

    return dirty


async def backfill_show_episodes(
    db: AsyncSession,
    user_id: str | None,
    media_item: MediaItem,
    provider_override: EpisodeMetadataProvider | None = None,
) -> bool:
    if media_item.media_type not in {"tv", "anime"}:
        return False

    provider = provider_override
    if provider is not None and not isinstance(provider, EpisodeMetadataProvider):
        return False
    if provider is None:
        if not user_id:
            return False
        service = MetadataProviderService(db, user_id)
        provider = await service.load_provider("tmdb")
        if not provider or not isinstance(provider, EpisodeMetadataProvider):
            return False
    provider_name = provider.provider

    media_dirty = False
    if not media_item.tmdb_id:
        if media_item.imdb_id and provider.capabilities.supports_external_id:
            if not await _acquire_rate_limit(db, user_id, provider_name):
                return False
            try:
                candidates = await provider.find_by_external_id(media_item.imdb_id.lower(), "tv")
            except Exception as exc:
                logger.warning("TMDB external ID lookup failed for %s: %s", media_item.id, exc)
            else:
                candidate = next(
                    (item for item in candidates if item.provider_id and item.media_type == "tv"),
                    None,
                )
                if candidate and candidate.provider_id:
                    media_item.tmdb_id = candidate.provider_id
                    if not media_item.title and candidate.title:
                        media_item.title = candidate.title
                    if media_item.year is None and candidate.year is not None:
                        media_item.year = candidate.year
                    if not media_item.poster_url and candidate.poster_url:
                        media_item.poster_url = candidate.poster_url
                    media_dirty = True
        if not media_item.tmdb_id:
            return False

    if not await _acquire_rate_limit(db, user_id, provider_name):
        return False
    try:
        seasons = await provider.list_seasons(media_item.tmdb_id)
    except Exception as exc:
        logger.warning("TMDB season lookup failed for %s: %s", media_item.id, exc)
        return False
    refreshed = True
    rate_limited = False

    episodes_dirty = False
    for season in seasons:
        if not await _acquire_rate_limit(db, user_id, provider_name):
            rate_limited = True
            break
        try:
            episodes = await provider.list_episodes(media_item.tmdb_id, season.season_number)
        except Exception as exc:
            logger.warning(
                "TMDB episode lookup failed for %s season %s: %s",
                media_item.id,
                season.season_number,
                exc,
            )
            continue
        if await _persist_episode_list_for_media_item(
            db,
            media_item,
            "tmdb",
            media_item.tmdb_id,
            season.season_number,
            episodes,
        ):
            episodes_dirty = True

    if refreshed and not rate_limited:
        media_item.updated_at = datetime.now(timezone.utc)
        await db.flush()
    return (refreshed and not rate_limited) or media_dirty or episodes_dirty


async def _acquire_rate_limit(
    db: AsyncSession,
    user_id: str | None,
    provider: str,
) -> bool:
    if not user_id:
        return True
    now = datetime.now(timezone.utc)
    rate_decision = await RATE_LIMITER.try_acquire(db, user_id, provider, now=now)
    if rate_decision and not rate_decision.allowed:
        retry_at = rate_decision.retry_at.isoformat() if rate_decision.retry_at else "unknown"
        logger.info(
            "%s metadata backfill rate-limited for user %s until %s",
            provider,
            user_id,
            retry_at,
        )
        return False
    return True


WATCHLIST_ITEM_UNIQUE_CONSTRAINT = "uq_watchlist_items_user_media"


def _is_watchlist_item_unique_violation(exc: IntegrityError) -> bool:
    diag = getattr(exc.orig, "diag", None)
    constraint_name = getattr(diag, "constraint_name", None)
    if constraint_name:
        return constraint_name == WATCHLIST_ITEM_UNIQUE_CONSTRAINT
    return WATCHLIST_ITEM_UNIQUE_CONSTRAINT in str(exc)


async def _get_watchlist_item(
    db: AsyncSession,
    user_id: str,
    media_item_id: str,
) -> WatchlistItem | None:
    result = await db.execute(
        select(WatchlistItem).where(
            WatchlistItem.user_id == user_id,
            WatchlistItem.media_item_id == media_item_id,
        )
    )
    return result.scalars().first()


async def _enqueue_watchlist_sync(
    db: AsyncSession,
    watchlist_item: WatchlistItem,
    media_item: MediaItem | None,
) -> None:
    # Deferred import: watchlist_sync depends on watch_pipeline, which imports this module.
    from librarysync.core.watchlist_sync import enqueue_personal_watchlist_sync

    await enqueue_personal_watchlist_sync(db, watchlist_item, media_item)


async def check_and_update_watchlist(
    db: AsyncSession,
    user_id: str,
    media_item_id: str,
    *,
    watched_at: datetime | None = None,
) -> None:
    result = await db.execute(
        select(WatchlistItem).where(
            WatchlistItem.user_id == user_id,
            WatchlistItem.media_item_id == media_item_id,
        )
    )
    item = result.scalars().first()
    if not item:
        return

    result = await db.execute(select(MediaItem).where(MediaItem.id == media_item_id))
    media_item = result.scalars().first()
    if not media_item:
        return

    await clear_watchlist_rewatch_request(
        db,
        item,
        user_id,
        media_item_id,
        reason="watched",
        watched_at=watched_at,
    )

    if media_item.media_type == "movie":
        await apply_watchlist_status_change(
            db,
            item,
            user_id,
            media_item_id,
            "watched",
            reason="watched",
        )

    elif media_item.media_type in {"tv", "anime"}:
        await evaluate_show_watchlist_status(db, user_id, item, media_item)


async def ensure_show_watchlist_item(
    db: AsyncSession,
    user_id: str,
    media_item: MediaItem | None,
    *,
    watched_at: datetime,
) -> tuple[WatchlistItem | None, bool]:
    """
    Ensure a watchlist item exists for a show.

    Returns the item (or None) and a flag telling the caller whether the item
    was newly created (or restored) and its show status was already evaluated
    by this call, so the caller can skip a redundant evaluation.
    """
    if not media_item or media_item.media_type not in {"tv", "anime"}:
        return None, False

    existing_item = await _get_watchlist_item(db, user_id, media_item.id)
    if existing_item:
        return existing_item, False

    media_ids = normalize_media_ids(
        {
            "imdb_id": media_item.imdb_id,
            "tmdb_id": media_item.tmdb_id,
            "tvdb_id": media_item.tvdb_id,
            "tvmaze_id": media_item.tvmaze_id,
            "kitsu_id": media_item.kitsu_id,
            "myanimelist_id": media_item.myanimelist_id,
            "anilist_id": media_item.anilist_id,
        }
    )
    if media_ids:
        watchlist_item, upsert_status = await upsert_watchlist_item(
            db,
            user_id,
            media_item.media_type,
            media_ids,
            media_item.title,
            media_item.year,
            media_item.poster_url,
            "auto_from_history",
            now=watched_at,
            event_raw={},
            enqueue_sync=True,
        )
        evaluated = upsert_status in {"created", "restored"}
        return watchlist_item, evaluated

    initial_status = "added"
    if _is_future_date(media_item.first_air_date, watched_at.date()):
        initial_status = "not_released"

    watchlist_item = WatchlistItem(
        user_id=user_id,
        media_item_id=media_item.id,
        type=media_item.media_type,
        status=initial_status,
        source="auto_from_history",
    )
    try:
        async with db.begin_nested():
            db.add(watchlist_item)
            await log_watchlist_event(
                db,
                user_id,
                media_item.id,
                "watchlist_added",
                {"source": "auto_from_history"},
            )
            await db.flush()
    except IntegrityError as exc:
        if not _is_watchlist_item_unique_violation(exc):
            raise
        # Lost an insert race with a concurrent job: reuse the row it inserted.
        existing_item = await _get_watchlist_item(db, user_id, media_item.id)
        if existing_item is None:
            raise
        logger.info(
            "Watchlist item insert race for user %s media %s; using existing row",
            user_id,
            media_item.id,
        )
        return existing_item, False

    await evaluate_show_watchlist_status(db, user_id, watchlist_item, media_item)
    await _enqueue_watchlist_sync(db, watchlist_item, media_item)
    return watchlist_item, True


async def refresh_watchlist_from_history(
    db: AsyncSession,
    user_id: str,
    media_item_id: str,
) -> None:
    result = await db.execute(
        select(WatchlistItem).where(
            WatchlistItem.user_id == user_id,
            WatchlistItem.media_item_id == media_item_id,
        )
    )
    item = result.scalars().first()
    if not item:
        return

    media_item = await db.get(MediaItem, media_item_id)
    if not media_item:
        return

    now_date = datetime.now(timezone.utc).date()
    if media_item.media_type == "movie":
        watch_result = await db.execute(
            select(WatchedItem.id)
            .where(
                WatchedItem.user_id == user_id,
                WatchedItem.media_item_id == media_item_id,
            )
            .limit(1)
        )
        has_watched = bool(watch_result.scalars().first())
        new_status = determine_movie_watchlist_status(
            media_item,
            has_watched=has_watched,
            now_date=now_date,
        )
        await apply_watchlist_status_change(
            db,
            item,
            user_id,
            media_item_id,
            new_status,
            reason="history_update",
        )
    elif media_item.media_type in {"tv", "anime"}:
        await evaluate_show_watchlist_status(db, user_id, item, media_item)


async def evaluate_show_watchlist_status(
    db: AsyncSession,
    user_id: str,
    watchlist_item: WatchlistItem,
    media_item: MediaItem,
) -> None:
    # Logic:
    # 1. Get all released episodes for this show
    # 2. Get all watched episodes for this show by user
    # 3. If there is at least one released episode that is NOT watched -> Active
    # 4. If all released episodes are watched -> Waiting (if returning) or Watched (if ended)

    if watchlist_item.status == "removed":
        return

    # Find released episodes
    now_date = datetime.now(timezone.utc).date()

    result = await db.execute(
        select(EpisodeItem).where(
            EpisodeItem.show_media_item_id == media_item.id,
            EpisodeItem.air_date is not None,
            EpisodeItem.air_date <= now_date,
            EpisodeItem.season_number > 0,  # Exclude specials (season 0)
        )
    )
    released_episodes = result.scalars().all()
    total_released = len(released_episodes)

    earliest_air_date = None
    if total_released == 0:
        earliest_result = await db.execute(
            select(func.min(EpisodeItem.air_date)).where(
                EpisodeItem.show_media_item_id == media_item.id,
                EpisodeItem.season_number > 0,  # Exclude specials (season 0)
            )
        )
        earliest_air_date = earliest_result.scalar()

    watched_count = 0
    if total_released > 0:
        result = await db.execute(
            select(func.count(func.distinct(WatchedItem.episode_item_id))).where(
                WatchedItem.user_id == user_id,
                WatchedItem.media_item_id is None,
                WatchedItem.episode_item_id.in_([e.id for e in released_episodes]),
            )
        )
        watched_count = result.scalar() or 0

    new_status = determine_show_watchlist_status(
        total_released=total_released,
        watched_count=watched_count,
        first_air_date=media_item.first_air_date,
        earliest_air_date=earliest_air_date,
        now_date=now_date,
    )

    await apply_watchlist_status_change(
        db,
        watchlist_item,
        user_id,
        media_item.id,
        new_status,
        reason="auto_evaluation",
    )


async def apply_watchlist_status_change(
    db: AsyncSession,
    watchlist_item: WatchlistItem,
    user_id: str,
    media_item_id: str,
    new_status: str,
    *,
    reason: str,
    now: datetime | None = None,
) -> bool:
    if watchlist_item.status == "removed":
        return False
    if watchlist_item.status == new_status:
        return False

    current_status = watchlist_item.status
    normalized_current = normalize_watchlist_status(current_status)
    normalized_new = normalize_watchlist_status(new_status)
    if normalized_current == normalized_new:
        watchlist_item.status = new_status
        watchlist_item.updated_at = now or datetime.now(timezone.utc)
        return True

    watchlist_item.status = new_status
    watchlist_item.updated_at = now or datetime.now(timezone.utc)
    await log_watchlist_event(
        db,
        user_id,
        media_item_id,
        "watchlist_status_changed",
        {"status": new_status, "previous_status": current_status, "reason": reason},
    )
    return True


async def set_watchlist_rewatch_request(
    db: AsyncSession,
    watchlist_item: WatchlistItem,
    user_id: str,
    media_item_id: str,
    *,
    enabled: bool,
    reason: str,
    now: datetime | None = None,
) -> bool:
    if watchlist_item.status == "removed":
        return False
    if watchlist_item.rewatch_requested == enabled:
        return False

    effective_now = now or datetime.now(timezone.utc)
    watchlist_item.rewatch_requested = enabled
    watchlist_item.rewatch_requested_at = effective_now if enabled else None
    watchlist_item.updated_at = effective_now
    await log_watchlist_event(
        db,
        user_id,
        media_item_id,
        "watchlist_rewatch_updated",
        {"enabled": enabled, "reason": reason},
    )
    return True


async def clear_watchlist_rewatch_request(
    db: AsyncSession,
    watchlist_item: WatchlistItem,
    user_id: str,
    media_item_id: str,
    *,
    reason: str,
    now: datetime | None = None,
    watched_at: datetime | None = None,
) -> bool:
    if not watchlist_item.rewatch_requested:
        return False

    requested_at = watchlist_item.rewatch_requested_at
    if watched_at is not None and requested_at is not None and watched_at < requested_at:
        return False

    return await set_watchlist_rewatch_request(
        db,
        watchlist_item,
        user_id,
        media_item_id,
        enabled=False,
        reason=reason,
        now=now,
    )


async def log_watchlist_event(
    db: AsyncSession, user_id: str, media_item_id: str, event_type: str, raw: dict
) -> None:
    event = WatchEvent(
        user_id=user_id,
        media_item_id=media_item_id,
        event_type=event_type,
        occurred_at=datetime.now(timezone.utc),
        raw=raw,
    )
    db.add(event)


def normalize_media_ids(ids: dict[str, str] | None) -> dict[str, str]:
    if not isinstance(ids, dict):
        return {}
    normalized: dict[str, str] = {}
    for key, value in ids.items():
        if not value:
            continue
        cleaned = str(value).strip()
        if not cleaned:
            continue
        if key == "imdb_id":
            cleaned = cleaned.lower()
        normalized[key] = cleaned
    return normalized


def fallback_title(ids: dict[str, str]) -> str:
    for provider, value in ids.items():
        return f"{provider.upper()} {value}"
    return "Unknown title"


async def find_media_item_by_ids(
    db: AsyncSession, media_type: str, ids: dict[str, str]
) -> MediaItem | None:
    clauses = []
    if ids.get("imdb_id"):
        clauses.append(MediaItem.imdb_id == ids["imdb_id"])
    if ids.get("tmdb_id"):
        clauses.append((MediaItem.tmdb_id == ids["tmdb_id"]) & (MediaItem.media_type == media_type))
    if ids.get("tvdb_id"):
        clauses.append((MediaItem.tvdb_id == ids["tvdb_id"]) & (MediaItem.media_type == media_type))
    if ids.get("tvmaze_id"):
        clauses.append(
            (MediaItem.tvmaze_id == ids["tvmaze_id"]) & (MediaItem.media_type == media_type)
        )
    if ids.get("kitsu_id"):
        clauses.append(
            (MediaItem.kitsu_id == ids["kitsu_id"]) & (MediaItem.media_type == media_type)
        )
    if ids.get("myanimelist_id"):
        clauses.append(
            (MediaItem.myanimelist_id == ids["myanimelist_id"])
            & (MediaItem.media_type == media_type)
        )
    if ids.get("anilist_id"):
        clauses.append(
            (MediaItem.anilist_id == ids["anilist_id"]) & (MediaItem.media_type == media_type)
        )
    if ids.get("letterboxd_film_id"):
        clauses.append(
            (MediaItem.raw["letterboxd_film_id"].as_string() == ids["letterboxd_film_id"])
            & (MediaItem.media_type == media_type)
        )
    if not clauses:
        return None
    result = await db.execute(select(MediaItem).where(or_(*clauses)).limit(1))
    return result.scalars().first()


def apply_media_id_update(item: MediaItem, field: str, value: str | None) -> None:
    if not value:
        return
    current = getattr(item, field)
    if not current:
        setattr(item, field, value)


async def _resolve_existing_watchlist_item(
    db: AsyncSession,
    existing: WatchlistItem,
    *,
    user_id: str,
    media_item: MediaItem,
    media_type: str,
    source: str,
    now: datetime,
    event_raw: dict[str, Any] | None,
    enqueue_sync: bool = False,
) -> tuple[WatchlistItem, str]:
    if existing.status == "removed":
        initial_status = "added"
        now_date = now.date()
        if media_type == "movie":
            w_result = await db.execute(
                select(WatchedItem)
                .where(
                    WatchedItem.user_id == user_id,
                    WatchedItem.media_item_id == media_item.id,
                )
                .limit(1)
            )
            has_watched = bool(w_result.scalars().first())
            initial_status = determine_movie_watchlist_status(
                media_item,
                has_watched=has_watched,
                now_date=now_date,
            )
        elif media_type in {"tv", "anime"}:
            if _is_future_date(media_item.first_air_date, now_date):
                initial_status = "not_released"
        existing.status = initial_status
        existing.updated_at = now
        if existing.source != source and existing.source != "manual":
            existing.source = source
        await log_watchlist_event(
            db,
            user_id,
            media_item.id,
            "watchlist_added",
            {"restored": True, "source": source, **(event_raw or {})},
        )
        if media_type in {"tv", "anime"}:
            await evaluate_show_watchlist_status(db, user_id, existing, media_item)
        if enqueue_sync:
            await _enqueue_watchlist_sync(db, existing, media_item)
        return existing, "restored"
    return existing, "already_exists"


async def upsert_watchlist_item(
    db: AsyncSession,
    user_id: str,
    media_type: str,
    ids: dict[str, str],
    title: str | None,
    year: int | None,
    poster_url: str | None,
    source: str,
    *,
    now: datetime | None = None,
    event_raw: dict[str, Any] | None = None,
    enqueue_sync: bool = False,
) -> tuple[WatchlistItem | None, str]:
    """
    Create, restore, or return a watchlist item.

    When enqueue_sync is True, a newly created or restored item is also pushed
    to connected providers via the watchlist outbox. Callers that enqueue the
    push themselves (manual API add) or that must not echo items back out
    (watchlist imports) leave this False.
    """
    normalized_ids = normalize_media_ids(ids)
    if not normalized_ids:
        return None, "skipped"
    if now is None:
        now = datetime.now(timezone.utc)

    media_item = await find_media_item_by_ids(db, media_type, normalized_ids)
    if media_item and media_item.media_type != media_type:
        return None, "conflict"

    if not media_item:
        resolved_title = title or fallback_title(normalized_ids)
        raw = {"source": source, "ids": normalized_ids}
        if normalized_ids.get("letterboxd_film_id"):
            raw["letterboxd_film_id"] = normalized_ids["letterboxd_film_id"]
        media_item = MediaItem(
            media_type=media_type,
            title=resolved_title,
            year=year,
            poster_url=poster_url,
            imdb_id=normalized_ids.get("imdb_id"),
            tmdb_id=normalized_ids.get("tmdb_id"),
            tvdb_id=normalized_ids.get("tvdb_id"),
            tvmaze_id=normalized_ids.get("tvmaze_id"),
            kitsu_id=normalized_ids.get("kitsu_id"),
            myanimelist_id=normalized_ids.get("myanimelist_id"),
            anilist_id=normalized_ids.get("anilist_id"),
            raw=raw,
        )
        db.add(media_item)
        await db.flush()
    else:
        apply_media_id_update(media_item, "imdb_id", normalized_ids.get("imdb_id"))
        apply_media_id_update(media_item, "tmdb_id", normalized_ids.get("tmdb_id"))
        apply_media_id_update(media_item, "tvdb_id", normalized_ids.get("tvdb_id"))
        apply_media_id_update(media_item, "tvmaze_id", normalized_ids.get("tvmaze_id"))
        apply_media_id_update(media_item, "kitsu_id", normalized_ids.get("kitsu_id"))
        apply_media_id_update(media_item, "myanimelist_id", normalized_ids.get("myanimelist_id"))
        apply_media_id_update(media_item, "anilist_id", normalized_ids.get("anilist_id"))
        if year is not None and media_item.year is None:
            media_item.year = year
        if poster_url and not media_item.poster_url:
            media_item.poster_url = poster_url
        if normalized_ids.get("letterboxd_film_id"):
            existing_raw = media_item.raw if isinstance(media_item.raw, dict) else {}
            if not existing_raw.get("letterboxd_film_id"):
                existing_raw["letterboxd_film_id"] = normalized_ids["letterboxd_film_id"]
                media_item.raw = existing_raw

    existing = await _get_watchlist_item(db, user_id, media_item.id)
    if existing:
        return await _resolve_existing_watchlist_item(
            db,
            existing,
            user_id=user_id,
            media_item=media_item,
            media_type=media_type,
            source=source,
            now=now,
            event_raw=event_raw,
            enqueue_sync=enqueue_sync,
        )

    initial_status = "added"
    if media_type == "movie":
        w_result = await db.execute(
            select(WatchedItem)
            .where(
                WatchedItem.user_id == user_id,
                WatchedItem.media_item_id == media_item.id,
            )
            .limit(1)
        )
        has_watched = bool(w_result.scalars().first())
        initial_status = determine_movie_watchlist_status(
            media_item,
            has_watched=has_watched,
            now_date=now.date(),
        )
    elif media_type in {"tv", "anime"}:
        if _is_future_date(media_item.first_air_date, now.date()):
            initial_status = "not_released"

    watchlist_item = WatchlistItem(
        user_id=user_id,
        media_item_id=media_item.id,
        type=media_type,
        status=initial_status,
        source=source,
    )
    try:
        async with db.begin_nested():
            db.add(watchlist_item)
            await log_watchlist_event(
                db,
                user_id,
                media_item.id,
                "watchlist_added",
                {"source": source, **(event_raw or {})},
            )
            await db.flush()
    except IntegrityError as exc:
        if not _is_watchlist_item_unique_violation(exc):
            raise
        # Lost an insert race with a concurrent writer: reuse the row it inserted.
        existing = await _get_watchlist_item(db, user_id, media_item.id)
        if existing is None:
            raise
        logger.info(
            "Watchlist item insert race for user %s media %s; using existing row",
            user_id,
            media_item.id,
        )
        # The concurrent writer owns the sync push for the row it inserted.
        return await _resolve_existing_watchlist_item(
            db,
            existing,
            user_id=user_id,
            media_item=media_item,
            media_type=media_type,
            source=source,
            now=now,
            event_raw=event_raw,
            enqueue_sync=False,
        )
    if media_type in {"tv", "anime"}:
        await evaluate_show_watchlist_status(db, user_id, watchlist_item, media_item)
    if enqueue_sync:
        await _enqueue_watchlist_sync(db, watchlist_item, media_item)
    return watchlist_item, "created"


def _coerce_bool(raw: dict | None, key: str, default: bool) -> bool:
    if not isinstance(raw, dict):
        return default
    value = raw.get(key)
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        cleaned = value.strip().lower()
        if cleaned in {"true", "1", "yes", "on"}:
            return True
        if cleaned in {"false", "0", "no", "off"}:
            return False
    return default


def _coerce_list(raw: dict | None, keys: tuple[str, ...]) -> list[str]:
    if not isinstance(raw, dict):
        return []
    value: object = None
    for key in keys:
        if key in raw:
            value = raw.get(key)
            break
    items: list[str] = []
    if isinstance(value, list):
        for entry in value:
            if isinstance(entry, str):
                cleaned = entry.strip()
                if cleaned:
                    items.append(cleaned)
    elif isinstance(value, str):
        for entry in value.splitlines():
            cleaned = entry.strip()
            if cleaned:
                items.append(cleaned)
    return items
