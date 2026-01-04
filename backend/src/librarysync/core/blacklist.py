from __future__ import annotations

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from librarysync.db.models import BlacklistItem


def normalize_id(value: str | None) -> str | None:
    if value is None:
        return None
    cleaned = value.strip()
    return cleaned or None


def normalize_imdb_id(value: str | None) -> str | None:
    if not value:
        return None
    cleaned = value.strip().lower()
    if cleaned.startswith("tt") and cleaned[2:].isdigit():
        return cleaned
    return None


async def find_blacklisted_show(
    db: AsyncSession,
    user_id: str,
    *,
    imdb_id: str | None = None,
    tmdb_id: str | None = None,
    tvdb_id: str | None = None,
    tvmaze_id: str | None = None,
) -> BlacklistItem | None:
    imdb_id = normalize_imdb_id(imdb_id)
    tmdb_id = normalize_id(tmdb_id)
    tvdb_id = normalize_id(tvdb_id)
    tvmaze_id = normalize_id(tvmaze_id)
    filters = []
    if imdb_id:
        filters.append(BlacklistItem.imdb_id == imdb_id)
    if tmdb_id:
        filters.append(BlacklistItem.tmdb_id == tmdb_id)
    if tvdb_id:
        filters.append(BlacklistItem.tvdb_id == tvdb_id)
    if tvmaze_id:
        filters.append(BlacklistItem.tvmaze_id == tvmaze_id)
    if not filters:
        return None
    result = await db.execute(
        select(BlacklistItem).where(
            BlacklistItem.user_id == user_id,
            BlacklistItem.media_type == "tv",
            or_(*filters),
        )
    )
    return result.scalars().first()
