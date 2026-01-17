from __future__ import annotations

import copy
import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from librarysync.db.models import StremioAddonConfig

DEFAULT_CATALOGS: list[dict[str, Any]] = [
    {
        "id": "watchlist_movies",
        "name": "Watchlist Movies",
        "media_type": "movie",
        "enabled": True,
        "filters": {"statuses": ["added", "in_progress", "not_released"]},
        "ordering": {"order_by": "date_added", "order_dir": "desc"},
    },
    {
        "id": "watchlist_shows",
        "name": "Watchlist Shows",
        "media_type": "tv",
        "enabled": True,
        "filters": {"statuses": ["added", "in_progress", "not_released"]},
        "ordering": {"order_by": "date_added", "order_dir": "desc"},
    },
    {
        "id": "watchlist_anime",
        "name": "Watchlist Anime",
        "media_type": "anime",
        "enabled": False,
        "filters": {"statuses": ["added", "in_progress", "not_released"]},
        "ordering": {"order_by": "date_added", "order_dir": "desc"},
    },
    {
        "id": "in_progress_shows",
        "name": "In Progress",
        "media_type": "tv",
        "enabled": True,
        "filters": {"statuses": ["added", "in_progress", "not_released"]},
        "ordering": {"order_by": "episodes_left", "order_dir": "asc"},
    },
]


def build_default_catalogs() -> list[dict[str, Any]]:
    return copy.deepcopy(DEFAULT_CATALOGS)


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
    result = await db.execute(
        select(StremioAddonConfig).where(StremioAddonConfig.id == addon_id)
    )
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
