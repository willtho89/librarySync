import re
from datetime import datetime, timezone
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel
from sqlalchemy import and_, delete, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from librarysync.api.deps import get_current_user, get_db
from librarysync.core.ratings import normalize_star_rating
from librarysync.core.watch_pipeline import (
    SYNC_COORDINATOR,
    SYNC_STRATEGY_REGISTRY,
    enqueue_new_item_job,
)
from librarysync.db.models import (
    EpisodeItem,
    MediaItem,
    User,
    WatchedItem,
    WatchEvent,
    WatchSync,
)

router = APIRouter(prefix="/api/history", tags=["history"])


class HistoryItemIds(BaseModel):
    imdb_id: str | None = None
    tmdb_id: str | None = None
    tvdb_id: str | None = None
    tvmaze_id: str | None = None
    kitsu_id: str | None = None
    myanimelist_id: str | None = None
    anilist_id: str | None = None


class HistoryEpisodeIds(BaseModel):
    imdb_id: str | None = None
    tmdb_id: str | None = None
    tvdb_id: str | None = None
    tvmaze_id: str | None = None


class HistoryItemMetadata(BaseModel):
    media_item_id: str | None = None
    episode_item_id: str | None = None
    ids: HistoryItemIds
    episode_ids: HistoryEpisodeIds | None = None
    media_created_at: datetime | None = None
    media_updated_at: datetime | None = None
    episode_created_at: datetime | None = None
    episode_updated_at: datetime | None = None
    watched_created_at: datetime | None = None
    first_sync_at: datetime | None = None
    last_sync_at: datetime | None = None


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
    anilist_id: str | None
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
    simkl_status: str | None = None
    simkl_external_id: str | None = None
    simkl_last_error: str | None = None
    stremio_status: str | None = None
    stremio_external_id: str | None = None
    stremio_last_error: str | None = None
    anilist_status: str | None = None
    anilist_external_id: str | None = None
    anilist_last_error: str | None = None
    metadata: HistoryItemMetadata | None = None


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
    anilist_id: str | None = None
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


class WatchedItemBulkDeleteIn(BaseModel):
    watched_ids: list[str]
    delete_integrations: bool = False


