import logging
from datetime import datetime, timezone
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel
from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from librarysync.api.deps import get_current_user, get_db
from librarysync.connectors.services.letterboxd import LetterboxdError
from librarysync.connectors.services.simkl import SimklError
from librarysync.connectors.services.trakt import TraktError
from librarysync.core.catalog_ordering import (
    CatalogOrderBy,
    CatalogOrderDirection,
    apply_catalog_ordering,
)
from librarysync.core.publicmetadb import is_publicmetadb_sync_enabled
from librarysync.core.watch_pipeline import enqueue_new_item_job
from librarysync.core.watchlist import (
    apply_combined_status_filter,
    apply_watchlist_status_change,
    clear_watchlist_rewatch_request,
    determine_movie_watchlist_status,
    determine_show_watchlist_status,
    log_watchlist_event,
    normalize_media_ids,
    normalize_watchlist_statuses,
    set_watchlist_rewatch_request,
    upsert_watchlist_item,
)
from librarysync.core.watchlist_links import (
    parse_letterboxd_list_urls,
    parse_trakt_list_urls,
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
from librarysync.core.watchlist_sync import (
    enqueue_personal_watchlist_removal,
    enqueue_personal_watchlist_sync,
)
from librarysync.db.models import (
    EpisodeItem,
    Integration,
    MediaItem,
    User,
    WatchedItem,
    WatchEvent,
    WatchlistItem,
    WatchlistSource,
    WatchlistSourceItem,
)
from librarysync.jobs.letterboxd_import import (
    import_watchlist_source as import_letterboxd_watchlist_source,
)
from librarysync.jobs.simkl_import import (
    import_watchlist_source as import_simkl_watchlist_source,
)
from librarysync.jobs.trakt_import import (
    import_watchlist_source as import_trakt_watchlist_source,
)

logger = logging.getLogger(__name__)
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
    tvmaze_id: str | None = None
    kitsu_id: str | None = None
    myanimelist_id: str | None = None
    anilist_id: str | None = None
    release_date: str | None = None
    first_air_date: str | None = None
    overview: str | None = None
    genres: list[str] | None = None
    runtime_in_seconds: int | None = None
    progress: dict | None = None
    rewatch_requested: bool = False
    rewatch_requested_at: datetime | None = None
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
            Integration.provider.in_(["trakt", "simkl", "letterboxd", "publicmetadb"]),
            Integration.status != "disconnected",
        )
    )
    integrations = integrations_result.scalars().all()
    for integration in integrations:
        if integration.provider == "trakt":
            await ensure_personal_watchlist_source(
                db,
                user_id=current_user.id,
                provider="trakt",
                name="Trakt watchlist",
            )
        elif integration.provider == "simkl":
            await ensure_personal_watchlist_source(
                db,
                user_id=current_user.id,
                provider="simkl",
                name="SIMKL watchlist",
            )
        elif integration.provider == "letterboxd":
            await ensure_personal_watchlist_source(
                db,
                user_id=current_user.id,
                provider="letterboxd",
                name="Letterboxd watchlist",
            )
        elif integration.provider == "publicmetadb" and is_publicmetadb_sync_enabled(
            dict(integration.config or {})
        ):
            await ensure_personal_watchlist_source(
                db,
                user_id=current_user.id,
                provider="publicmetadb",
                name="PublicMetaDB watchlist",
            )
    if integrations:
        await db.commit()
    sources = await list_watchlist_sources(db, current_user.id, include_disabled=True)
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
    existing_result = await db.execute(
        select(WatchlistSource).where(
            WatchlistSource.user_id == current_user.id,
            WatchlistSource.provider == provider,
            WatchlistSource.source_type == URL_SOURCE_TYPE,
            WatchlistSource.external_id == ref.external_id,
        )
    )
    existing_source = existing_result.scalars().first()
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
    sync_error = None
    imported = 0
    if existing_source is None:
        try:
            imported = await _sync_watchlist_source(db, source)
        except HTTPException as exc:
            sync_error = exc.detail
        except Exception:
            logger.exception("Watchlist sync failed for source %s", source.id)
            sync_error = "Watchlist sync failed"
    payload = WatchlistSourceOut(
        id=source.id,
        provider=source.provider,
        source_type=source.source_type,
        url=source.url,
        name=source.name,
        is_enabled=source.is_enabled,
        is_deletable=source.source_type in {URL_SOURCE_TYPE, LEGACY_LIST_SOURCE_TYPE},
        last_synced_at=source.last_synced_at,
    ).model_dump()
    return payload | {"imported": imported, "sync_error": sync_error}


