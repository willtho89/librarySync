import re
from datetime import datetime, timezone
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel
from sqlalchemy import and_, func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from librarysync.api.deps import get_current_user, get_db
from librarysync.connectors.services.letterboxd import has_required_letterboxd_fields
from librarysync.connectors.services.trakt import has_required_trakt_fields
from librarysync.config import settings
from librarysync.core.integrations import load_integration_with_secrets
from librarysync.core.ratings import normalize_star_rating
from librarysync.db.models import (
    EpisodeItem,
    MediaItem,
    OutboxJob,
    User,
    WatchEvent,
    WatchSync,
    WatchedItem,
)

router = APIRouter(prefix="/api/history", tags=["history"])


class WatchedItemOut(BaseModel):
    id: str
    watched_at: datetime
    rating: float | None = None
    media_type: str
    title: str
    year: int | None
    imdb_id: str | None
    tmdb_id: str | None
    tvdb_id: str | None
    kitsu_id: str | None
    tvmaze_id: str | None
    myanimelist_id: str | None
    poster_url: str | None
    season_number: int | None
    episode_number: int | None
    episode_title: str | None
    episode_imdb_id: str | None
    episode_tmdb_id: str | None
    episode_tvdb_id: str | None
    episode_tvmaze_id: str | None
    letterboxd_status: str | None = None
    letterboxd_external_id: str | None = None
    letterboxd_rewatch: bool | None = None
    letterboxd_last_error: str | None = None
    trakt_status: str | None = None
    trakt_external_id: str | None = None
    trakt_last_error: str | None = None


class WatchedItemCreateIn(BaseModel):
    watched_at: datetime | None = None
    rating: float | None = None
    media_type: Literal["movie", "tv", "anime"] = "movie"
    imdb_id: str | None = None
    tmdb_id: str | None = None
    tvdb_id: str | None = None
    tvmaze_id: str | None = None
    kitsu_id: str | None = None
    myanimelist_id: str | None = None
    title: str | None = None
    year: int | None = None
    poster_url: str | None = None
    season_number: int | None = None
    episode_number: int | None = None
    episode_title: str | None = None
    episode_imdb_id: str | None = None
    episode_tmdb_id: str | None = None
    episode_tvdb_id: str | None = None
    episode_tvmaze_id: str | None = None


class WatchedItemUpdateIn(BaseModel):
    watched_at: datetime | None = None
    rating: float | None = None