class WatchedItemSyncIn(BaseModel):
    provider: str
    watched_ids: list[str] | None = None


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
    await enqueue_new_item_job(
        db,
        current_user.id,
        watched.id,
        is_rewatch=is_rewatch,
        source="manual",
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
    offset: int = Query(0, ge=0),
    search: str | None = Query(None, max_length=200),
    media_type: Literal["movie", "tv", "anime"] | None = Query(None),
    source: str | None = Query(None, max_length=32),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    show_item = aliased(MediaItem)
    letterboxd_sync = aliased(WatchSync)
    trakt_sync = aliased(WatchSync)
    simkl_sync = aliased(WatchSync)
    stremio_sync = aliased(WatchSync)
    anilist_sync = aliased(WatchSync)
    filters = [WatchedItem.user_id == current_user.id]
    if media_type:
        filters.append(
            or_(MediaItem.media_type == media_type, show_item.media_type == media_type)
        )
    if source:
        normalized_source = source.strip().lower()
        if normalized_source:
            if normalized_source == "manual":
                source_values = ("manual", "api")
            elif normalized_source in {"trakt", "simkl", "stremio", "letterboxd"}:
                source_values = (normalized_source, f"{normalized_source}_import")
            else:
                source_values = (normalized_source,)
            filters.append(WatchedItem.source.in_(source_values))
    if search:
        normalized_search = search.strip()
    else:
        normalized_search = ""
    if normalized_search:
        like_value = f"%{normalized_search}%"
        search_clauses = [
            MediaItem.title.ilike(like_value),
            show_item.title.ilike(like_value),
            EpisodeItem.title.ilike(like_value),
            MediaItem.imdb_id.ilike(like_value),
            MediaItem.tmdb_id.ilike(like_value),
            MediaItem.tvdb_id.ilike(like_value),
            MediaItem.tvmaze_id.ilike(like_value),
            MediaItem.kitsu_id.ilike(like_value),
            MediaItem.myanimelist_id.ilike(like_value),
            MediaItem.anilist_id.ilike(like_value),
            EpisodeItem.imdb_id.ilike(like_value),
            EpisodeItem.tmdb_id.ilike(like_value),
            EpisodeItem.tvdb_id.ilike(like_value),
            EpisodeItem.tvmaze_id.ilike(like_value),
        ]
        if normalized_search.isdigit() and len(normalized_search) == 4:
            year_value = int(normalized_search)
            search_clauses.extend(
                [MediaItem.year == year_value, show_item.year == year_value]
            )
        filters.append(or_(*search_clauses))

    total_result = await db.execute(
        select(func.count(WatchedItem.id))
        .select_from(WatchedItem)
        .outerjoin(MediaItem, WatchedItem.media_item_id == MediaItem.id)
        .outerjoin(EpisodeItem, WatchedItem.episode_item_id == EpisodeItem.id)
        .outerjoin(show_item, EpisodeItem.show_media_item_id == show_item.id)
        .where(*filters)
    )
    total = int(total_result.scalar() or 0)

    result = await db.execute(
        select(
            WatchedItem,
            MediaItem,
            EpisodeItem,
            show_item,
            letterboxd_sync,
            trakt_sync,
            simkl_sync,
            stremio_sync,
            anilist_sync,
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
        .outerjoin(
            simkl_sync,
            and_(
                simkl_sync.watched_item_id == WatchedItem.id,
                simkl_sync.provider == "simkl",
            ),
        )
        .outerjoin(
            stremio_sync,
            and_(
                stremio_sync.watched_item_id == WatchedItem.id,
                stremio_sync.provider == "stremio",
            ),
        )
        .outerjoin(
            anilist_sync,
            and_(
                anilist_sync.watched_item_id == WatchedItem.id,
                anilist_sync.provider == "anilist",
            ),
        )
        .where(*filters)
        .order_by(WatchedItem.watched_at.desc())
        .offset(offset)
        .limit(limit)
    )
    items = []
    for watched, media_item, episode_item, show, sync, trakt, simkl, stremio, anilist in result.all():
        base_item = media_item or show
        if not base_item:
            continue
        sync_entries = [entry for entry in (sync, trakt, simkl, stremio, anilist) if entry]
        first_sync_at = (
            min((entry.created_at for entry in sync_entries), default=None)
            if sync_entries
            else None
        )
        last_sync_at = None
        if sync_entries:
            last_sync_at = max(
                (
                    entry.last_synced_at or entry.updated_at or entry.created_at
                    for entry in sync_entries
                ),
                default=None,
            )
        metadata = HistoryItemMetadata(
            media_item_id=base_item.id,
            episode_item_id=episode_item.id if episode_item else None,
            ids=HistoryItemIds(
                imdb_id=base_item.imdb_id,
                tmdb_id=base_item.tmdb_id,
                tvdb_id=base_item.tvdb_id,
                tvmaze_id=base_item.tvmaze_id,
                kitsu_id=base_item.kitsu_id,
                myanimelist_id=base_item.myanimelist_id,
                anilist_id=base_item.anilist_id,
            ),
            episode_ids=HistoryEpisodeIds(
                imdb_id=episode_item.imdb_id if episode_item else None,
                tmdb_id=episode_item.tmdb_id if episode_item else None,
                tvdb_id=episode_item.tvdb_id if episode_item else None,
                tvmaze_id=episode_item.tvmaze_id if episode_item else None,
            )
            if episode_item
            else None,
            media_created_at=base_item.created_at,
            media_updated_at=base_item.updated_at,
            episode_created_at=episode_item.created_at if episode_item else None,
            episode_updated_at=episode_item.updated_at if episode_item else None,
            watched_created_at=watched.created_at,
            first_sync_at=first_sync_at,
            last_sync_at=last_sync_at,
        )
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
                anilist_id=base_item.anilist_id,
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
                simkl_status=simkl.status if simkl else None,
                simkl_external_id=simkl.external_id if simkl else None,
                simkl_last_error=simkl.last_error if simkl else None,
                stremio_status=stremio.status if stremio else None,
                stremio_external_id=stremio.external_id if stremio else None,
                stremio_last_error=stremio.last_error if stremio else None,
                anilist_status=anilist.status if anilist else None,
                anilist_external_id=anilist.external_id if anilist else None,
                anilist_last_error=anilist.last_error if anilist else None,
                metadata=metadata,
            ).model_dump()
        )
    return {
        "items": _merge_history_items(items),
        "limit": limit,
        "offset": offset,
        "total": total,
    }