async def _sync_watchlist_source(db: AsyncSession, source: WatchlistSource) -> int:
    if not source.is_enabled:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Watchlist source is disabled",
        )
    if source.provider == "trakt":
        try:
            return await import_trakt_watchlist_source(db, source)
        except ValueError as exc:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=str(exc),
            ) from exc
        except TraktError as exc:
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail=str(exc),
            ) from exc
    if source.provider == "letterboxd":
        try:
            return await import_letterboxd_watchlist_source(db, source)
        except ValueError as exc:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=str(exc),
            ) from exc
        except LetterboxdError as exc:
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail=str(exc),
            ) from exc
    if source.provider == "simkl":
        try:
            return await import_simkl_watchlist_source(db, source)
        except ValueError as exc:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=str(exc),
            ) from exc
        except SimklError as exc:
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail=str(exc),
            ) from exc
    raise HTTPException(
        status_code=status.HTTP_400_BAD_REQUEST,
        detail="Unsupported watchlist provider",
    )


@router.post(
    "/sources/{source_id}/sync",
    summary="Sync watchlist source",
    description="Sync a watchlist source immediately.",
)
async def sync_watchlist_source(
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
    imported = await _sync_watchlist_source(db, source)
    return {"status": "synced", "imported": imported}


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
    status: str | None = Query("all", description="Comma-separated list of statuses"),
    media_type: Literal["movie", "tv", "anime"] | None = Query(None),
    search: str | None = Query(None, max_length=200),
    source: str | None = Query(None, max_length=32),
    rewatch: Literal["all", "only", "exclude"] = Query(
        "all", description="Filter rewatch-queued items"
    ),
    order_by: CatalogOrderBy = Query(
        "date_added",
        description="Order by: date_added, release_date, last_watched, episodes_left, "
        "progress, last_episode_air_date, next_episode_air_date",
    ),
    order_dir: CatalogOrderDirection = Query("desc", description="Order direction"),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    normalized_statuses: list[str] = []
    status_filter_values: list[str] = []
    if status and status != "all":
        raw_statuses = [s.strip() for s in status.split(",") if s.strip()]
        normalized_statuses = normalize_watchlist_statuses(raw_statuses)
        status_filter_values = list(dict.fromkeys([*raw_statuses, *normalized_statuses]))
        if "not_released" in normalized_statuses:
            await _refresh_watchlist_statuses_for_filter(
                db,
                current_user.id,
                media_type=media_type,
            )

    query = (
        select(WatchlistItem, MediaItem)
        .join(MediaItem, WatchlistItem.media_item_id == MediaItem.id)
        .where(WatchlistItem.user_id == current_user.id)
    )

    if status and status != "all" and status_filter_values:
        filter_now_date = datetime.now(timezone.utc).date()
        query = apply_combined_status_filter(
            query,
            user_id=current_user.id,
            now_date=filter_now_date,
            normalized_statuses=normalized_statuses,
            status_filter_values=status_filter_values,
            media_type=media_type,
        )

    if media_type:
        query = query.where(WatchlistItem.type == media_type)

    if rewatch == "only":
        query = query.where(WatchlistItem.rewatch_requested.is_(True))
    elif rewatch == "exclude":
        query = query.where(WatchlistItem.rewatch_requested.is_(False))

    if source:
        normalized_source = source.strip().lower()
        if normalized_source == "manual":
            query = query.where(WatchlistItem.source == "manual")
        elif normalized_source:
            source_match = (
                select(1)
                .select_from(WatchlistSourceItem)
                .join(
                    WatchlistSource,
                    WatchlistSourceItem.source_id == WatchlistSource.id,
                )
                .where(
                    WatchlistSourceItem.watchlist_item_id == WatchlistItem.id,
                    WatchlistSourceItem.user_id == current_user.id,
                    WatchlistSource.provider == normalized_source,
                )
            )
            query = query.where(source_match.exists())

    if search:
        normalized_search = search.strip()
        like_value = f"%{normalized_search}%"
        search_clauses = [
            MediaItem.title.ilike(like_value),
            MediaItem.imdb_id.ilike(like_value),
            MediaItem.tmdb_id.ilike(like_value),
            MediaItem.tvdb_id.ilike(like_value),
            MediaItem.tvmaze_id.ilike(like_value),
            MediaItem.kitsu_id.ilike(like_value),
            MediaItem.myanimelist_id.ilike(like_value),
            MediaItem.anilist_id.ilike(like_value),
        ]
        if normalized_search.isdigit() and len(normalized_search) == 4:
            year_value = int(normalized_search)
            search_clauses.append(MediaItem.year == year_value)
        query = query.where(or_(*search_clauses))

    total_result = await db.execute(select(func.count()).select_from(query.subquery()))
    total = int(total_result.scalar() or 0)

    release_date_expr = func.coalesce(MediaItem.release_date, MediaItem.first_air_date)
    query = apply_catalog_ordering(
        query,
        order_by=order_by,
        order_dir=order_dir,
        user_id=current_user.id,
        date_added_col=WatchlistItem.created_at,
        release_date_col=release_date_expr,
        base_media_id_col=MediaItem.id,
        tie_breaker_col=WatchlistItem.id,
    )
    query = query.offset(offset).limit(limit)
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

    now_date = datetime.now(timezone.utc).date()
    tv_media_ids = [media.id for _, media in rows if media.media_type == "tv"]
    progress_map = await _get_show_progress_bulk(db, current_user.id, tv_media_ids)
    items = []
    status_changed = False
    for item, media in rows:
        progress = None
        desired_status = item.status
        if media.media_type == "tv":
            progress = progress_map.get(
                media.id, {"watched": 0, "total": 0, "earliest_air_date": None}
            )
            desired_status = determine_show_watchlist_status(
                total_released=progress["total"],
                watched_count=progress["watched"],
                first_air_date=media.first_air_date,
                earliest_air_date=progress.get("earliest_air_date"),
                now_date=now_date,
            )
        elif media.media_type == "movie":
            has_watched = item.status in {"watched", "waiting"}
            desired_status = determine_movie_watchlist_status(
                media,
                has_watched=has_watched,
                now_date=now_date,
            )

        if item.status != "removed" and item.status != desired_status:
            if await apply_watchlist_status_change(
                db,
                item,
                current_user.id,
                media.id,
                desired_status,
                reason="auto_evaluation",
            ):
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
                tvmaze_id=media.tvmaze_id,
                kitsu_id=media.kitsu_id,
                myanimelist_id=media.myanimelist_id,
                anilist_id=media.anilist_id,
                release_date=media.release_date.isoformat() if media.release_date else None,
                first_air_date=media.first_air_date.isoformat() if media.first_air_date else None,
                overview=media.overview,
                genres=media.genres,
                runtime_in_seconds=media.runtime_in_seconds,
                progress=progress,
                rewatch_requested=item.rewatch_requested,
                rewatch_requested_at=item.rewatch_requested_at,
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

    await log_watchlist_event(db, current_user.id, item.media_item_id, "watchlist_removed", {})
    media_item = None
    if item.media_item_id:
        media_result = await db.execute(select(MediaItem).where(MediaItem.id == item.media_item_id))
        media_item = media_result.scalars().first()
    if media_item:
        await enqueue_personal_watchlist_removal(db, item, media_item)
    await db.delete(item)
    await db.commit()
    return {"status": "deleted"}


@router.post(
    "/items/{watchlist_id}/rewatch",
    summary="Enable watchlist rewatch",
    description="Force a watched item to stay in the watchlist until it is watched again.",
)
async def enable_watchlist_rewatch(
    watchlist_id: str,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    result = await db.execute(
        select(WatchlistItem, MediaItem)
        .join(MediaItem, WatchlistItem.media_item_id == MediaItem.id)
        .where(
            WatchlistItem.id == watchlist_id,
            WatchlistItem.user_id == current_user.id,
        )
    )
    row = result.first()
    if not row:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Watchlist item not found"
        )
    item, media_item = row
    if item.status == "removed":
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Removed watchlist items cannot be queued for rewatch",
        )

    if media_item.media_type == "movie":
        watched_result = await db.execute(
            select(WatchedItem.id)
            .where(
                WatchedItem.user_id == current_user.id,
                WatchedItem.media_item_id == media_item.id,
            )
            .limit(1)
        )
        is_eligible = watched_result.scalars().first() is not None
    else:
        progress = (await _get_show_progress_bulk(db, current_user.id, [media_item.id])).get(
            media_item.id,
            {"watched": 0, "total": 0, "earliest_air_date": None},
        )
        current_status = determine_show_watchlist_status(
            total_released=progress["total"],
            watched_count=progress["watched"],
            first_air_date=media_item.first_air_date,
            earliest_air_date=progress.get("earliest_air_date"),
            now_date=datetime.now(timezone.utc).date(),
        )
        is_eligible = current_status == "watched"

    if not is_eligible:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Only watched items can be queued for rewatch",
        )

    changed = await set_watchlist_rewatch_request(
        db,
        item,
        current_user.id,
        item.media_item_id,
        enabled=True,
        reason="manual",
    )
    await db.commit()
    return {
        "id": item.id,
        "rewatch_requested": item.rewatch_requested,
        "rewatch_requested_at": item.rewatch_requested_at,
        "status": "updated" if changed else "unchanged",
    }