@router.post(
    "/items",
    status_code=status.HTTP_201_CREATED,
    summary="Add watched item",
    description="Record a manual watched entry for a movie or TV episode.",
)
async def add_watched_item(
    payload: WatchedItemCreateIn,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    media_ids = _extract_media_ids(payload)
    if not media_ids:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Provide at least one external ID",
        )

    if payload.media_type != "tv" and _has_episode_fields(payload):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Episode fields are only valid for TV items",
        )

    media_item = await _find_media_item_by_ids(db, payload.media_type, media_ids)
    if media_item and media_item.media_type != payload.media_type:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Media type does not match existing item",
        )

    if not media_item:
        title = payload.title or _fallback_title(media_ids)
        media_item = MediaItem(
            media_type=payload.media_type,
            title=title,
            year=payload.year,
            poster_url=payload.poster_url,
            imdb_id=media_ids.get("imdb_id"),
            tmdb_id=media_ids.get("tmdb_id"),
            tvdb_id=media_ids.get("tvdb_id"),
            tvmaze_id=media_ids.get("tvmaze_id"),
            kitsu_id=media_ids.get("kitsu_id"),
            myanimelist_id=media_ids.get("myanimelist_id"),
            raw={"source": "api", "ids": media_ids},
        )
        db.add(media_item)
        await db.flush()
    else:
        _apply_id_update(media_item, "imdb_id", media_ids.get("imdb_id"))
        _apply_id_update(media_item, "tmdb_id", media_ids.get("tmdb_id"))
        _apply_id_update(media_item, "tvdb_id", media_ids.get("tvdb_id"))
        _apply_id_update(media_item, "tvmaze_id", media_ids.get("tvmaze_id"))
        _apply_id_update(media_item, "kitsu_id", media_ids.get("kitsu_id"))
        _apply_id_update(media_item, "myanimelist_id", media_ids.get("myanimelist_id"))
        if payload.year is not None and media_item.year is None:
            media_item.year = payload.year
        if payload.poster_url and not media_item.poster_url:
            media_item.poster_url = payload.poster_url

    watched_at = _normalize_datetime(payload.watched_at)
    rating = _normalize_rating(payload.rating)
    episode_item: EpisodeItem | None = None
    episode_ids: dict[str, str] = {}

    if payload.media_type == "tv":
        season_number = _normalize_episode_number(payload.season_number, "season")
        episode_number = _normalize_episode_number(payload.episode_number, "episode")
        episode_ids = _extract_episode_ids(payload)
        episode_item = await _find_episode_item_by_ids(
            db,
            media_item.id,
            season_number,
            episode_number,
            episode_ids,
        )
        if episode_item and episode_item.show_media_item_id != media_item.id:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="Episode does not match the selected series",
            )
        if not episode_item:
            episode_item = EpisodeItem(
                show_media_item_id=media_item.id,
                season_number=season_number,
                episode_number=episode_number,
                title=payload.episode_title,
                tmdb_id=episode_ids.get("tmdb_id"),
                tvdb_id=episode_ids.get("tvdb_id"),
                tvmaze_id=episode_ids.get("tvmaze_id"),
                imdb_id=episode_ids.get("imdb_id"),
                raw={
                    "source": "api",
                    "ids": {**media_ids, **episode_ids},
                    "season_number": season_number,
                    "episode_number": episode_number,
                },
            )
            db.add(episode_item)
            await db.flush()
        else:
            _apply_episode_id_update(episode_item, "imdb_id", episode_ids.get("imdb_id"))
            _apply_episode_id_update(episode_item, "tmdb_id", episode_ids.get("tmdb_id"))
            _apply_episode_id_update(episode_item, "tvdb_id", episode_ids.get("tvdb_id"))
            _apply_episode_id_update(
                episode_item, "tvmaze_id", episode_ids.get("tvmaze_id")
            )
            if payload.episode_title and not episode_item.title:
                episode_item.title = payload.episode_title
        episode_ids = {
            key: value
            for key, value in {
                "imdb_id": episode_item.imdb_id,
                "tmdb_id": episode_item.tmdb_id,
                "tvdb_id": episode_item.tvdb_id,
                "tvmaze_id": episode_item.tvmaze_id,
            }.items()
            if value
        }

    is_rewatch = await _is_rewatch(
        db,
        current_user.id,
        media_item.id if not episode_item else None,
        episode_item.id if episode_item else None,
    )
    watched = WatchedItem(
        user_id=current_user.id,
        media_item_id=None if episode_item else media_item.id,
        episode_item_id=episode_item.id if episode_item else None,
        watched_at=watched_at,
        rating=rating,
        source="api",
    )
    event_raw = {
        "source": "api",
        "ids": media_ids,
        "rewatch": is_rewatch,
        "episode": {
            "season_number": episode_item.season_number,
            "episode_number": episode_item.episode_number,
            "title": episode_item.title,
            "ids": episode_ids,
        }
        if episode_item
        else None,
    }
    if rating is not None:
        event_raw["rating"] = rating
    event = WatchEvent(
        user_id=current_user.id,
        media_item_id=media_item.id if not episode_item else None,
        episode_item_id=episode_item.id if episode_item else None,
        event_type="manual_watched",
        occurred_at=watched_at,
        raw=event_raw,
    )
    db.add_all([watched, event])
    await db.flush()
    await _enqueue_letterboxd_sync(
        db, current_user.id, watched, media_item, watched_at, is_rewatch, rating
    )
    await _enqueue_trakt_sync(
        db,
        current_user.id,
        watched,
        media_item,
        episode_item,
        watched_at,
        is_rewatch,
        rating,
    )
    await db.commit()
    await db.refresh(watched)
    return {
        "watched_id": watched.id,
        "media_item_id": media_item.id,
        "episode_item_id": episode_item.id if episode_item else None,
    }


