from __future__ import annotations

from datetime import date, datetime, timezone
from importlib import metadata
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from librarysync.api.deps import get_db
from librarysync.core.catalog_ordering import apply_catalog_ordering
from librarysync.core.stremio_addon import (
    DEFAULT_PAGE_SIZE,
    DEFAULT_SHOW_IN_HOME,
    get_addon_config_by_id,
    normalize_default_catalogs,
)
from librarysync.db.models import (
    EpisodeItem,
    MediaItem,
    StremioCustomCatalog,
    StremioCustomCatalogItem,
    WatchedItem,
    WatchlistItem,
)

router = APIRouter(prefix="/stremio-addon", tags=["stremio-addon"])

STREMIO_EXTRA = ["search", "skip", "limit"]
MAX_LIMIT = 100


def _get_app_version() -> str:
    try:
        return metadata.version("librarysync")
    except metadata.PackageNotFoundError:
        return "unknown"


def _resolve_meta_id(media_item: MediaItem) -> str | None:
    raw = media_item.raw if isinstance(media_item.raw, dict) else {}
    stremio_id = raw.get("stremio_id")
    if not stremio_id:
        stremio_payload = raw.get("stremio")
        if isinstance(stremio_payload, dict):
            stremio_id = stremio_payload.get("id") or stremio_payload.get("_id")
    if stremio_id:
        return str(stremio_id)
    if media_item.imdb_id:
        return media_item.imdb_id
    return None


def _resolve_stremio_type(media_type: str) -> Literal["movie", "series"] | None:
    if media_type == "movie":
        return "movie"
    if media_type in {"tv", "anime", "series"}:
        return "series"
    return None


def _build_meta(media_item: MediaItem, catalog_type: str) -> dict[str, Any] | None:
    stremio_id = _resolve_meta_id(media_item)
    stremio_type = _resolve_stremio_type(media_item.media_type)
    if not stremio_id or not stremio_type or stremio_type != catalog_type:
        return None
    meta = {
        "id": stremio_id,
        "type": stremio_type,
        "name": media_item.title,
    }
    if media_item.poster_url:
        meta["poster"] = media_item.poster_url
    if media_item.year:
        meta["year"] = media_item.year
    return meta


def _extract_extra_param(request: Request, name: str) -> str | None:
    return request.query_params.get(name) or request.query_params.get(f"extra[{name}]")


def _parse_int_param(value: str | None, default: int) -> int:
    if not value:
        return default
    try:
        parsed = int(value)
    except ValueError:
        return default
    return parsed


def _apply_search_filter(query, search: str):
    normalized_search = search.strip()
    if not normalized_search:
        return query
    like_value = f"%{normalized_search}%"
    search_clauses = [
        MediaItem.title.ilike(like_value),
        MediaItem.imdb_id.ilike(like_value),
        MediaItem.tmdb_id.ilike(like_value),
        MediaItem.tvdb_id.ilike(like_value),
        MediaItem.tvmaze_id.ilike(like_value),
        MediaItem.kitsu_id.ilike(like_value),
        MediaItem.myanimelist_id.ilike(like_value),
        MediaItem.anilist_id.ilike(like_value),
    ]
    if normalized_search.isdigit() and len(normalized_search) == 4:
        search_clauses.append(MediaItem.year == int(normalized_search))
    return query.where(or_(*search_clauses))


def _resolve_pagination(
    request: Request,
    extra_overrides: dict[str, str] | None = None,
) -> tuple[int, int, str | None]:
    search = _extract_extra_param(request, "search")
    if not search and extra_overrides:
        search = extra_overrides.get("search")
    skip_value = _extract_extra_param(request, "skip")
    if not skip_value and extra_overrides:
        skip_value = extra_overrides.get("skip")
    limit_value = _extract_extra_param(request, "limit")
    if not limit_value and extra_overrides:
        limit_value = extra_overrides.get("limit")
    skip = _parse_int_param(skip_value, 0)
    limit = _parse_int_param(limit_value, 50)
    if skip < 0:
        skip = 0
    if limit <= 0:
        limit = 50
    if limit > MAX_LIMIT:
        limit = MAX_LIMIT
    return skip, limit, search