@router.post(
    "/items/sync",
    summary="Sync watched items",
    description="Queue watched items to sync with a connected integration.",
)
async def sync_watched_items(
    payload: WatchedItemSyncIn,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    provider = payload.provider.strip().lower()
    allowed = {strategy.provider for strategy in SYNC_STRATEGY_REGISTRY.list()}
    if provider not in allowed:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Unknown sync provider",
        )
    strategy = SYNC_STRATEGY_REGISTRY.get(provider)
    if not strategy:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Unknown sync provider",
        )
    watched_ids = [
        watched_id.strip()
        for watched_id in (payload.watched_ids or [])
        if watched_id and watched_id.strip()
    ]
    unique_ids = list(dict.fromkeys(watched_ids))
    show_item = aliased(MediaItem)
    query = (
        select(WatchedItem, MediaItem, EpisodeItem, show_item)
        .outerjoin(MediaItem, WatchedItem.media_item_id == MediaItem.id)
        .outerjoin(EpisodeItem, WatchedItem.episode_item_id == EpisodeItem.id)
        .outerjoin(show_item, EpisodeItem.show_media_item_id == show_item.id)
        .where(WatchedItem.user_id == current_user.id)
    )
    if unique_ids:
        query = query.where(WatchedItem.id.in_(unique_ids))
    result = await db.execute(query)
    rows = result.all()
    requested = 0
    for watched, media_item, episode_item, show in rows:
        resolved_media = media_item if watched.media_item_id else show
        resolved_episode = None if watched.media_item_id else episode_item
        if not resolved_media and not resolved_episode:
            continue
        await strategy.enqueue_new(
            db,
            watched,
            resolved_media,
            resolved_episode,
            is_rewatch=False,
            force=True,
        )
        requested += 1
    await db.commit()
    return {"requested": requested, "provider": provider}