@router.get(
    "/items",
    summary="List watched items",
    description="Return the current user's watched history, newest first.",
)
async def list_watched_items(
    limit: int = Query(100, ge=1, le=500),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    show_item = aliased(MediaItem)
    letterboxd_sync = aliased(WatchSync)
    trakt_sync = aliased(WatchSync)
    result = await db.execute(
        select(
            WatchedItem,
            MediaItem,
            EpisodeItem,
            show_item,
            letterboxd_sync,
            trakt_sync,
        )
        .outerjoin(MediaItem, WatchedItem.media_item_id == MediaItem.id)
        .outerjoin(EpisodeItem, WatchedItem.episode_item_id == EpisodeItem.id)
        .outerjoin(show_item, EpisodeItem.show_media_item_id == show_item.id)
        .outerjoin(
            letterboxd_sync,
            and_(
                letterboxd_sync.watched_item_id == WatchedItem.id,
                letterboxd_sync.provider == "letterboxd",
            ),
        )
        .outerjoin(
            trakt_sync,
            and_(
                trakt_sync.watched_item_id == WatchedItem.id,
                trakt_sync.provider == "trakt",
            ),
        )
        .where(WatchedItem.user_id == current_user.id)
        .order_by(WatchedItem.watched_at.desc())
        .limit(limit)
    )
    items = []
    for watched, media_item, episode_item, show, sync, trakt in result.all():
        base_item = media_item or show
        if not base_item:
            continue
        items.append(
            WatchedItemOut(
                id=watched.id,
                watched_at=watched.watched_at,
                rating=watched.rating,
                media_type=base_item.media_type,
                title=base_item.title,
                year=base_item.year,
                imdb_id=base_item.imdb_id,
                tmdb_id=base_item.tmdb_id,
                tvdb_id=base_item.tvdb_id,
                kitsu_id=base_item.kitsu_id,
                tvmaze_id=base_item.tvmaze_id,
                myanimelist_id=base_item.myanimelist_id,
                poster_url=base_item.poster_url,
                season_number=episode_item.season_number if episode_item else None,
                episode_number=episode_item.episode_number if episode_item else None,
                episode_title=episode_item.title if episode_item else None,
                episode_imdb_id=episode_item.imdb_id if episode_item else None,
                episode_tmdb_id=episode_item.tmdb_id if episode_item else None,
                episode_tvdb_id=episode_item.tvdb_id if episode_item else None,
                episode_tvmaze_id=episode_item.tvmaze_id if episode_item else None,
                letterboxd_status=sync.status if sync else None,
                letterboxd_external_id=sync.external_id if sync else None,
                letterboxd_rewatch=sync.is_rewatch if sync else None,
                letterboxd_last_error=sync.last_error if sync else None,
                trakt_status=trakt.status if trakt else None,
                trakt_external_id=trakt.external_id if trakt else None,
                trakt_last_error=trakt.last_error if trakt else None,
            ).model_dump()
        )
    return {"items": _merge_history_items(items)}


@router.patch(
    "/items/{watched_id}",
    summary="Update watched item",
    description="Update the watch timestamp for an existing entry.",
)
async def update_watched_item(
    watched_id: str,
    payload: WatchedItemUpdateIn,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    result = await db.execute(
        select(WatchedItem).where(
            WatchedItem.id == watched_id, WatchedItem.user_id == current_user.id
        )
    )
    watched = result.scalars().first()
    if not watched:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Watched entry not found"
        )

    watched_at_updated = False
    rating_updated = False
    previous_watched_at = watched.watched_at
    previous_rating = watched.rating

    if "watched_at" in payload.model_fields_set:
        watched_at = _normalize_datetime(payload.watched_at)
        if watched_at != watched.watched_at:
            watched.watched_at = watched_at
            watched_at_updated = True
    if "rating" in payload.model_fields_set:
        normalized_rating = _normalize_rating(payload.rating)
        if normalized_rating != watched.rating:
            watched.rating = normalized_rating
            rating_updated = True

    if not watched_at_updated and not rating_updated:
        return {
            "watched_id": watched.id,
            "watched_at": watched.watched_at,
            "rating": watched.rating,
        }

    event_raw: dict[str, object] = {"watched_id": watched.id}
    if watched_at_updated:
        event_raw["previous_watched_at"] = (
            previous_watched_at.isoformat() if previous_watched_at else None
        )
        event_raw["watched_at"] = watched.watched_at.isoformat()
    if rating_updated:
        event_raw["previous_rating"] = previous_rating
        event_raw["rating"] = watched.rating

    event_time = watched.watched_at if watched_at_updated else datetime.now(timezone.utc)
    event = WatchEvent(
        user_id=current_user.id,
        media_item_id=watched.media_item_id,
        episode_item_id=watched.episode_item_id,
        event_type="manual_watched_updated",
        occurred_at=event_time,
        raw=event_raw,
    )
    db.add_all([watched, event])
    if watched_at_updated or rating_updated:
        media_item: MediaItem | None = None
        episode_item: EpisodeItem | None = None
        show_item: MediaItem | None = None
        if watched.media_item_id:
            result = await db.execute(
                select(MediaItem).where(MediaItem.id == watched.media_item_id)
            )
            media_item = result.scalars().first()
        if watched.episode_item_id:
            result = await db.execute(
                select(EpisodeItem).where(EpisodeItem.id == watched.episode_item_id)
            )
            episode_item = result.scalars().first()
            if episode_item:
                result = await db.execute(
                    select(MediaItem).where(
                        MediaItem.id == episode_item.show_media_item_id
                    )
                )
                show_item = result.scalars().first()
        if media_item:
            await _enqueue_letterboxd_update_sync(
                db,
                current_user.id,
                watched,
                media_item,
                watched_at_updated,
                rating_updated,
            )
        if media_item or episode_item:
            await _enqueue_trakt_update_sync(
                db,
                current_user.id,
                watched,
                media_item or show_item,
                episode_item,
                watched_at_updated,
                rating_updated,
            )
    await db.commit()
    return {
        "watched_id": watched.id,
        "watched_at": watched.watched_at,
        "rating": watched.rating,
    }


@router.delete(
    "/items/{watched_id}",
    summary="Delete watched item",
    description="Remove a watched entry from history.",
)
async def delete_watched_item(
    watched_id: str,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    result = await db.execute(
        select(WatchedItem).where(
            WatchedItem.id == watched_id, WatchedItem.user_id == current_user.id
        )
    )
    watched = result.scalars().first()
    if not watched:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Watched entry not found"
        )

    event = WatchEvent(
        user_id=current_user.id,
        media_item_id=watched.media_item_id,
        episode_item_id=watched.episode_item_id,
        event_type="manual_watched_deleted",
        occurred_at=datetime.now(timezone.utc),
        raw={
            "watched_id": watched.id,
            "previous_watched_at": watched.watched_at.isoformat()
            if watched.watched_at
            else None,
        },
    )
    db.add(event)
    await db.delete(watched)
    await db.commit()
    return {"status": "deleted"}


def _normalize_datetime(value: datetime | None) -> datetime:
    if value is None:
        return datetime.now(timezone.utc)
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value


def _normalize_id(value: str | None, lowercase: bool = False) -> str | None:
    if value is None:
        return None
    cleaned = value.strip()
    if not cleaned:
        return None
    return cleaned.lower() if lowercase else cleaned


def _normalize_episode_number(value: int | None, label: str) -> int:
    if value is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"{label.title()} number is required for TV items",
        )
    if value < 0:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"{label.title()} number must be 0 or higher",
        )
    return value