@router.delete(
    "/items/{watchlist_id}/rewatch",
    summary="Disable watchlist rewatch",
    description="Remove the rewatch override from a watchlist item.",
)
async def disable_watchlist_rewatch(
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
    changed = await clear_watchlist_rewatch_request(
        db,
        item,
        current_user.id,
        item.media_item_id,
        reason="manual",
    )
    await db.commit()
    return {
        "id": item.id,
        "rewatch_requested": item.rewatch_requested,
        "rewatch_requested_at": item.rewatch_requested_at,
        "status": "updated" if changed else "unchanged",
    }


@router.post(
    "/items/{watchlist_id}/mark-watched",
    summary="Mark watchlist item as watched",
    description="Mark a movie as watched or the next episode of a show as watched.",
)
async def mark_watchlist_item_watched(
    watchlist_id: str,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    # 1. Fetch watchlist item
    result = await db.execute(
        select(WatchlistItem, MediaItem)
        .join(MediaItem, WatchlistItem.media_item_id == MediaItem.id)
        .where(
            WatchlistItem.id == watchlist_id,
            WatchlistItem.user_id == current_user.id,
        )
    )
    row = result.first()
    if not row:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Watchlist item not found"
        )
    watchlist_item, media_item = row

    # 2. Determine target (Movie or Next Episode)
    target_media = media_item
    target_episode: EpisodeItem | None = None

    if media_item.media_type == "tv":
        # Logic to find next episode:
        # a) Get all released episodes
        now_date = datetime.now(timezone.utc).date()
        episodes_result = await db.execute(
            select(EpisodeItem)
            .where(
                EpisodeItem.show_media_item_id == media_item.id,
                EpisodeItem.air_date is not None,
                EpisodeItem.air_date <= now_date,
                EpisodeItem.season_number > 0,  # Exclude specials (season 0)
            )
            .order_by(EpisodeItem.season_number, EpisodeItem.episode_number)
        )
        released_episodes = episodes_result.scalars().all()
        if not released_episodes:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="No released episodes found for this show",
            )

        # b) Get watched episodes
        # Use a JOIN to properly filter watched episodes for this show
        watched_result = await db.execute(
            select(WatchedItem.episode_item_id)
            .join(EpisodeItem, WatchedItem.episode_item_id == EpisodeItem.id)
            .where(
                WatchedItem.user_id == current_user.id,
                WatchedItem.media_item_id.is_(None),
                EpisodeItem.show_media_item_id == media_item.id,
                EpisodeItem.air_date.is_not(None),
                EpisodeItem.air_date <= now_date,
                EpisodeItem.season_number > 0,
            )
        )
        watched_episode_ids = set(watched_result.scalars().all())

        # c) Find first unwatched
        for ep in released_episodes:
            if ep.id not in watched_episode_ids:
                target_episode = ep
                break

        if not target_episode:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="All released episodes are already watched",
            )
        target_media = None  # For shows, we set media to None in WatchedItem
    # 3. Create WatchedItem
    # Check if rewatch (only for movies, shows handle individual eps)
    is_rewatch = False
    if media_item.media_type == "movie":
        check_rewatch = await db.execute(
            select(WatchedItem.id)
            .where(
                WatchedItem.user_id == current_user.id,
                WatchedItem.media_item_id == media_item.id,
            )
            .limit(1)
        )
        is_rewatch = check_rewatch.scalars().first() is not None

    watched_at = datetime.now(timezone.utc)
    watched = WatchedItem(
        user_id=current_user.id,
        media_item_id=target_media.id if target_media else None,
        episode_item_id=target_episode.id if target_episode else None,
        watched_at=watched_at,
        source="manual",
    )

    event_raw = {
        "source": "manual",
        "watchlist_id": watchlist_item.id,
        "from_watchlist": True,
    }
    if is_rewatch:
        event_raw["rewatch"] = True
    if target_episode:
        event_raw["episode"] = {
            "season_number": target_episode.season_number,
            "episode_number": target_episode.episode_number,
            "title": target_episode.title,
            "id": target_episode.id,
        }

    event = WatchEvent(
        user_id=current_user.id,
        media_item_id=target_media.id if target_media else None,
        episode_item_id=target_episode.id if target_episode else None,
        event_type="manual_watched",
        occurred_at=watched_at,
        raw=event_raw,
    )

    db.add_all([watched, event])
    await db.flush()

    await clear_watchlist_rewatch_request(
        db,
        watchlist_item,
        current_user.id,
        watchlist_item.media_item_id,
        reason="watched",
        now=watched_at,
        watched_at=watched_at,
    )

    # 4. Enqueue Jobs
    await enqueue_new_item_job(
        db,
        current_user.id,
        watched.id,
        is_rewatch=is_rewatch,
        source="manual",
    )

    # 5. Update Watchlist Status (Handled by background/hooks usually, but we can trigger check)
    # `enqueue_new_item_job` calls `process_new_item_job` which calls `check_and_update_watchlist`.
    # So the status update should happen asynchronously.
    # However, for immediate UI feedback, we might want to return the updated status?
    # Or just return success.

    await db.commit()

    return {
        "watched_id": watched.id,
        "media_type": media_item.media_type,
        "added_episode": f"S{target_episode.season_number}E{target_episode.episode_number}"
        if target_episode
        else None,
    }


