from __future__ import annotations

import copy
import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from librarysync.db.models import MediaItem, StremioAddonConfig

DEFAULT_PAGE_SIZE = 30
DEFAULT_SHOW_IN_HOME = True


def resolve_meta_id(media_item: MediaItem) -> str | None:
    raw = media_item.raw if isinstance(media_item.raw, dict) else {}
    stremio_payload = raw.get("stremio")
    stremio_id = raw.get("stremio_id")
    if not stremio_id and isinstance(stremio_payload, dict):
        stremio_id = stremio_payload.get("id") or stremio_payload.get("_id")
    if stremio_id:
        return str(stremio_id)
    if media_item.imdb_id:
        return media_item.imdb_id
    for prefix, field in (("tmdb", "tmdb_id"), ("tvdb", "tvdb_id"), ("kitsu", "kitsu_id")):
        value = getattr(media_item, field)
        if value:
            return f"{prefix}:{value}"
    return None

DEFAULT_CATALOGS: list[dict[str, Any]] = [
    {
        "id": "watchlist_movies",
        "name": "Watchlist Movies",
        "media_type": "movie",
        "enabled": True,
        "filters": {"statuses": []},
        "ordering": {"order_by": "date_added", "order_dir": "desc"},
        "pageSize": DEFAULT_PAGE_SIZE,
        "showInHome": DEFAULT_SHOW_IN_HOME,
    },
    {
        "id": "watchlist_shows",
        "name": "Watchlist Shows",
        "media_type": "tv",
        "enabled": True,
        "filters": {"statuses": []},
        "ordering": {"order_by": "date_added", "order_dir": "desc"},
        "pageSize": DEFAULT_PAGE_SIZE,
        "showInHome": DEFAULT_SHOW_IN_HOME,
    },
    {
        "id": "watchlist_anime",
        "name": "Watchlist Anime",
        "media_type": "anime",
        "enabled": False,
        "filters": {"statuses": []},
        "ordering": {"order_by": "date_added", "order_dir": "desc"},
        "pageSize": DEFAULT_PAGE_SIZE,
        "showInHome": DEFAULT_SHOW_IN_HOME,
    },
    {
        "id": "in_progress_shows",
        "name": "In Progress",
        "media_type": "tv",
        "enabled": True,
        "filters": {"statuses": [], "show_watched": False},
        "ordering": {"order_by": "episodes_left", "order_dir": "asc"},
        "pageSize": DEFAULT_PAGE_SIZE,
        "showInHome": DEFAULT_SHOW_IN_HOME,
    },
]


def build_default_catalogs() -> list[dict[str, Any]]:
    return copy.deepcopy(DEFAULT_CATALOGS)


def normalize_default_catalogs(catalogs: list[dict[str, Any]] | None) -> list[dict[str, Any]]:
    """Normalize catalog configurations with proper defaults."""
    base = catalogs or build_default_catalogs()
    normalized: list[dict[str, Any]] = []
    for catalog in base:
        clone = copy.deepcopy(catalog)

        # Normalize page size
        page_size = clone.get("pageSize")
        if not isinstance(page_size, int) or page_size <= 0:
            clone["pageSize"] = DEFAULT_PAGE_SIZE

        # Normalize show in home flag
        show_in_home = clone.get("showInHome")
        if not isinstance(show_in_home, bool):
            clone["showInHome"] = DEFAULT_SHOW_IN_HOME

        # Normalize in_progress_shows specific filters
        if clone.get("id") == "in_progress_shows":
            filters = clone.get("filters", {})
            if not isinstance(filters, dict):
                filters = {}
            if not isinstance(filters.get("show_watched"), bool):
                filters["show_watched"] = False
            clone["filters"] = filters

        normalized.append(clone)
    return normalized


async def get_addon_config_by_user(
    db: AsyncSession,
    user_id: str,
) -> StremioAddonConfig | None:
    result = await db.execute(
        select(StremioAddonConfig).where(StremioAddonConfig.user_id == user_id)
    )
    return result.scalars().first()


async def get_addon_config_by_id(
    db: AsyncSession,
    addon_id: str,
) -> StremioAddonConfig | None:
    result = await db.execute(select(StremioAddonConfig).where(StremioAddonConfig.id == addon_id))
    return result.scalars().first()


async def ensure_addon_config(
    db: AsyncSession,
    user_id: str,
) -> StremioAddonConfig:
    config = await get_addon_config_by_user(db, user_id)
    if config:
        return config
    config = StremioAddonConfig(
        id=str(uuid.uuid4()),
        user_id=user_id,
        is_enabled=True,
        default_catalogs=build_default_catalogs(),
    )
    db.add(config)
    await db.commit()
    await db.refresh(config)
    return config