def _normalize_rating(value: object | None) -> float | None:
    try:
        return normalize_star_rating(value)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)
        ) from exc


def _has_episode_fields(payload: WatchedItemCreateIn) -> bool:
    return any(
        value is not None
        for value in (
            payload.season_number,
            payload.episode_number,
            payload.episode_title,
            payload.episode_imdb_id,
            payload.episode_tmdb_id,
            payload.episode_tvdb_id,
            payload.episode_tvmaze_id,
        )
    )


def _extract_media_ids(payload: WatchedItemCreateIn) -> dict[str, str]:
    ids: dict[str, str] = {}
    imdb_id = _normalize_id(payload.imdb_id, lowercase=True)
    if imdb_id:
        ids["imdb_id"] = imdb_id
    tmdb_id = _normalize_id(payload.tmdb_id)
    if tmdb_id:
        ids["tmdb_id"] = tmdb_id
    tvdb_id = _normalize_id(payload.tvdb_id)
    if tvdb_id:
        ids["tvdb_id"] = tvdb_id
    tvmaze_id = _normalize_id(payload.tvmaze_id)
    if tvmaze_id:
        ids["tvmaze_id"] = tvmaze_id
    kitsu_id = _normalize_id(payload.kitsu_id)
    if kitsu_id:
        ids["kitsu_id"] = kitsu_id
    myanimelist_id = _normalize_id(payload.myanimelist_id)
    if myanimelist_id:
        ids["myanimelist_id"] = myanimelist_id
    return ids


def _extract_episode_ids(payload: WatchedItemCreateIn) -> dict[str, str]:
    ids: dict[str, str] = {}
    imdb_id = _normalize_id(payload.episode_imdb_id, lowercase=True)
    if imdb_id:
        ids["imdb_id"] = imdb_id
    tmdb_id = _normalize_id(payload.episode_tmdb_id)
    if tmdb_id:
        ids["tmdb_id"] = tmdb_id
    tvdb_id = _normalize_id(payload.episode_tvdb_id)
    if tvdb_id:
        ids["tvdb_id"] = tvdb_id
    tvmaze_id = _normalize_id(payload.episode_tvmaze_id)
    if tvmaze_id:
        ids["tvmaze_id"] = tvmaze_id
    return ids


def _fallback_title(ids: dict[str, str]) -> str:
    if ids.get("imdb_id"):
        return f"IMDb {ids['imdb_id']}"
    if ids.get("tmdb_id"):
        return f"TMDB {ids['tmdb_id']}"
    if ids.get("tvdb_id"):
        return f"TVDB {ids['tvdb_id']}"
    if ids.get("tvmaze_id"):
        return f"TVMaze {ids['tvmaze_id']}"
    if ids.get("kitsu_id"):
        return f"Kitsu {ids['kitsu_id']}"
    if ids.get("myanimelist_id"):
        return f"MAL {ids['myanimelist_id']}"
    return "Unknown title"


def _apply_id_update(item: MediaItem, field: str, value: str | None) -> None:
    if not value:
        return
    current = getattr(item, field)
    if current and current != value:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Conflicting {field} for existing item",
        )
    if not current:
        setattr(item, field, value)


