from datetime import datetime, timezone
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel
from sqlalchemy import delete, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from librarysync.api.deps import get_current_user, get_db
from librarysync.core.watchlist import evaluate_show_watchlist_status
from librarysync.db.models import (
    MediaItem,
    User,
    WatchedItem,
    WatchEvent,
    WatchlistItem,
)

router = APIRouter(prefix="/api/watchlist", tags=["watchlist"])


class WatchlistItemCreateIn(BaseModel):
    media_type: Literal["movie", "tv", "anime"] = "movie"
    imdb_id: str | None = None
    tmdb_id: str | None = None
    tvdb_id: str | None = None
    tvmaze_id: str | None = None
    kitsu_id: str | None = None
    myanimelist_id: str | None = None
    anilist_id: str | None = None
    title: str | None = None
    year: int | None = None
    poster_url: str | None = None


class WatchlistItemOut(BaseModel):
    id: str
    media_item_id: str
    type: str
    status: str
    source: str
    created_at: datetime
    updated_at: datetime
    media_type: str
    title: str
    year: int | None
    poster_url: str | None
    imdb_id: str | None
    tmdb_id: str | None
    tvdb_id: str | None
    release_date: str | None = None
    first_air_date: str | None = None
    progress: dict | None = None


@router.post(
    "/items",
    status_code=status.HTTP_201_CREATED,
    summary="Add watchlist item",
    description="Add a movie or show to the watchlist.",
)
async def add_watchlist_item(
    payload: WatchlistItemCreateIn,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    media_ids = _extract_media_ids(payload)
    if not media_ids:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Provide at least one external ID",
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
            anilist_id=media_ids.get("anilist_id"),
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
        _apply_id_update(media_item, "anilist_id", media_ids.get("anilist_id"))
        if payload.year is not None and media_item.year is None:
            media_item.year = payload.year
        if payload.poster_url and not media_item.poster_url:
            media_item.poster_url = payload.poster_url

    # Check existing watchlist item
    result = await db.execute(
        select(WatchlistItem).where(
            WatchlistItem.user_id == current_user.id,
            WatchlistItem.media_item_id == media_item.id,
        )
    )
    existing = result.scalars().first()
    if existing:
        if existing.status == "removed":
            initial_status = "active"
            if payload.media_type == "movie":
                # Check history
                w_result = await db.execute(
                    select(WatchedItem)
                    .where(
                        WatchedItem.user_id == current_user.id,
                        WatchedItem.media_item_id == media_item.id,
                    )
                    .limit(1)
                )
                if w_result.scalars().first():
                    initial_status = "watched"

            existing.status = initial_status
            existing.updated_at = datetime.now(timezone.utc)
            # Log event
            await _log_watchlist_event(
                db, current_user.id, media_item.id, "watchlist_added", {"restored": True}
            )
            await db.commit()

            # Evaluate for TV
            if payload.media_type == "tv":
                await evaluate_show_watchlist_status(db, current_user.id, existing, media_item)
                await db.commit()

            await db.refresh(existing)
            return {"id": existing.id, "status": "restored"}
        return {"id": existing.id, "status": "already_exists"}

    initial_status = "active"
    if payload.media_type == "movie":
        # Check history
        w_result = await db.execute(
            select(WatchedItem)
            .where(
                WatchedItem.user_id == current_user.id,
                WatchedItem.media_item_id == media_item.id,
            )
            .limit(1)
        )
        if w_result.scalars().first():
            initial_status = "watched"

    watchlist_item = WatchlistItem(
        user_id=current_user.id,
        media_item_id=media_item.id,
        type=payload.media_type,
        status=initial_status,
        source="manual",
    )
    db.add(watchlist_item)

    await _log_watchlist_event(db, current_user.id, media_item.id, "watchlist_added", {})
    await db.commit()

    # Evaluate for TV
    if payload.media_type == "tv":
        await db.refresh(watchlist_item)
        await evaluate_show_watchlist_status(db, current_user.id, watchlist_item, media_item)
        await db.commit()

    await db.refresh(watchlist_item)
    return {"id": watchlist_item.id, "status": "created"}


@router.get(
    "/items",
    summary="List watchlist items",
    description="Return the current user's watchlist.",
)
async def list_watchlist_items(
    limit: int = Query(100, ge=1, le=500),
    offset: int = Query(0, ge=0),
    status: str | None = Query("active", description="Comma-separated list of statuses"),
    media_type: Literal["movie", "tv", "anime"] | None = Query(None),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    query = (
        select(WatchlistItem, MediaItem)
        .join(MediaItem, WatchlistItem.media_item_id == MediaItem.id)
        .where(WatchlistItem.user_id == current_user.id)
    )

    if status and status != "all":
        statuses = [s.strip() for s in status.split(",") if s.strip()]
        if statuses:
            query = query.where(WatchlistItem.status.in_(statuses))

    if media_type:
        query = query.where(WatchlistItem.type == media_type)

    total_result = await db.execute(select(func.count()).select_from(query.subquery()))
    total = int(total_result.scalar() or 0)

    query = query.order_by(WatchlistItem.updated_at.desc()).offset(offset).limit(limit)
    result = await db.execute(query)

    items = []
    for item, media in result.all():
        progress = None
        if media.media_type == "tv":
            # For v1, this N+1 query is acceptable for small page size (25-100)
            # In future, use group by subquery or CTE
            progress = await _get_show_progress(db, current_user.id, media.id)

        items.append(
            WatchlistItemOut(
                id=item.id,
                media_item_id=media.id,
                type=item.type,
                status=item.status,
                source=item.source,
                created_at=item.created_at,
                updated_at=item.updated_at,
                media_type=media.media_type,
                title=media.title,
                year=media.year,
                poster_url=media.poster_url,
                imdb_id=media.imdb_id,
                tmdb_id=media.tmdb_id,
                tvdb_id=media.tvdb_id,
                release_date=media.release_date.isoformat() if media.release_date else None,
                first_air_date=media.first_air_date.isoformat() if media.first_air_date else None,
                progress=progress,
            ).model_dump()
        )

    return {
        "items": items,
        "limit": limit,
        "offset": offset,
        "total": total,
    }


@router.delete(
    "/items/{watchlist_id}",
    summary="Remove watchlist item",
    description="Remove an item from the watchlist.",
)
async def remove_watchlist_item(
    watchlist_id: str,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    result = await db.execute(
        select(WatchlistItem).where(
            WatchlistItem.id == watchlist_id,
            WatchlistItem.user_id == current_user.id,
        )
    )
    item = result.scalars().first()
    if not item:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Watchlist item not found"
        )

    # We can hard delete or soft delete. The checklist says "Hard delete on remove; emit watch_events entries"
    # But for "filter - remove from watchlist when watched", we might want soft delete or just delete.
    # The checklist item "Hard delete on remove" implies DELETE endpoint does hard delete.

    await _log_watchlist_event(db, current_user.id, item.media_item_id, "watchlist_removed", {})
    await db.delete(item)
    await db.commit()
    return {"status": "deleted"}


# --- Helpers ---


async def _log_watchlist_event(
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


def _extract_media_ids(payload: WatchlistItemCreateIn) -> dict[str, str]:
    ids: dict[str, str] = {}
    if payload.imdb_id:
        ids["imdb_id"] = payload.imdb_id.strip().lower()
    if payload.tmdb_id:
        ids["tmdb_id"] = payload.tmdb_id.strip()
    if payload.tvdb_id:
        ids["tvdb_id"] = payload.tvdb_id.strip()
    if payload.tvmaze_id:
        ids["tvmaze_id"] = payload.tvmaze_id.strip()
    if payload.kitsu_id:
        ids["kitsu_id"] = payload.kitsu_id.strip()
    if payload.myanimelist_id:
        ids["myanimelist_id"] = payload.myanimelist_id.strip()
    if payload.anilist_id:
        ids["anilist_id"] = payload.anilist_id.strip()
    return ids


def _fallback_title(ids: dict[str, str]) -> str:
    for provider, val in ids.items():
        return f"{provider.upper()} {val}"
    return "Unknown title"


async def _find_media_item_by_ids(
    db: AsyncSession, media_type: str, ids: dict[str, str]
) -> MediaItem | None:
    # Simplified lookup
    clauses = []
    if ids.get("imdb_id"):
        clauses.append(MediaItem.imdb_id == ids["imdb_id"])
    if ids.get("tmdb_id"):
        clauses.append((MediaItem.tmdb_id == ids["tmdb_id"]) & (MediaItem.media_type == media_type))
    if ids.get("tvdb_id"):
        clauses.append((MediaItem.tvdb_id == ids["tvdb_id"]) & (MediaItem.media_type == media_type))

    if not clauses:
        return None

    # This is a bit loose, normally we check one by one to merge or avoid conflict
    # But for now let's just find the first match
    result = await db.execute(select(MediaItem).where(or_(*clauses)).limit(1))
    return result.scalars().first()


async def _get_show_progress(db: AsyncSession, user_id: str, media_item_id: str) -> dict:
    from librarysync.db.models import EpisodeItem, WatchedItem

    # Count released episodes
    now = datetime.now(timezone.utc).date()
    result = await db.execute(
        select(EpisodeItem.id).where(
            EpisodeItem.show_media_item_id == media_item_id,
            EpisodeItem.air_date != None,
            EpisodeItem.air_date <= now,
        )
    )
    released_ids = result.scalars().all()
    total_released = len(released_ids)

    if total_released == 0:
        return {"watched": 0, "total": 0}

    # Count watched among released
    result = await db.execute(
        select(func.count(func.distinct(WatchedItem.episode_item_id))).where(
            WatchedItem.user_id == user_id,
            WatchedItem.media_item_id == None,
            WatchedItem.episode_item_id.in_(released_ids),
        )
    )
    watched_count = result.scalar() or 0
    return {"watched": watched_count, "total": total_released}


def _apply_id_update(item: MediaItem, field: str, value: str | None) -> None:
    if not value:
        return
    current = getattr(item, field)
    if not current:
        setattr(item, field, value)
