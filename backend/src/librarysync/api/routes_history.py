from datetime import datetime, timezone
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from librarysync.api.deps import get_current_user, get_db
from librarysync.db.models import EpisodeItem, MediaItem, User, WatchEvent, WatchedItem

router = APIRouter(prefix="/api/history", tags=["history"])


class WatchedItemOut(BaseModel):
    id: str
    watched_at: datetime
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


class WatchedItemCreateIn(BaseModel):
    watched_at: datetime | None = None
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

    watched = WatchedItem(
        user_id=current_user.id,
        media_item_id=None if episode_item else media_item.id,
        episode_item_id=episode_item.id if episode_item else None,
        watched_at=watched_at,
        source="api",
    )
    event = WatchEvent(
        user_id=current_user.id,
        media_item_id=media_item.id if not episode_item else None,
        episode_item_id=episode_item.id if episode_item else None,
        event_type="manual_watched",
        occurred_at=watched_at,
        raw={
            "source": "api",
            "ids": media_ids,
            "episode": {
                "season_number": episode_item.season_number,
                "episode_number": episode_item.episode_number,
                "title": episode_item.title,
                "ids": episode_ids,
            }
            if episode_item
            else None,
        },
    )
    db.add_all([watched, event])
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
    result = await db.execute(
        select(WatchedItem, MediaItem, EpisodeItem, show_item)
        .outerjoin(MediaItem, WatchedItem.media_item_id == MediaItem.id)
        .outerjoin(EpisodeItem, WatchedItem.episode_item_id == EpisodeItem.id)
        .outerjoin(show_item, EpisodeItem.show_media_item_id == show_item.id)
        .where(WatchedItem.user_id == current_user.id)
        .order_by(WatchedItem.watched_at.desc())
        .limit(limit)
    )
    items = []
    for watched, media_item, episode_item, show in result.all():
        base_item = media_item or show
        if not base_item:
            continue
        items.append(
            WatchedItemOut(
                id=watched.id,
                watched_at=watched.watched_at,
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
            ).model_dump()
        )
    return {"items": items}


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

    previous_watched_at = watched.watched_at
    watched_at = _normalize_datetime(payload.watched_at)
    watched.watched_at = watched_at

    event = WatchEvent(
        user_id=current_user.id,
        media_item_id=watched.media_item_id,
        episode_item_id=watched.episode_item_id,
        event_type="manual_watched_updated",
        occurred_at=watched_at,
        raw={
            "watched_id": watched.id,
            "previous_watched_at": previous_watched_at.isoformat()
            if previous_watched_at
            else None,
        },
    )
    db.add_all([watched, event])
    await db.commit()
    return {"watched_id": watched.id, "watched_at": watched.watched_at}


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