def _apply_episode_id_update(item: EpisodeItem, field: str, value: str | None) -> None:
    if not value:
        return
    current = getattr(item, field)
    if current and current != value:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Conflicting episode {field} for existing item",
        )
    if not current:
        setattr(item, field, value)


async def _is_rewatch(
    db: AsyncSession,
    user_id: str,
    media_item_id: str | None,
    episode_item_id: str | None,
) -> bool:
    if media_item_id:
        result = await db.execute(
            select(WatchedItem.id).where(
                WatchedItem.user_id == user_id,
                WatchedItem.media_item_id == media_item_id,
            ).limit(1)
        )
        return result.scalars().first() is not None
    if episode_item_id:
        result = await db.execute(
            select(WatchedItem.id).where(
                WatchedItem.user_id == user_id,
                WatchedItem.episode_item_id == episode_item_id,
            ).limit(1)
        )
        return result.scalars().first() is not None
    return False


def _merge_history_items(items: list[dict]) -> list[dict]:
    if not items:
        return []
    groups: dict[str, list[dict]] = {}
    key_map: dict[str, str] = {}
    for item in items:
        keys = _history_merge_keys(item)
        group_id = None
        for key in keys:
            group_id = key_map.get(key)
            if group_id:
                break
        if not group_id:
            group_id = keys[0] if keys else f"id:{item.get('id')}"
            groups[group_id] = [item]
            for key in keys:
                key_map[key] = group_id
            continue
        groups[group_id].append(item)
        for key in keys:
            key_map.setdefault(key, group_id)

    merged: list[dict] = []
    for group_items in groups.values():
        base = max(group_items, key=_history_item_score)
        merged_item = dict(base)
        merged_item["watched_at"] = max(
            item["watched_at"] for item in group_items if item.get("watched_at")
        )
        for field in _provider_fields():
            if merged_item.get(field) is None:
                for item in group_items:
                    value = item.get(field)
                    if value is not None:
                        merged_item[field] = value
                        break
        for field in _metadata_fields():
            if merged_item.get(field) in (None, ""):
                for item in group_items:
                    value = item.get(field)
                    if value not in (None, ""):
                        merged_item[field] = value
                        break
        for item in group_items:
            if item.get("rating") is not None and merged_item.get("rating") is None:
                merged_item["rating"] = item["rating"]
                break
        for item in group_items:
            title = item.get("title")
            if title and (
                not merged_item.get("title")
                or len(title) > len(str(merged_item.get("title")))
            ):
                merged_item["title"] = title
        merged.append(merged_item)
    merged.sort(key=lambda entry: entry.get("watched_at"), reverse=True)
    return merged


def _history_merge_keys(item: dict) -> list[str]:
    watched_at = item.get("watched_at")
    if isinstance(watched_at, datetime):
        date_key = watched_at.date().isoformat()
    else:
        date_key = "unknown"
    keys: list[str] = []
    media_type = item.get("media_type")
    if media_type == "movie":
        for label in ("imdb_id", "tmdb_id", "tvdb_id"):
            value = item.get(label)
            if value:
                keys.append(f"d:{date_key}:movie:{label}:{value}")
        title_key = _title_key(item.get("title"), item.get("year"))
        if title_key:
            keys.append(f"d:{date_key}:movie:title:{title_key}")
    elif media_type == "tv":
        season = item.get("season_number")
        episode = item.get("episode_number")
        if season is not None and episode is not None:
            for label in (
                "episode_imdb_id",
                "episode_tmdb_id",
                "episode_tvdb_id",
                "episode_tvmaze_id",
            ):
                value = item.get(label)
                if value:
                    keys.append(
                        f"d:{date_key}:episode:{label}:{value}:s{season}e{episode}"
                    )
            for label in ("imdb_id", "tmdb_id", "tvdb_id", "tvmaze_id"):
                value = item.get(label)
                if value:
                    keys.append(
                        f"d:{date_key}:show:{label}:{value}:s{season}e{episode}"
                    )
            title_key = _title_key(item.get("title"), item.get("year"))
            if title_key:
                keys.append(
                    f"d:{date_key}:show:title:{title_key}:s{season}e{episode}"
                )
    if not keys:
        fallback_id = item.get("id") or "unknown"
        keys.append(f"d:{date_key}:id:{fallback_id}")
    return keys


def _history_item_score(item: dict) -> int:
    score = 0
    for field in (
        "imdb_id",
        "tmdb_id",
        "tvdb_id",
        "tvmaze_id",
        "kitsu_id",
        "myanimelist_id",
        "episode_imdb_id",
        "episode_tmdb_id",
        "episode_tvdb_id",
        "episode_tvmaze_id",
    ):
        if item.get(field):
            score += 2
    if item.get("poster_url"):
        score += 1
    if item.get("year"):
        score += 1
    if item.get("title"):
        score += 1
    return score