def _parse_extra_path(extra_path: str | None) -> dict[str, str]:
    if not extra_path:
        return {}
    extras: dict[str, str] = {}
    for segment in extra_path.split("&"):
        if "=" not in segment:
            continue
        key, value = segment.split("=", 1)
        normalized = key.strip()
        if normalized.startswith("extra[") and normalized.endswith("]"):
            normalized = normalized[6:-1]
        if not normalized:
            continue
        extras[normalized] = value
    return extras


def _normalize_catalogs(config_catalogs: list[dict] | None) -> list[dict]:
    return normalize_default_catalogs(config_catalogs)


def _catalogs_by_id(catalogs: list[dict]) -> dict[str, dict]:
    return {catalog.get("id"): catalog for catalog in catalogs if catalog.get("id")}


def _resolve_status_filter(catalog: dict | None, base_statuses: list[str]) -> list[str]:
    filters = catalog.get("filters") if isinstance(catalog, dict) else {}
    statuses = filters.get("statuses") if isinstance(filters, dict) else None
    extras: list[str] = []
    if isinstance(statuses, list):
        for status_value in statuses:
            if not status_value:
                continue
            status = str(status_value)
            if status == "added":
                continue
            if status in base_statuses:
                continue
            extras.append(status)
    return list(dict.fromkeys([*base_statuses, *extras]))


def _coerce_page_size(catalog: dict | None) -> int:
    if not isinstance(catalog, dict):
        return DEFAULT_PAGE_SIZE
    value = catalog.get("pageSize")
    if isinstance(value, int) and value > 0:
        return value
    return DEFAULT_PAGE_SIZE


def _coerce_show_in_home(catalog: dict | None) -> bool:
    if not isinstance(catalog, dict):
        return DEFAULT_SHOW_IN_HOME
    value = catalog.get("showInHome")
    if isinstance(value, bool):
        return value
    return DEFAULT_SHOW_IN_HOME


def _build_manifest(
    catalogs: list[dict],
    custom_catalogs: list[StremioCustomCatalog],
) -> dict[str, Any]:
    manifest_catalogs: list[dict[str, Any]] = []
    seen_types: set[str] = set()

    for catalog in catalogs:
        if not catalog.get("enabled", True):
            continue
        catalog_id = catalog.get("id")
        if not catalog_id:
            continue
        media_type = catalog.get("media_type")
        stremio_type = _resolve_stremio_type(str(media_type))
        if not stremio_type:
            continue
        page_size = _coerce_page_size(catalog)
        show_in_home = _coerce_show_in_home(catalog)
        manifest_catalogs.append(
            {
                "type": stremio_type,
                "id": catalog_id,
                "name": catalog.get("name") or catalog_id,
                "extraSupported": STREMIO_EXTRA,
                "pageSize": page_size,
                "showInHome": show_in_home,
            }
        )
        seen_types.add(stremio_type)

    for custom in custom_catalogs:
        stremio_type = _resolve_stremio_type(custom.media_type)
        if not stremio_type:
            continue
        manifest_catalogs.append(
            {
                "type": stremio_type,
                "id": custom.slug,
                "name": custom.name,
                "extraSupported": STREMIO_EXTRA,
                "pageSize": DEFAULT_PAGE_SIZE,
                "showInHome": DEFAULT_SHOW_IN_HOME,
            }
        )
        seen_types.add(stremio_type)

    return {
        "id": "org.librarysync.catalogs",
        "version": _get_app_version(),
        "name": "librarySync Watchlists",
        "description": "Personal watchlist catalogs from librarySync.",
        "resources": ["catalog"],
        "types": sorted(seen_types) if seen_types else ["movie", "series"],
        "catalogs": manifest_catalogs,
    }


async def _build_watchlist_query(
    user_id: str,
    catalog: dict,
    search: str | None,
):
    query = (
        select(MediaItem)
        .join(WatchlistItem, WatchlistItem.media_item_id == MediaItem.id)
        .where(
            WatchlistItem.user_id == user_id,
            WatchlistItem.status != "removed",
        )
    )
    media_type = catalog.get("media_type")
    if media_type:
        normalized = str(media_type)
        if normalized == "series":
            query = query.where(WatchlistItem.type.in_(["tv", "anime"]))
        else:
            query = query.where(WatchlistItem.type == normalized)
    statuses = _resolve_status_filter(catalog, ["added"])
    if statuses:
        query = query.where(WatchlistItem.status.in_(statuses))
    if search:
        query = _apply_search_filter(query, search)
    return query


