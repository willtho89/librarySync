from datetime import datetime, timezone
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from librarysync.api.deps import get_current_user, get_db
from librarysync.core.watchlist_links import (
    parse_letterboxd_list_urls,
    parse_trakt_list_urls,
)
from librarysync.core.watchlist import (
    log_watchlist_event,
    normalize_media_ids,
    upsert_watchlist_item,
)
from librarysync.core.watchlist_sync import (
    enqueue_personal_watchlist_removal,
    enqueue_personal_watchlist_sync,
)
from librarysync.core.watchlist_sources import (
    LEGACY_LIST_SOURCE_TYPE,
    URL_SOURCE_TYPE,
    ensure_manual_watchlist_source,
    ensure_personal_watchlist_source,
    ensure_watchlist_source,
    list_watchlist_sources,
    remove_watchlist_source,
    upsert_watchlist_source_item,
)
from librarysync.db.models import (
    Integration,
    MediaItem,
    User,
    WatchedItem,
    WatchlistItem,
    WatchlistSource,
    WatchlistSourceItem,
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


class WatchlistItemSourceOut(BaseModel):
    id: str
    provider: str
    source_type: str
    name: str | None
    url: str | None
    is_enabled: bool


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
    sources: list[WatchlistItemSourceOut] = []


class WatchlistSourceCreateIn(BaseModel):
    url: str


class WatchlistSourceUpdateIn(BaseModel):
    is_enabled: bool


class WatchlistSourceOut(BaseModel):
    id: str
    provider: str
    source_type: str
    url: str | None
    name: str | None
    is_enabled: bool
    is_deletable: bool
    last_synced_at: datetime | None


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
    now = datetime.now(timezone.utc)
    media_ids = normalize_media_ids(
        {
            "imdb_id": payload.imdb_id,
            "tmdb_id": payload.tmdb_id,
            "tvdb_id": payload.tvdb_id,
            "tvmaze_id": payload.tvmaze_id,
            "kitsu_id": payload.kitsu_id,
            "myanimelist_id": payload.myanimelist_id,
            "anilist_id": payload.anilist_id,
        }
    )
    if not media_ids:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Provide at least one external ID",
        )
    watchlist_item, status_value = await upsert_watchlist_item(
        db,
        current_user.id,
        payload.media_type,
        media_ids,
        payload.title,
        payload.year,
        payload.poster_url,
        "manual",
        now=now,
        event_raw={},
    )
    if status_value == "conflict":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Media type does not match existing item",
        )
    if not watchlist_item:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Unable to add watchlist item",
        )
    if status_value in {"created", "restored"}:
        await db.commit()
        await db.refresh(watchlist_item)
    if watchlist_item:
        source = await ensure_manual_watchlist_source(db, current_user.id)
        await upsert_watchlist_source_item(
            db,
            source,
            watchlist_item,
            external_item_id=None,
            now=now,
        )
        media_result = await db.execute(
            select(MediaItem).where(MediaItem.id == watchlist_item.media_item_id)
        )
        media_item = media_result.scalars().first()
        await enqueue_personal_watchlist_sync(db, watchlist_item, media_item)
        await db.commit()
    return {"id": watchlist_item.id, "status": status_value}


@router.get(
    "/sources",
    summary="List watchlist sources",
    description="Return the current user's configured watchlist sources.",
)
async def list_watchlist_sources_route(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    integrations_result = await db.execute(
        select(Integration).where(
            Integration.user_id == current_user.id,
            Integration.provider.in_(["trakt", "letterboxd"]),
            Integration.status != "disconnected",
        )
    )
    for integration in integrations_result.scalars().all():
        if integration.provider == "trakt":
            await ensure_personal_watchlist_source(
                db,
                user_id=current_user.id,
                provider="trakt",
                name="Trakt watchlist",
            )
        elif integration.provider == "letterboxd":
            await ensure_personal_watchlist_source(
                db,
                user_id=current_user.id,
                provider="letterboxd",
                name="Letterboxd watchlist",
            )
    sources = await list_watchlist_sources(
        db, current_user.id, include_disabled=True
    )
    items = [
        WatchlistSourceOut(
            id=source.id,
            provider=source.provider,
            source_type=source.source_type,
            url=source.url,
            name=source.name,
            is_enabled=source.is_enabled,
            is_deletable=source.source_type in {URL_SOURCE_TYPE, LEGACY_LIST_SOURCE_TYPE},
            last_synced_at=source.last_synced_at,
        ).model_dump()
        for source in sources
    ]
    return {"sources": items}


@router.post(
    "/sources",
    status_code=status.HTTP_201_CREATED,
    summary="Add watchlist source",
    description="Add an external watchlist source by URL.",
)
async def add_watchlist_source(
    payload: WatchlistSourceCreateIn,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    url = payload.url.strip()
    trakt_refs = parse_trakt_list_urls([url])
    letterboxd_refs = parse_letterboxd_list_urls([url]) if not trakt_refs else []
    ref = trakt_refs[0] if trakt_refs else (letterboxd_refs[0] if letterboxd_refs else None)
    if not ref:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Unsupported watchlist URL",
        )
    provider = "trakt" if trakt_refs else "letterboxd"
    integration_result = await db.execute(
        select(Integration).where(
            Integration.user_id == current_user.id,
            Integration.provider == provider,
            Integration.status != "disconnected",
        )
    )
    if not integration_result.scalars().first():
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Connect the integration before adding watchlists",
        )
    source = await ensure_watchlist_source(
        db,
        user_id=current_user.id,
        provider=provider,
        source_type=URL_SOURCE_TYPE,
        external_id=ref.external_id,
        url=ref.url,
        name=ref.name,
        is_enabled=True,
    )
    await db.commit()
    return WatchlistSourceOut(
        id=source.id,
        provider=source.provider,
        source_type=source.source_type,
        url=source.url,
        name=source.name,
        is_enabled=source.is_enabled,
        is_deletable=source.source_type in {URL_SOURCE_TYPE, LEGACY_LIST_SOURCE_TYPE},
        last_synced_at=source.last_synced_at,
    ).model_dump()


