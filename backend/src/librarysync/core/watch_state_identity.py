"""Resolve protocol identities without guessing episode numbering."""

from __future__ import annotations

from fastapi import HTTPException
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from librarysync.core.watchlist import apply_media_id_update
from librarysync.db.models import MediaItem

EVENT_TYPE = "aiostreams_watch_state"
ID_FIELDS = {
    "imdb": "imdb_id",
    "tmdb": "tmdb_id",
    "tvdb": "tvdb_id",
    "tvmaze": "tvmaze_id",
    "kitsu": "kitsu_id",
    "mal": "myanimelist_id",
    "anilist": "anilist_id",
}


def _media_ids(payload: dict) -> dict[str, str]:
    ids = {
        field: str(payload["ids"][provider]).strip()
        for provider, field in ID_FIELDS.items()
        if payload.get("ids", {}).get(provider)
    }
    meta_id = payload["metaId"]
    meta_field = None
    meta_value = None
    if meta_id.startswith("tt"):
        meta_field, meta_value = "imdb_id", meta_id
    elif ":" in meta_id:
        prefix, value = meta_id.split(":", 1)
        if prefix in ID_FIELDS and value.isdigit():
            meta_field, meta_value = ID_FIELDS[prefix], value
    if meta_field:
        if len(meta_value) > 32:
            raise HTTPException(422, "Provider identifier exceeds 32 characters")
        if ids.get(meta_field, meta_value) != meta_value:
            raise HTTPException(409, "Watch event identifiers do not match the meta id")
        ids[meta_field] = meta_value
    return ids


async def _resolve_media(db: AsyncSession, media_type: str, payload: dict) -> MediaItem | None:
    if payload.get("_canonical_media_item_id"):
        media = await db.get(MediaItem, payload["_canonical_media_item_id"])
        if not media:
            raise HTTPException(409, "Mapped title no longer exists")
        return media
    ids = _media_ids(payload)
    clauses = [getattr(MediaItem, field) == value for field, value in ids.items()]
    clauses.extend(
        [
            MediaItem.raw["stremio_id"].as_string() == payload["metaId"],
            MediaItem.raw["stremio"]["id"].as_string() == payload["metaId"],
            MediaItem.raw["stremio"]["_id"].as_string() == payload["metaId"],
        ]
    )
    types = ["movie"] if media_type == "movie" else ["tv", "anime", "series"]
    result = await db.execute(select(MediaItem).where(MediaItem.media_type.in_(types), or_(*clauses)))
    matches = list(result.scalars().all())
    if len(matches) > 1:
        raise HTTPException(409, "Watch event identifiers refer to different titles")
    if matches:
        media = matches[0]
        # Keep identifiers learned from the client available to downstream sync.
        for field, value in ids.items():
            await apply_media_id_update(db, media, field, value)
        return media
    if payload["event"] in {"unplayed", "unrated", "unwatchlisted", "undropped"}:
        return None
    media = MediaItem(
        media_type="movie" if media_type == "movie" else "tv",
        title=payload["metaId"],
        raw={"stremio_id": payload["metaId"], "source": EVENT_TYPE},
        **ids,
    )
    db.add(media)
    await db.flush()
    return media