@router.delete(
    "/items",
    summary="Clear watched history",
    description="Remove all watched entries for the current user.",
)
async def clear_watched_items(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    result = await db.execute(
        select(
            WatchedItem.id,
            WatchedItem.media_item_id,
            WatchedItem.episode_item_id,
            WatchedItem.watched_at,
        ).where(WatchedItem.user_id == current_user.id)
    )
    rows = result.all()
    if not rows:
        return {"deleted": 0}

    now = datetime.now(timezone.utc)
    events = [
        WatchEvent(
            user_id=current_user.id,
            media_item_id=row.media_item_id,
            episode_item_id=row.episode_item_id,
            event_type="manual_watched_deleted",
            occurred_at=now,
            raw={
                "watched_id": row.id,
                "previous_watched_at": row.watched_at.isoformat()
                if row.watched_at
                else None,
                "bulk_clear": True,
            },
        )
        for row in rows
    ]
    db.add_all(events)
    await db.execute(delete(WatchedItem).where(WatchedItem.user_id == current_user.id))
    await db.execute(
        delete(WatchEvent).where(
            WatchEvent.user_id == current_user.id,
            WatchEvent.event_type.in_(
                (
                    "trakt_imported",
                    "letterboxd_imported",
                    "simkl_imported",
                    "stremio_imported",
                )
            ),
        )
    )
    await db.commit()
    return {"deleted": len(rows)}


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
        if media_item or episode_item:
            await _enqueue_update_syncs(
                db,
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
    delete_integrations: bool = Query(
        False, description="Also delete the item from connected integrations."
    ),
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

    media_item: MediaItem | None = None
    episode_item: EpisodeItem | None = None
    show_item: MediaItem | None = None
    if delete_integrations:
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

    event_raw: dict[str, object] = {
        "watched_id": watched.id,
        "previous_watched_at": watched.watched_at.isoformat()
        if watched.watched_at
        else None,
    }
    if delete_integrations:
        event_raw["delete_integrations"] = True

    event = WatchEvent(
        user_id=current_user.id,
        media_item_id=watched.media_item_id,
        episode_item_id=watched.episode_item_id,
        event_type="manual_watched_deleted",
        occurred_at=datetime.now(timezone.utc),
        raw=event_raw,
    )
    db.add(event)
    if delete_integrations:
        target_media = media_item or show_item
        if target_media:
            await _enqueue_delete_syncs(
                db,
                watched,
                target_media,
                episode_item,
            )
    await db.delete(watched)
    await db.commit()
    return {"status": "deleted"}


@router.post(
    "/items/bulk-delete",
    summary="Delete multiple watched items",
    description="Remove selected watched entries from history.",
)
async def bulk_delete_watched_items(
    payload: WatchedItemBulkDeleteIn,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    watched_ids = [watched_id.strip() for watched_id in payload.watched_ids if watched_id]
    if not watched_ids:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Provide at least one watched ID",
        )
    unique_ids = list(dict.fromkeys(watched_ids))
    show_item = aliased(MediaItem)
    result = await db.execute(
        select(WatchedItem, MediaItem, EpisodeItem, show_item)
        .outerjoin(MediaItem, WatchedItem.media_item_id == MediaItem.id)
        .outerjoin(EpisodeItem, WatchedItem.episode_item_id == EpisodeItem.id)
        .outerjoin(show_item, EpisodeItem.show_media_item_id == show_item.id)
        .where(
            WatchedItem.user_id == current_user.id,
            WatchedItem.id.in_(unique_ids),
        )
    )
    rows = result.all()
    if not rows:
        return {"deleted": 0}

    now = datetime.now(timezone.utc)
    events: list[WatchEvent] = []
    delete_ids: list[str] = []

    for watched, media_item, episode_item, show in rows:
        delete_ids.append(watched.id)
        event_raw: dict[str, object] = {
            "watched_id": watched.id,
            "previous_watched_at": watched.watched_at.isoformat()
            if watched.watched_at
            else None,
            "bulk_delete": True,
        }
        if payload.delete_integrations:
            event_raw["delete_integrations"] = True
        events.append(
            WatchEvent(
                user_id=current_user.id,
                media_item_id=watched.media_item_id,
                episode_item_id=watched.episode_item_id,
                event_type="manual_watched_deleted",
                occurred_at=now,
                raw=event_raw,
            )
        )
        if payload.delete_integrations:
            target_media = media_item or show
            if target_media:
                await _enqueue_delete_syncs(
                    db,
                    watched,
                    target_media,
                    episode_item,
                )

    db.add_all(events)
    await db.execute(
        delete(WatchedItem).where(
            WatchedItem.user_id == current_user.id,
            WatchedItem.id.in_(delete_ids),
        )
    )
    await db.commit()
    return {"deleted": len(delete_ids)}


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
    anilist_id = _normalize_id(payload.anilist_id)
    if anilist_id:
        ids["anilist_id"] = anilist_id
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
    if ids.get("anilist_id"):
        return f"AniList {ids['anilist_id']}"
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
        "anilist_id",
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
        "simkl_status",
        "simkl_external_id",
        "simkl_last_error",
        "stremio_status",
        "stremio_external_id",
        "stremio_last_error",
        "anilist_status",
        "anilist_external_id",
        "anilist_last_error",
    )


def _metadata_fields() -> tuple[str, ...]:
    return (
        "imdb_id",
        "tmdb_id",
        "tvdb_id",
        "tvmaze_id",
        "kitsu_id",
        "myanimelist_id",
        "anilist_id",
        "poster_url",
        "season_number",
        "episode_number",
        "episode_title",
        "episode_imdb_id",
        "episode_tmdb_id",
        "episode_tvdb_id",
        "episode_tvmaze_id",
        "metadata",
    )


async def _enqueue_update_syncs(
    db: AsyncSession,
    watched: WatchedItem,
    media_item: MediaItem | None,
    episode_item: EpisodeItem | None,
    watched_at_updated: bool,
    rating_updated: bool,
) -> None:
    await SYNC_COORDINATOR.enqueue_update_all(
        db,
        watched,
        media_item,
        episode_item,
        watched_at_updated,
        rating_updated,
    )


async def _enqueue_delete_syncs(
    db: AsyncSession,
    watched: WatchedItem,
    media_item: MediaItem | None,
    episode_item: EpisodeItem | None,
) -> None:
    await SYNC_COORDINATOR.enqueue_delete_all(
        db,
        watched,
        media_item,
        episode_item,
    )


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
    if ids.get("anilist_id"):
        result = await db.execute(
            select(MediaItem).where(
                MediaItem.anilist_id == ids["anilist_id"],
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
