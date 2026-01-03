"""Anime detection and classification helpers."""

from __future__ import annotations

from typing import Any

from librarysync.db.models import MediaItem


def is_anime(
    media_item: MediaItem | None = None,
    *,
    media_type: str | None = None,
    kitsu_id: str | None = None,
    myanimelist_id: str | None = None,
    anilist_id: str | None = None,
    raw: dict[str, Any] | None = None,
) -> bool:
    """
    Determine if media is anime based on available metadata.

    Anime is detected via:
    1. media_type == "anime"
    2. presence of kitsu_id, myanimelist_id, or anilist_id
    3. raw metadata type field == "anime"

    Args:
        media_item: Optional MediaItem to check
        media_type: Explicit media type to check
        kitsu_id: Kitsu ID (anime-specific provider)
        myanimelist_id: MyAnimeList ID (anime-specific provider)
        anilist_id: AniList ID (anime-specific provider)
        raw: Raw metadata dictionary that may contain type info

    Returns:
        True if the media is anime, False otherwise
    """
    # If media_item is provided, extract values from it
    if media_item:
        media_type = media_item.media_type
        kitsu_id = media_item.kitsu_id
        myanimelist_id = media_item.myanimelist_id
        anilist_id = media_item.anilist_id
        raw = media_item.raw

    # Check media_type
    if media_type == "anime":
        return True

    # Check for anime-specific provider IDs
    if kitsu_id or myanimelist_id or anilist_id:
        return True

    # Check raw metadata for type field
    if raw and isinstance(raw, dict):
        raw_type = raw.get("type")
        if raw_type == "anime":
            return True

    return False


def get_anime_provider_ids(media_item: MediaItem) -> dict[str, str]:
    """
    Extract anime-specific provider IDs from a media item.

    Args:
        media_item: MediaItem to extract IDs from

    Returns:
        Dictionary of provider IDs (only includes non-None values)
    """
    ids = {}
    if media_item.kitsu_id:
        ids["kitsu"] = media_item.kitsu_id
    if media_item.myanimelist_id:
        ids["myanimelist"] = media_item.myanimelist_id
    if media_item.anilist_id:
        ids["anilist"] = media_item.anilist_id
    return ids