async def _get_show_progress_bulk(
    db: AsyncSession, user_id: str, media_item_ids: list[str]
) -> dict[str, dict]:
    if not media_item_ids:
        return {}

    now = datetime.now(timezone.utc).date()
    released_subq = (
        select(
            EpisodeItem.show_media_item_id.label("media_item_id"),
            func.count(EpisodeItem.id).label("total_released"),
        )
        .where(
            EpisodeItem.show_media_item_id.in_(media_item_ids),
            EpisodeItem.air_date.is_not(None),
            EpisodeItem.air_date <= now,
            EpisodeItem.season_number > 0,
        )
        .group_by(EpisodeItem.show_media_item_id)
        .subquery()
    )
    earliest_subq = (
        select(
            EpisodeItem.show_media_item_id.label("media_item_id"),
            func.min(EpisodeItem.air_date).label("earliest_air_date"),
        )
        .where(
            EpisodeItem.show_media_item_id.in_(media_item_ids),
            EpisodeItem.season_number > 0,
        )
        .group_by(EpisodeItem.show_media_item_id)
        .subquery()
    )
    watched_subq = (
        select(
            EpisodeItem.show_media_item_id.label("media_item_id"),
            func.count(func.distinct(WatchedItem.episode_item_id)).label("watched_count"),
        )
        .join(WatchedItem, WatchedItem.episode_item_id == EpisodeItem.id)
        .where(
            EpisodeItem.show_media_item_id.in_(media_item_ids),
            EpisodeItem.air_date.is_not(None),
            EpisodeItem.air_date <= now,
            EpisodeItem.season_number > 0,
            WatchedItem.user_id == user_id,
            WatchedItem.media_item_id.is_(None),
        )
        .group_by(EpisodeItem.show_media_item_id)
        .subquery()
    )
    base = (
        select(MediaItem.id.label("media_item_id"))
        .where(MediaItem.id.in_(media_item_ids))
        .subquery()
    )
    result = await db.execute(
        select(
            base.c.media_item_id,
            func.coalesce(released_subq.c.total_released, 0).label("total_released"),
            earliest_subq.c.earliest_air_date,
            func.coalesce(watched_subq.c.watched_count, 0).label("watched_count"),
        )
        .select_from(base)
        .outerjoin(released_subq, released_subq.c.media_item_id == base.c.media_item_id)
        .outerjoin(earliest_subq, earliest_subq.c.media_item_id == base.c.media_item_id)
        .outerjoin(watched_subq, watched_subq.c.media_item_id == base.c.media_item_id)
    )
    progress_map: dict[str, dict] = {}
    for row in result.all():
        progress_map[row.media_item_id] = {
            "watched": int(row.watched_count or 0),
            "total": int(row.total_released or 0),
            "earliest_air_date": row.earliest_air_date,
        }
    return progress_map