def _build_progress_subquery(user_id: str, now_date: date):
    base = (
        select(EpisodeItem.show_media_item_id.label("media_item_id"))
        .where(EpisodeItem.show_media_item_id.is_not(None))
        .group_by(EpisodeItem.show_media_item_id)
        .subquery()
    )
    released_subq = (
        select(
            EpisodeItem.show_media_item_id.label("media_item_id"),
            func.count(EpisodeItem.id).label("total_released"),
        )
        .where(
            EpisodeItem.air_date.is_not(None),
            EpisodeItem.air_date <= now_date,
            EpisodeItem.season_number > 0,
        )
        .group_by(EpisodeItem.show_media_item_id)
        .subquery()
    )
    watched_subq = (
        select(
            EpisodeItem.show_media_item_id.label("media_item_id"),
            func.count(func.distinct(WatchedItem.episode_item_id)).label("watched_count"),
        )
        .join(WatchedItem, WatchedItem.episode_item_id == EpisodeItem.id)
        .where(
            WatchedItem.user_id == user_id,
            WatchedItem.media_item_id.is_(None),
            EpisodeItem.air_date.is_not(None),
            EpisodeItem.air_date <= now_date,
            EpisodeItem.season_number > 0,
        )
        .group_by(EpisodeItem.show_media_item_id)
        .subquery()
    )
    return (
        select(
            base.c.media_item_id,
            func.coalesce(released_subq.c.total_released, 0).label("total_released"),
            func.coalesce(watched_subq.c.watched_count, 0).label("watched_count"),
        )
        .select_from(base)
        .outerjoin(released_subq, released_subq.c.media_item_id == base.c.media_item_id)
        .outerjoin(watched_subq, watched_subq.c.media_item_id == base.c.media_item_id)
        .subquery()
    )


async def _build_in_progress_query(
    user_id: str,
    catalog: dict | None,
    search: str | None,
):
    now_date = datetime.now(timezone.utc).date()
    progress_subq = _build_progress_subquery(user_id, now_date)
    query = (
        select(MediaItem)
        .join(WatchlistItem, WatchlistItem.media_item_id == MediaItem.id)
        .outerjoin(progress_subq, progress_subq.c.media_item_id == MediaItem.id)
        .where(
            WatchlistItem.user_id == user_id,
            WatchlistItem.status != "removed",
            WatchlistItem.type.in_(["tv", "anime"]),
            MediaItem.media_type.in_(["tv", "anime"]),
            progress_subq.c.total_released > 0,
            progress_subq.c.watched_count > 0,
            progress_subq.c.watched_count < progress_subq.c.total_released,
        )
    )
    statuses = _resolve_status_filter(catalog, ["in_progress"])
    if statuses:
        query = query.where(WatchlistItem.status.in_(statuses))
    if search:
        query = _apply_search_filter(query, search)
    return query


async def _build_custom_catalog_query(
    catalog: StremioCustomCatalog,
    search: str | None,
):
    query = (
        select(MediaItem)
        .join(
            StremioCustomCatalogItem,
            StremioCustomCatalogItem.media_item_id == MediaItem.id,
        )
        .where(StremioCustomCatalogItem.catalog_id == catalog.id)
    )
    if search:
        query = _apply_search_filter(query, search)
    return query


def _apply_ordering(
    query,
    *,
    order_by: str,
    order_dir: str,
    user_id: str,
    date_added_col,
    release_date_col,
    base_media_id_col,
    tie_breaker_col=None,
):
    if order_by == "random":
        return query.order_by(func.random())
    return apply_catalog_ordering(
        query,
        order_by=order_by,
        order_dir=order_dir,
        user_id=user_id,
        date_added_col=date_added_col,
        release_date_col=release_date_col,
        base_media_id_col=base_media_id_col,
        tie_breaker_col=tie_breaker_col,
    )