def _title_key(title: object, year: object) -> str | None:
    if not isinstance(title, str):
        return None
    cleaned = re.sub(r"[^a-z0-9]+", " ", title.strip().lower())
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    if not cleaned:
        return None
    year_value = str(year) if isinstance(year, int) else ""
    return f"{cleaned}:{year_value}" if year_value else cleaned


def _provider_fields() -> tuple[str, ...]:
    return (
        "letterboxd_status",
        "letterboxd_external_id",
        "letterboxd_rewatch",
        "letterboxd_last_error",
        "trakt_status",
        "trakt_external_id",
        "trakt_last_error",
    )


def _metadata_fields() -> tuple[str, ...]:
    return (
        "imdb_id",
        "tmdb_id",
        "tvdb_id",
        "tvmaze_id",
        "kitsu_id",
        "myanimelist_id",
        "poster_url",
        "season_number",
        "episode_number",
        "episode_title",
        "episode_imdb_id",
        "episode_tmdb_id",
        "episode_tvdb_id",
        "episode_tvmaze_id",
    )


async def _has_same_day_watch(
    db: AsyncSession,
    user_id: str,
    media_item_id: str | None,
    episode_item_id: str | None,
    watched_at: datetime,
    exclude_watched_id: str | None = None,
) -> bool:
    if not media_item_id and not episode_item_id:
        return False
    target_date = watched_at.date()
    query = select(WatchedItem.id).where(
        WatchedItem.user_id == user_id,
        func.date(WatchedItem.watched_at) == target_date,
    )
    if media_item_id:
        query = query.where(WatchedItem.media_item_id == media_item_id)
    if episode_item_id:
        query = query.where(WatchedItem.episode_item_id == episode_item_id)
    if exclude_watched_id:
        query = query.where(WatchedItem.id != exclude_watched_id)
    query = query.limit(1)
    result = await db.execute(query)
    return result.scalars().first() is not None


async def _enqueue_letterboxd_sync(
    db: AsyncSession,
    user_id: str,
    watched: WatchedItem,
    media_item: MediaItem,
    watched_at: datetime,
    is_rewatch: bool,
    rating: float | None,
) -> None:
    if media_item.media_type != "movie":
        return
    if not media_item.imdb_id and not media_item.tmdb_id:
        return
    integration, secret_data = await load_integration_with_secrets(
        db, user_id, "letterboxd"
    )
    if not integration or not secret_data:
        return
    if not has_required_letterboxd_fields(secret_data):
        return
    watch_sync = WatchSync(
        user_id=user_id,
        watched_item_id=watched.id,
        provider="letterboxd",
        status="pending",
        is_rewatch=is_rewatch,
    )
    db.add(watch_sync)
    await db.flush()
    imdb_id = media_item.imdb_id.lower() if media_item.imdb_id else None
    tmdb_id = media_item.tmdb_id if media_item.tmdb_id else None
    job = OutboxJob(
        user_id=user_id,
        target_provider="letterboxd",
        job_type="push_watched",
        payload={
            "watch_sync_id": watch_sync.id,
            "watched_item_id": watched.id,
            "media_item_id": media_item.id,
            "imdb_id": imdb_id,
            "tmdb_id": tmdb_id,
            "watched_at": watched_at.isoformat(),
            "is_rewatch": is_rewatch,
            "rating": rating,
        },
        status="pending",
    )
    db.add(job)


async def _enqueue_letterboxd_update_sync(
    db: AsyncSession,
    user_id: str,
    watched: WatchedItem,
    media_item: MediaItem,
    watched_at_updated: bool,
    rating_updated: bool,
) -> None:
    if media_item.media_type != "movie":
        return
    integration, secret_data = await load_integration_with_secrets(
        db, user_id, "letterboxd"
    )
    if not integration or not secret_data:
        return
    if not has_required_letterboxd_fields(secret_data):
        return
    result = await db.execute(
        select(WatchSync).where(
            WatchSync.watched_item_id == watched.id, WatchSync.provider == "letterboxd"
        )
    )
    watch_sync = result.scalars().first()
    if not watch_sync or not watch_sync.external_id:
        return
    payload: dict[str, object] = {
        "watch_sync_id": watch_sync.id,
        "watched_item_id": watched.id,
        "media_item_id": media_item.id,
        "entry_id": watch_sync.external_id,
    }
    if watched_at_updated:
        payload["watched_at"] = watched.watched_at.isoformat()
    if rating_updated and watched.rating is not None:
        payload["rating"] = watched.rating
    if not {"watched_at", "rating"} & payload.keys():
        return

    watch_sync.status = "pending"
    watch_sync.last_error = None

    job = OutboxJob(
        user_id=user_id,
        target_provider="letterboxd",
        job_type="update_log_entry",
        payload=payload,
        status="pending",
    )
    db.add(job)


