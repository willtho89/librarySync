from __future__ import annotations

import copy
import hashlib
import hmac
import secrets
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from librarysync.config import settings
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


def generate_addon_key() -> str:
    return secrets.token_urlsafe(32)


def hash_addon_key(addon_key: str) -> str:
    if not settings.secret_key:
        raise RuntimeError("LIBRARYSYNC_SECRET_KEY is not set")
    return hmac.new(
        settings.secret_key.encode("utf-8"),
        addon_key.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()


async def get_addon_config_by_user(
    db: AsyncSession,
    user_id: str,
) -> StremioAddonConfig | None:
    result = await db.execute(
        select(StremioAddonConfig).where(StremioAddonConfig.user_id == user_id)
    )
    return result.scalars().first()


async def get_addon_config_by_key(
    db: AsyncSession,
    addon_key: str,
) -> StremioAddonConfig | None:
    key_hash = hash_addon_key(addon_key)
    result = await db.execute(
        select(StremioAddonConfig).where(StremioAddonConfig.addon_key_hash == key_hash)
    )
    config = result.scalars().first()
    if not config:
        return None
    if not hmac.compare_digest(config.addon_key_hash, key_hash):
        return None
    return config


async def ensure_addon_config(
    db: AsyncSession,
    user_id: str,
) -> tuple[StremioAddonConfig, str | None]:
    config = await get_addon_config_by_user(db, user_id)
    if config:
        return config, None
    addon_key = generate_addon_key()
    now = datetime.now(timezone.utc)
    config = StremioAddonConfig(
        user_id=user_id,
        is_enabled=True,
        addon_key_hash=hash_addon_key(addon_key),
        addon_key_last_rotated_at=now,
        default_catalogs=build_default_catalogs(),
    )
    db.add(config)
    await db.commit()
    await db.refresh(config)
    return config, addon_key


async def rotate_addon_key(
    db: AsyncSession,
    config: StremioAddonConfig,
) -> str:
    addon_key = generate_addon_key()
    config.addon_key_hash = hash_addon_key(addon_key)
    config.addon_key_last_rotated_at = datetime.now(timezone.utc)
    db.add(config)
    await db.commit()
    await db.refresh(config)
    return addon_key