async def _refresh_watchlist_statuses_for_filter(
    db: AsyncSession,
    user_id: str,
    *,
    media_type: str | None,
) -> None:
    query = (
        select(WatchlistItem, MediaItem)
        .join(MediaItem, WatchlistItem.media_item_id == MediaItem.id)
        .where(
            WatchlistItem.user_id == user_id,
            WatchlistItem.status.in_(["added", "active", "hidden", "not_released"]),
        )
    )
    if media_type:
        query = query.where(WatchlistItem.type == media_type)

    rows = (await db.execute(query)).all()
    if not rows:
        return

    now_date = datetime.now(timezone.utc).date()
    tv_media_ids = [media.id for _, media in rows if media.media_type == "tv"]
    progress_map = await _get_show_progress_bulk(db, user_id, tv_media_ids)
    status_changed = False
    for item, media in rows:
        if item.status == "removed":
            continue
        desired_status = item.status
        if media.media_type == "tv":
            progress = progress_map.get(
                media.id, {"watched": 0, "total": 0, "earliest_air_date": None}
            )
            desired_status = determine_show_watchlist_status(
                total_released=progress["total"],
                watched_count=progress["watched"],
                first_air_date=media.first_air_date,
                earliest_air_date=progress.get("earliest_air_date"),
                now_date=now_date,
            )
        elif media.media_type == "movie":
            has_watched = item.status == "watched"
            desired_status = determine_movie_watchlist_status(
                media,
                has_watched=has_watched,
                now_date=now_date,
            )

        if item.status != desired_status:
            if await apply_watchlist_status_change(
                db,
                item,
                user_id,
                media.id,
                desired_status,
                reason="auto_evaluation",
            ):
                status_changed = True

    if status_changed:
        await db.commit()