async def _enqueue_trakt_sync(
    db: AsyncSession,
    user_id: str,
    watched: WatchedItem,
    media_item: MediaItem,
    episode_item: EpisodeItem | None,
    watched_at: datetime,
    is_rewatch: bool,
    rating: float | None,
) -> None:
    if not settings.trakt_client_id or not settings.trakt_client_secret:
        return
    payload = _build_trakt_payload(media_item, episode_item, watched_at, rating)
    if not payload:
        return
    integration, secret_data = await load_integration_with_secrets(
        db, user_id, "trakt"
    )
    if not integration or not secret_data:
        return
    if not has_required_trakt_fields(secret_data):
        return

    same_day_duplicate = await _has_same_day_watch(
        db,
        user_id,
        media_item.id if not episode_item else None,
        episode_item.id if episode_item else None,
        watched_at,
        watched.id,
    )
    now = datetime.now(timezone.utc)
    watch_status = "pending"
    if same_day_duplicate and rating is None:
        watch_status = "assumed_tracked"

    watch_sync = WatchSync(
        user_id=user_id,
        watched_item_id=watched.id,
        provider="trakt",
        status=watch_status,
        is_rewatch=is_rewatch,
    )
    if watch_status == "assumed_tracked":
        watch_sync.last_synced_at = now
    db.add(watch_sync)
    await db.flush()

    payload["watch_sync_id"] = watch_sync.id
    payload["watched_item_id"] = watched.id
    if watch_status != "assumed_tracked" and not same_day_duplicate:
        job = OutboxJob(
            user_id=user_id,
            target_provider="trakt",
            job_type="push_watched",
            payload=payload,
            status="pending",
        )
        db.add(job)

    if rating is not None:
        rating_payload = dict(payload)
        rating_payload["rating"] = rating
        rating_job = OutboxJob(
            user_id=user_id,
            target_provider="trakt",
            job_type="push_rating",
            payload=rating_payload,
            status="pending",
        )
        db.add(rating_job)


async def _enqueue_trakt_update_sync(
    db: AsyncSession,
    user_id: str,
    watched: WatchedItem,
    media_item: MediaItem | None,
    episode_item: EpisodeItem | None,
    watched_at_updated: bool,
    rating_updated: bool,
) -> None:
    if not media_item:
        return
    if not settings.trakt_client_id or not settings.trakt_client_secret:
        return
    integration, secret_data = await load_integration_with_secrets(
        db, user_id, "trakt"
    )
    if not integration or not secret_data:
        return
    if not has_required_trakt_fields(secret_data):
        return
    result = await db.execute(
        select(WatchSync).where(
            WatchSync.watched_item_id == watched.id, WatchSync.provider == "trakt"
        )
    )
    watch_sync = result.scalars().first()
    if not watch_sync:
        return
    payload = _build_trakt_payload(
        media_item,
        episode_item,
        watched.watched_at,
        watched.rating,
    )
    if not payload:
        return
    payload["watch_sync_id"] = watch_sync.id
    payload["watched_item_id"] = watched.id
    if watch_sync.external_id:
        payload["history_id"] = watch_sync.external_id
    if watched_at_updated:
        payload["watched_at"] = watched.watched_at.isoformat()
    if rating_updated and watched.rating is not None:
        payload["rating"] = watched.rating

    watch_sync.status = "pending"
    watch_sync.last_error = None
    db.add(watch_sync)

    if watched_at_updated:
        job = OutboxJob(
            user_id=user_id,
            target_provider="trakt",
            job_type="update_history",
            payload=payload,
            status="pending",
        )
        db.add(job)
    if rating_updated and watched.rating is not None:
        rating_payload = dict(payload)
        rating_payload["rating"] = watched.rating
        job = OutboxJob(
            user_id=user_id,
            target_provider="trakt",
            job_type="push_rating",
            payload=rating_payload,
            status="pending",
        )
        db.add(job)