@router.get("/{addon_id}/manifest.json", include_in_schema=False)
async def stremio_addon_manifest(
    addon_id: str,
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    config = await get_addon_config_by_id(db, addon_id)
    if not config or not config.is_enabled:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found")
    catalogs = _normalize_catalogs(config.default_catalogs)
    custom_result = await db.execute(
        select(StremioCustomCatalog).where(StremioCustomCatalog.user_id == config.user_id)
    )
    custom_catalogs = custom_result.scalars().all()
    return _build_manifest(catalogs, custom_catalogs)


@router.get("/{addon_id}/catalog/{catalog_type}/{catalog_id}.json", include_in_schema=False)
async def stremio_addon_catalog(
    addon_id: str,
    catalog_type: Literal["movie", "series"],
    catalog_id: str,
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    return await _serve_catalog(addon_id, catalog_type, catalog_id, request, db, None)


@router.get(
    "/{addon_id}/catalog/{catalog_type}/{catalog_id}/{extra_path}.json",
    include_in_schema=False,
)
async def stremio_addon_catalog_extra(
    addon_id: str,
    catalog_type: Literal["movie", "series"],
    catalog_id: str,
    extra_path: str,
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    extras = _parse_extra_path(extra_path)
    return await _serve_catalog(addon_id, catalog_type, catalog_id, request, db, extras)


async def _serve_catalog(
    addon_id: str,
    catalog_type: Literal["movie", "series"],
    catalog_id: str,
    request: Request,
    db: AsyncSession,
    extra_overrides: dict[str, str] | None,
) -> dict[str, Any]:
    config = await get_addon_config_by_id(db, addon_id)
    if not config or not config.is_enabled:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found")

    catalogs = _normalize_catalogs(config.default_catalogs)
    catalogs_by_id = _catalogs_by_id(catalogs)
    catalog = catalogs_by_id.get(catalog_id)
    custom_catalog = None
    if not catalog:
        custom_result = await db.execute(
            select(StremioCustomCatalog).where(
                StremioCustomCatalog.user_id == config.user_id,
                StremioCustomCatalog.slug == catalog_id,
            )
        )
        custom_catalog = custom_result.scalars().first()
        if not custom_catalog:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Catalog not found")
        expected_type = _resolve_stremio_type(custom_catalog.media_type)
        if expected_type != catalog_type:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Catalog not found")
    else:
        if not catalog.get("enabled", True):
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Catalog not found")
        expected_type = _resolve_stremio_type(str(catalog.get("media_type")))
        if expected_type != catalog_type:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Catalog not found")

    skip, limit, search = _resolve_pagination(request, extra_overrides)

    if custom_catalog:
        query = await _build_custom_catalog_query(custom_catalog, search)
        order_by = custom_catalog.order_by or "manual"
        order_dir = custom_catalog.order_dir or "asc"
        if order_by == "manual":
            query = query.order_by(
                StremioCustomCatalogItem.position.asc(), StremioCustomCatalogItem.created_at.asc()
            )
        else:
            release_date_expr = func.coalesce(MediaItem.release_date, MediaItem.first_air_date)
            query = _apply_ordering(
                query,
                order_by=order_by,
                order_dir=order_dir,
                user_id=config.user_id,
                date_added_col=StremioCustomCatalogItem.created_at,
                release_date_col=release_date_expr,
                base_media_id_col=MediaItem.id,
                tie_breaker_col=MediaItem.id,
            )
    else:
        if catalog_id == "in_progress_shows":
            query = await _build_in_progress_query(config.user_id, catalog, search)
        else:
            query = await _build_watchlist_query(config.user_id, catalog, search)
        ordering = catalog.get("ordering") if isinstance(catalog.get("ordering"), dict) else {}
        order_by = str(ordering.get("order_by") or "date_added")
        order_dir = str(ordering.get("order_dir") or "desc")
        release_date_expr = func.coalesce(MediaItem.release_date, MediaItem.first_air_date)
        query = _apply_ordering(
            query,
            order_by=order_by,
            order_dir=order_dir,
            user_id=config.user_id,
            date_added_col=WatchlistItem.created_at,
            release_date_col=release_date_expr,
            base_media_id_col=MediaItem.id,
            tie_breaker_col=MediaItem.id,
        )

    query = query.offset(skip).limit(limit + 1)
    result = await db.execute(query)
    media_items = result.scalars().all()
    has_more = len(media_items) > limit
    if has_more:
        media_items = media_items[:limit]

    metas: list[dict[str, Any]] = []
    for media in media_items:
        meta = _build_meta(media, catalog_type)
        if meta:
            metas.append(meta)

    payload: dict[str, Any] = {"metas": metas}
    if has_more:
        payload["hasMore"] = True
    return payload