@router.patch(
    "/sources/{source_id}",
    summary="Update watchlist source",
    description="Enable or disable a watchlist source.",
)
async def update_watchlist_source(
    source_id: str,
    payload: WatchlistSourceUpdateIn,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    result = await db.execute(
        select(WatchlistSource).where(
            WatchlistSource.id == source_id,
            WatchlistSource.user_id == current_user.id,
        )
    )
    source = result.scalars().first()
    if not source:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Watchlist source not found"
        )
    source.is_enabled = bool(payload.is_enabled)
    source.updated_at = datetime.now(timezone.utc)
    db.add(source)
    await db.commit()
    return {
        "id": source.id,
        "is_enabled": source.is_enabled,
    }


@router.delete(
    "/sources/{source_id}",
    summary="Delete watchlist source",
    description="Delete a watchlist source.",
)
async def delete_watchlist_source(
    source_id: str,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    result = await db.execute(
        select(WatchlistSource).where(
            WatchlistSource.id == source_id,
            WatchlistSource.user_id == current_user.id,
        )
    )
    source = result.scalars().first()
    if not source:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Watchlist source not found"
        )
    if source.source_type not in {URL_SOURCE_TYPE, LEGACY_LIST_SOURCE_TYPE}:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="This watchlist source cannot be deleted",
        )
    removed_count = await remove_watchlist_source(db, source)
    return {"status": "deleted", "removed": removed_count}


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
    rows = result.all()
    source_map: dict[str, list[WatchlistItemSourceOut]] = {}
    item_ids = [item.id for item, _ in rows]
    if item_ids:
        source_result = await db.execute(
            select(WatchlistSourceItem.watchlist_item_id, WatchlistSource)
            .join(WatchlistSource, WatchlistSource.id == WatchlistSourceItem.source_id)
            .where(
                WatchlistSourceItem.watchlist_item_id.in_(item_ids),
                WatchlistSourceItem.user_id == current_user.id,
            )
            .order_by(WatchlistSource.provider, WatchlistSource.name)
        )
        for watchlist_item_id, source in source_result.all():
            source_map.setdefault(watchlist_item_id, []).append(
                WatchlistItemSourceOut(
                    id=source.id,
                    provider=source.provider,
                    source_type=source.source_type,
                    name=source.name,
                    url=source.url,
                    is_enabled=source.is_enabled,
                )
            )

    items = []
    status_changed = False
    for item, media in rows:
        progress = None
        if media.media_type == "tv":
            # For v1, this N+1 query is acceptable for small page size (25-100)
            # In future, use group by subquery or CTE
            progress = await _get_show_progress(db, current_user.id, media.id)
            if progress and progress["total"] > 0 and item.status != "removed":
                desired_status = (
                    "waiting" if progress["watched"] >= progress["total"] else "active"
                )
                if item.status != desired_status:
                    item.status = desired_status
                    item.updated_at = datetime.now(timezone.utc)
                    await log_watchlist_event(
                        db,
                        current_user.id,
                        media.id,
                        "watchlist_status_changed",
                        {"status": desired_status, "reason": "auto_evaluation"},
                    )
                    status_changed = True

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
                sources=[source.model_dump() for source in source_map.get(item.id, [])],
            ).model_dump()
        )

    if status_changed:
        await db.commit()

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

    await log_watchlist_event(
        db, current_user.id, item.media_item_id, "watchlist_removed", {}
    )
    media_item = None
    if item.media_item_id:
        media_result = await db.execute(
            select(MediaItem).where(MediaItem.id == item.media_item_id)
        )
        media_item = media_result.scalars().first()
    if media_item:
        await enqueue_personal_watchlist_removal(db, item, media_item)
    await db.delete(item)
    await db.commit()
    return {"status": "deleted"}


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