def _build_trakt_payload(
    media_item: MediaItem,
    episode_item: EpisodeItem | None,
    watched_at: datetime,
    rating: float | None,
) -> dict[str, object] | None:
    if episode_item:
        show_ids = _collect_trakt_ids(
            media_item.imdb_id, media_item.tmdb_id, media_item.tvdb_id
        )
        episode_ids = _collect_trakt_ids(
            episode_item.imdb_id, episode_item.tmdb_id, episode_item.tvdb_id
        )
        if not show_ids and not episode_ids:
            return None
        payload: dict[str, object] = {
            "media_type": "tv",
            "season_number": episode_item.season_number,
            "episode_number": episode_item.episode_number,
            "watched_at": watched_at.isoformat(),
        }
        if show_ids:
            payload["show_ids"] = show_ids
        if episode_ids:
            payload["episode_ids"] = episode_ids
        if rating is not None:
            payload["rating"] = rating
        return payload

    if media_item.media_type != "movie":
        return None
    movie_ids = _collect_trakt_ids(
        media_item.imdb_id, media_item.tmdb_id, media_item.tvdb_id
    )
    if not movie_ids:
        return None
    payload = {
        "media_type": "movie",
        "movie_ids": movie_ids,
        "watched_at": watched_at.isoformat(),
    }
    if rating is not None:
        payload["rating"] = rating
    return payload


def _collect_trakt_ids(
    imdb_id: str | None, tmdb_id: str | None, tvdb_id: str | None
) -> dict[str, object]:
    ids: dict[str, object] = {}
    if imdb_id:
        ids["imdb"] = imdb_id.lower()
    if tmdb_id:
        ids["tmdb"] = tmdb_id
    if tvdb_id:
        ids["tvdb"] = tvdb_id
    return ids


async def _find_media_item_by_ids(
    db: AsyncSession, media_type: str, ids: dict[str, str]
) -> MediaItem | None:
    item: MediaItem | None = None

    def _set_item(candidate: MediaItem | None) -> None:
        nonlocal item
        if not candidate:
            return
        if item and item.id != candidate.id:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="Provided IDs refer to different items",
            )
        item = candidate

    if ids.get("imdb_id"):
        result = await db.execute(
            select(MediaItem).where(MediaItem.imdb_id == ids["imdb_id"])
        )
        _set_item(result.scalars().first())
    if ids.get("tmdb_id"):
        result = await db.execute(
            select(MediaItem).where(
                MediaItem.tmdb_id == ids["tmdb_id"],
                MediaItem.media_type == media_type,
            )
        )
        _set_item(result.scalars().first())
    if ids.get("tvdb_id"):
        result = await db.execute(
            select(MediaItem).where(
                MediaItem.tvdb_id == ids["tvdb_id"],
                MediaItem.media_type == media_type,
            )
        )
        _set_item(result.scalars().first())
    if ids.get("tvmaze_id"):
        result = await db.execute(
            select(MediaItem).where(
                MediaItem.tvmaze_id == ids["tvmaze_id"],
                MediaItem.media_type == media_type,
            )
        )
        _set_item(result.scalars().first())
    if ids.get("kitsu_id"):
        result = await db.execute(
            select(MediaItem).where(
                MediaItem.kitsu_id == ids["kitsu_id"],
                MediaItem.media_type == media_type,
            )
        )
        _set_item(result.scalars().first())
    if ids.get("myanimelist_id"):
        result = await db.execute(
            select(MediaItem).where(
                MediaItem.myanimelist_id == ids["myanimelist_id"],
                MediaItem.media_type == media_type,
            )
        )
        _set_item(result.scalars().first())

    return item


async def _find_episode_item_by_ids(
    db: AsyncSession,
    show_media_item_id: str,
    season_number: int,
    episode_number: int,
    ids: dict[str, str],
) -> EpisodeItem | None:
    item: EpisodeItem | None = None

    def _set_item(candidate: EpisodeItem | None) -> None:
        nonlocal item
        if not candidate:
            return
        if item and item.id != candidate.id:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="Provided episode IDs refer to different items",
            )
        item = candidate

    if ids.get("imdb_id"):
        result = await db.execute(
            select(EpisodeItem).where(EpisodeItem.imdb_id == ids["imdb_id"])
        )
        _set_item(result.scalars().first())
    if ids.get("tmdb_id"):
        result = await db.execute(
            select(EpisodeItem).where(EpisodeItem.tmdb_id == ids["tmdb_id"])
        )
        _set_item(result.scalars().first())
    if ids.get("tvdb_id"):
        result = await db.execute(
            select(EpisodeItem).where(EpisodeItem.tvdb_id == ids["tvdb_id"])
        )
        _set_item(result.scalars().first())
    if ids.get("tvmaze_id"):
        result = await db.execute(
            select(EpisodeItem).where(EpisodeItem.tvmaze_id == ids["tvmaze_id"])
        )
        _set_item(result.scalars().first())

    result = await db.execute(
        select(EpisodeItem).where(
            EpisodeItem.show_media_item_id == show_media_item_id,
            EpisodeItem.season_number == season_number,
            EpisodeItem.episode_number == episode_number,
        )
    )
    _set_item(result.scalars().first())

    return item
