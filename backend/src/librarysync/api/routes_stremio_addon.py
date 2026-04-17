from __future__ import annotations

import copy
import re
import secrets
from datetime import datetime, timezone
from typing import Literal

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm.attributes import flag_modified

from librarysync.api.deps import get_current_user, get_db
from librarysync.config import settings
from librarysync.connectors.services.letterboxd import LetterboxdError
from librarysync.core.catalog_ordering import CatalogOrderBy
from librarysync.core.external_catalog import (
    ExternalCatalogProviderError,
    discover_external_catalog_source,
    external_catalog_out,
    mark_external_catalog_refresh_failed,
    normalize_external_filters,
    normalize_external_manifest_url,
    normalize_external_order_by,
    normalize_external_order_dir,
    normalize_external_page_size,
    normalize_external_show_in_home,
    normalize_external_source_kind,
    normalize_external_source_provider,
    refresh_external_catalog,
)
from librarysync.core.stremio_addon import (
    ensure_addon_config,
    normalize_default_catalogs,
)
from librarysync.core.watchlist import (
    apply_media_id_update,
    fallback_title,
    find_media_item_by_ids,
    normalize_media_ids,
)
from librarysync.db.models import (
    MediaItem,
    StremioAddonConfig,
    StremioCustomCatalog,
    StremioCustomCatalogItem,
    StremioExternalCatalog,
    User,
)

router = APIRouter(prefix="/api/stremio-addon", tags=["stremio-addon"])

CUSTOM_MEDIA_TYPES = {"movie", "tv", "anime"}
CUSTOM_ORDER_BY = {
    "manual",
    "random",
    *CatalogOrderBy.__args__,
}
CUSTOM_ORDER_DIR = {"asc", "desc"}
EXTERNAL_CATALOG_TYPES = {"movie", "series"}


class StremioCatalogFilters(BaseModel):
    statuses: list[str] | None = None
    show_watched: bool | None = None


class StremioCatalogOrdering(BaseModel):
    order_by: str | None = None
    order_dir: Literal["asc", "desc"] | None = None


class StremioCatalogUpdate(BaseModel):
    id: str
    enabled: bool | None = None
    filters: StremioCatalogFilters | None = None
    ordering: StremioCatalogOrdering | None = None
    showInHome: bool | None = None


class StremioAddonConfigUpdate(BaseModel):
    is_enabled: bool | None = None
    catalogs: list[StremioCatalogUpdate] | None = None


class StremioCustomCatalogCreate(BaseModel):
    name: str
    media_type: Literal["movie", "tv", "anime"] = "movie"
    order_by: str | None = None
    order_dir: Literal["asc", "desc"] | None = None


class StremioCustomCatalogUpdate(BaseModel):
    name: str | None = None
    media_type: Literal["movie", "tv", "anime"] | None = None
    order_by: str | None = None
    order_dir: Literal["asc", "desc"] | None = None


class StremioCustomCatalogItemCreate(BaseModel):
    media_item_id: str | None = None
    media_type: Literal["movie", "tv", "anime"] | None = None
    title: str | None = None
    year: int | None = None
    poster_url: str | None = None
    imdb_id: str | None = None
    tmdb_id: str | None = None
    tvdb_id: str | None = None
    tvmaze_id: str | None = None
    kitsu_id: str | None = None
    myanimelist_id: str | None = None
    anilist_id: str | None = None
    stremio_id: str | None = None


class StremioCustomCatalogReorder(BaseModel):
    media_item_ids: list[str]


class StremioExternalCatalogDiscoverPayload(BaseModel):
    source_url: str | None = None
    manifest_url: str | None = None


class StremioExternalCatalogCreate(BaseModel):
    name: str
    manifest_url: str | None = None
    source_url: str | None = None
    source_kind: Literal["manifest", "list"] | None = None
    source_provider: Literal["tmdb", "tvdb", "letterboxd", "mdblist"] | None = None
    addon_name: str | None = None
    source_catalog_id: str
    source_catalog_type: Literal["movie", "series"]
    media_type: Literal["movie", "tv", "anime"] | None = None
    enabled: bool | None = None
    filters: StremioCatalogFilters | None = None
    order_by: str | None = None
    order_dir: Literal["asc", "desc"] | None = None
    page_size: int | None = None
    show_in_home: bool | None = None


class StremioExternalCatalogUpdate(BaseModel):
    name: str | None = None
    enabled: bool | None = None
    filters: StremioCatalogFilters | None = None
    order_by: str | None = None
    order_dir: Literal["asc", "desc"] | None = None
    page_size: int | None = None
    show_in_home: bool | None = None


def _resolve_base_url(request: Request) -> str:
    base = settings.base_url or str(request.base_url)
    return base.rstrip("/")


def _build_manifest_links(base_url: str, addon_id: str) -> dict[str, str]:
    manifest_url = f"{base_url}/stremio-addon/{addon_id}/manifest.json"
    # Strip protocol (http:// or https://) for Stremio install URL
    manifest_path = manifest_url.split("://", 1)[-1]
    install_url = f"stremio://{manifest_path}"
    return {"manifest_url": manifest_url, "install_url": install_url}


def _slugify(value: str) -> str:
    normalized = re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")
    return normalized or "catalog"


def _truncate_slug(value: str, suffix: str | None = None) -> str:
    suffix_len = len(suffix) if suffix else 0
    trimmed = value[: 64 - suffix_len].rstrip("-")
    return f"{trimmed}{suffix}" if suffix else trimmed


async def _build_unique_slug(
    db: AsyncSession,
    user_id: str,
    value: str,
    *,
    exclude_custom_catalog_id: str | None = None,
    exclude_external_catalog_id: str | None = None,
) -> str:
    base_slug = _truncate_slug(_slugify(value))
    custom_result = await db.execute(
        select(StremioCustomCatalog.id, StremioCustomCatalog.slug).where(
            StremioCustomCatalog.user_id == user_id
        )
    )
    external_result = await db.execute(
        select(StremioExternalCatalog.id, StremioExternalCatalog.slug).where(
            StremioExternalCatalog.user_id == user_id
        )
    )
    existing = {
        slug
        for catalog_id, slug in custom_result.all()
        if slug and (not exclude_custom_catalog_id or catalog_id != exclude_custom_catalog_id)
    }
    existing.update(
        {
            slug
            for catalog_id, slug in external_result.all()
            if slug
            and (not exclude_external_catalog_id or catalog_id != exclude_external_catalog_id)
        }
    )
    if base_slug not in existing:
        return base_slug
    for index in range(2, 100):
        candidate = _truncate_slug(base_slug, f"-{index}")
        if candidate not in existing:
            return candidate
    return _truncate_slug(base_slug, f"-{secrets.token_hex(3)}")


def _normalize_custom_order_by(value: str | None) -> str:
    if not value:
        return "manual"
    normalized = value.strip().lower()
    return normalized if normalized in CUSTOM_ORDER_BY else "manual"


def _normalize_custom_order_dir(value: str | None) -> str:
    normalized = value.strip().lower() if value else ""
    return normalized if normalized in CUSTOM_ORDER_DIR else "asc"


def _custom_catalog_out(catalog: StremioCustomCatalog) -> dict:
    return {
        "id": catalog.id,
        "name": catalog.name,
        "slug": catalog.slug,
        "media_type": catalog.media_type,
        "order_by": catalog.order_by,
        "order_dir": catalog.order_dir,
        "created_at": catalog.created_at,
        "updated_at": catalog.updated_at,
    }


def _custom_catalog_item_out(
    item: StremioCustomCatalogItem, media_item: MediaItem
) -> dict[str, object]:
    return {
        "media_item_id": media_item.id,
        "position": item.position,
        "created_at": item.created_at,
        "media_type": media_item.media_type,
        "title": media_item.title,
        "year": media_item.year,
        "poster_url": media_item.poster_url,
        "imdb_id": media_item.imdb_id,
        "tmdb_id": media_item.tmdb_id,
        "tvdb_id": media_item.tvdb_id,
        "tvmaze_id": media_item.tvmaze_id,
        "kitsu_id": media_item.kitsu_id,
        "myanimelist_id": media_item.myanimelist_id,
        "anilist_id": media_item.anilist_id,
    }


def _build_reorder_map(
    existing_ids: list[str],
    requested_ids: list[str],
) -> dict[str, int]:
    if not existing_ids:
        if requested_ids:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Catalog has no items to reorder",
            )
        return {}
    if not requested_ids:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Provide media_item_ids to reorder",
        )
    if len(set(requested_ids)) != len(requested_ids):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Duplicate media_item_ids provided",
        )
    existing_set = set(existing_ids)
    requested_set = set(requested_ids)
    if existing_set != requested_set:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Provide all catalog items to reorder",
        )
    return {media_item_id: index for index, media_item_id in enumerate(requested_ids)}


def _merge_catalog_updates(
    existing: list[dict],
    updates: list[StremioCatalogUpdate],
) -> list[dict]:
    catalog_copy = [copy.deepcopy(catalog) for catalog in existing]
    by_id = {catalog.get("id"): catalog for catalog in catalog_copy if catalog.get("id")}
    for update in updates:
        catalog = by_id.get(update.id)
        if not catalog:
            continue
        if update.enabled is not None:
            catalog["enabled"] = bool(update.enabled)
        if update.filters is not None:
            catalog["filters"] = update.filters.model_dump(exclude_none=True)
        if update.ordering is not None:
            catalog["ordering"] = update.ordering.model_dump(exclude_none=True)
        if update.showInHome is not None:
            catalog["showInHome"] = bool(update.showInHome)
    return list(by_id.values())


async def _ensure_default_catalogs(
    db: AsyncSession,
    config: StremioAddonConfig,
) -> list[dict]:
    catalogs = normalize_default_catalogs(config.default_catalogs)
    if config.default_catalogs != catalogs:
        config.default_catalogs = catalogs
        db.add(config)
        await db.commit()
        await db.refresh(config)
    return catalogs


async def _load_custom_catalog(
    db: AsyncSession,
    user_id: str,
    catalog_id: str,
) -> StremioCustomCatalog:
    result = await db.execute(
        select(StremioCustomCatalog).where(
            StremioCustomCatalog.user_id == user_id,
            StremioCustomCatalog.id == catalog_id,
        )
    )
    catalog = result.scalars().first()
    if not catalog:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Catalog not found")
    return catalog


async def _load_external_catalog(
    db: AsyncSession,
    user_id: str,
    catalog_id: str,
) -> StremioExternalCatalog:
    result = await db.execute(
        select(StremioExternalCatalog).where(
            StremioExternalCatalog.user_id == user_id,
            StremioExternalCatalog.id == catalog_id,
        )
    )
    catalog = result.scalars().first()
    if not catalog:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Catalog not found")
    return catalog


async def _resolve_custom_media_item(
    db: AsyncSession,
    payload: StremioCustomCatalogItemCreate,
    catalog: StremioCustomCatalog,
) -> MediaItem:
    if payload.media_item_id:
        media_item = await db.get(MediaItem, payload.media_item_id)
        if not media_item:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail="Media item not found"
            )
        if media_item.media_type != catalog.media_type:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Media type does not match catalog",
            )
        return media_item

    media_type = payload.media_type or catalog.media_type
    if media_type not in CUSTOM_MEDIA_TYPES:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid media type",
        )
    if media_type != catalog.media_type:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Media type does not match catalog",
        )
    ids = normalize_media_ids(
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
    if not ids:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Provide a media_item_id or at least one external ID",
        )

    media_item = await find_media_item_by_ids(db, media_type, ids)
    if media_item and media_item.media_type != media_type:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Media type does not match existing item",
        )

    if not media_item:
        raw = {"source": "stremio_custom_catalog", "ids": ids}
        if payload.stremio_id:
            raw["stremio_id"] = payload.stremio_id

        media_item = MediaItem(
            media_type=media_type,
            title=payload.title or fallback_title(ids),
            year=payload.year,
            poster_url=payload.poster_url,
            imdb_id=ids.get("imdb_id"),
            tmdb_id=ids.get("tmdb_id"),
            tvdb_id=ids.get("tvdb_id"),
            tvmaze_id=ids.get("tvmaze_id"),
            kitsu_id=ids.get("kitsu_id"),
            myanimelist_id=ids.get("myanimelist_id"),
            anilist_id=ids.get("anilist_id"),
            raw=raw,
        )
        db.add(media_item)
        await db.flush()
        return media_item

    for id_field in [
        "imdb_id",
        "tmdb_id",
        "tvdb_id",
        "tvmaze_id",
        "kitsu_id",
        "myanimelist_id",
        "anilist_id",
    ]:
        apply_media_id_update(media_item, id_field, ids.get(id_field))
    if payload.year is not None and media_item.year is None:
        media_item.year = payload.year
    if payload.poster_url and not media_item.poster_url:
        media_item.poster_url = payload.poster_url
    if payload.stremio_id:
        raw = media_item.raw if isinstance(media_item.raw, dict) else {}
        if not raw.get("stremio_id"):
            raw["stremio_id"] = payload.stremio_id
            media_item.raw = raw
    return media_item


def _normalize_external_media_type(
    source_catalog_type: str,
    requested_media_type: str | None,
) -> str:
    if source_catalog_type == "movie":
        return "movie"
    normalized = (requested_media_type or "tv").strip().lower()
    return normalized if normalized in {"tv", "anime"} else "tv"


async def _refresh_external_catalog_or_502(
    db: AsyncSession,
    catalog: StremioExternalCatalog,
) -> int:
    try:
        count = await refresh_external_catalog(db, catalog)
        await db.commit()
        await db.refresh(catalog)
        return count
    except (httpx.HTTPError, LetterboxdError, ExternalCatalogProviderError) as exc:
        await db.rollback()
        await mark_external_catalog_refresh_failed(db, catalog, exc)
        await db.commit()
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="External catalog refresh failed",
        ) from exc
    except ValueError as exc:
        await db.rollback()
        await mark_external_catalog_refresh_failed(db, catalog, exc)
        await db.commit()
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(exc) or "Invalid external catalog configuration",
        ) from exc
    except Exception as exc:
        await db.rollback()
        await mark_external_catalog_refresh_failed(db, catalog, exc)
        await db.commit()
        raise


@router.get(
    "/config",
    summary="Get Stremio addon config",
    description="Return the Stremio addon config and install links.",
)
async def get_stremio_addon_config(
    request: Request,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    config = await ensure_addon_config(db, current_user.id)
    catalogs = await _ensure_default_catalogs(db, config)

    custom_result = await db.execute(
        select(StremioCustomCatalog)
        .where(StremioCustomCatalog.user_id == current_user.id)
        .order_by(StremioCustomCatalog.created_at.asc())
    )
    custom_catalogs = [_custom_catalog_out(catalog) for catalog in custom_result.scalars().all()]
    external_result = await db.execute(
        select(StremioExternalCatalog)
        .where(StremioExternalCatalog.user_id == current_user.id)
        .order_by(StremioExternalCatalog.created_at.asc())
    )
    external_catalogs = [
        external_catalog_out(catalog) for catalog in external_result.scalars().all()
    ]

    return {
        "addon_id": config.id,
        "is_enabled": bool(config.is_enabled),
        "catalogs": catalogs,
        "external_catalogs": external_catalogs,
        "custom_catalogs": custom_catalogs,
        **_build_manifest_links(_resolve_base_url(request), config.id),
    }


@router.post(
    "/config",
    summary="Update Stremio addon config",
    description="Update Stremio addon settings and catalog filters/order.",
)
async def update_stremio_addon_config(
    payload: StremioAddonConfigUpdate,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    config = await ensure_addon_config(db, current_user.id)
    catalogs = await _ensure_default_catalogs(db, config)

    if "is_enabled" in payload.model_fields_set:
        config.is_enabled = bool(payload.is_enabled)

    if payload.catalogs:
        catalogs = _merge_catalog_updates(catalogs, payload.catalogs)
        config.default_catalogs = catalogs
        flag_modified(config, "default_catalogs")

    config.updated_at = datetime.now(timezone.utc)
    db.add(config)
    await db.commit()
    await db.refresh(config)

    return {
        "is_enabled": config.is_enabled,
        "catalogs": config.default_catalogs,
    }


@router.post(
    "/external-catalogs/discover",
    summary="Discover external catalogs",
    description="Fetch an external Stremio manifest and list supported catalogs.",
)
async def discover_stremio_external_catalogs(
    payload: StremioExternalCatalogDiscoverPayload,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    source_url = (payload.source_url or payload.manifest_url or "").strip()
    if not source_url:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Source URL is required",
        )
    try:
        return await discover_external_catalog_source(db, current_user.id, source_url)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(exc),
        ) from exc
    except (httpx.HTTPError, LetterboxdError, ExternalCatalogProviderError) as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Failed to fetch external source",
        ) from exc


@router.get(
    "/external-catalogs",
    summary="List external catalogs",
    description="List external Stremio catalogs configured for the current user.",
)
async def list_external_catalogs(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    result = await db.execute(
        select(StremioExternalCatalog)
        .where(StremioExternalCatalog.user_id == current_user.id)
        .order_by(StremioExternalCatalog.created_at.asc())
    )
    return {
        "external_catalogs": [external_catalog_out(catalog) for catalog in result.scalars().all()]
    }


@router.post(
    "/external-catalogs",
    status_code=status.HTTP_201_CREATED,
    summary="Create external catalog",
    description="Create a new external Stremio catalog backed by another addon.",
)
async def create_external_catalog(
    payload: StremioExternalCatalogCreate,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    name = payload.name.strip()
    if not name:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Name is required")
    source_kind = normalize_external_source_kind(payload.source_kind)
    source_provider = normalize_external_source_provider(payload.source_provider)
    source_url = (payload.source_url or payload.manifest_url or "").strip()
    try:
        manifest_url = (
            await normalize_external_manifest_url(source_url)
            if source_kind == "manifest"
            else source_url
        )
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    if not manifest_url:
        detail = "Manifest URL is required" if source_kind == "manifest" else "Source URL is required"
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=detail)
    source_catalog_id = payload.source_catalog_id.strip()
    if not source_catalog_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Source catalog ID is required",
        )
    if payload.source_catalog_type not in EXTERNAL_CATALOG_TYPES:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid source catalog type",
        )
    if source_kind == "list" and not source_provider:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="List provider is required",
        )

    catalog = StremioExternalCatalog(
        user_id=current_user.id,
        name=name,
        slug=await _build_unique_slug(db, current_user.id, name),
        source_kind=source_kind,
        source_provider=source_provider,
        addon_name=(payload.addon_name or "").strip() or None,
        manifest_url=manifest_url,
        source_catalog_id=source_catalog_id,
        source_catalog_type=payload.source_catalog_type,
        media_type=_normalize_external_media_type(
            payload.source_catalog_type,
            payload.media_type,
        ),
        enabled=True if payload.enabled is None else bool(payload.enabled),
        filters=normalize_external_filters(
            payload.filters.model_dump(exclude_none=True) if payload.filters else None
        ),
        order_by=normalize_external_order_by(payload.order_by),
        order_dir=normalize_external_order_dir(payload.order_dir),
        page_size=normalize_external_page_size(payload.page_size),
        show_in_home=normalize_external_show_in_home(payload.show_in_home),
    )
    db.add(catalog)
    try:
        await db.flush()
        await refresh_external_catalog(db, catalog)
        await db.commit()
        await db.refresh(catalog)
    except (httpx.HTTPError, LetterboxdError, ExternalCatalogProviderError) as exc:
        await db.rollback()
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="External catalog refresh failed",
        ) from exc
    except ValueError as exc:
        await db.rollback()
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(exc),
        ) from exc
    except Exception:
        await db.rollback()
        raise
    return external_catalog_out(catalog)


@router.patch(
    "/external-catalogs/{catalog_id}",
    summary="Update external catalog",
    description="Update an external Stremio catalog.",
)
async def update_external_catalog(
    catalog_id: str,
    payload: StremioExternalCatalogUpdate,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    catalog = await _load_external_catalog(db, current_user.id, catalog_id)
    fields = payload.model_fields_set

    if "name" in fields:
        name = (payload.name or "").strip()
        if not name:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Name is required")
        if name != catalog.name:
            catalog.slug = await _build_unique_slug(
                db,
                current_user.id,
                name,
                exclude_external_catalog_id=catalog.id,
            )
        catalog.name = name
    if "enabled" in fields:
        if payload.enabled is None:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Enabled must be true or false",
            )
        catalog.enabled = payload.enabled
    if "filters" in fields:
        catalog.filters = normalize_external_filters(
            payload.filters.model_dump(exclude_none=True) if payload.filters else None
        )
    if "order_by" in fields:
        catalog.order_by = normalize_external_order_by(payload.order_by)
    if "order_dir" in fields:
        catalog.order_dir = normalize_external_order_dir(payload.order_dir)
    if "page_size" in fields:
        catalog.page_size = normalize_external_page_size(payload.page_size)
    if "show_in_home" in fields:
        catalog.show_in_home = normalize_external_show_in_home(payload.show_in_home)

    catalog.updated_at = datetime.now(timezone.utc)
    db.add(catalog)
    await db.commit()
    await db.refresh(catalog)
    return external_catalog_out(catalog)


@router.delete(
    "/external-catalogs/{catalog_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Delete external catalog",
    description="Delete an external Stremio catalog and its cached items.",
)
async def delete_external_catalog(
    catalog_id: str,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    catalog = await _load_external_catalog(db, current_user.id, catalog_id)
    await db.delete(catalog)
    await db.commit()


@router.post(
    "/external-catalogs/{catalog_id}/refresh",
    summary="Refresh external catalog",
    description="Fetch the latest items for an external Stremio catalog.",
)
async def refresh_stremio_external_catalog(
    catalog_id: str,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    catalog = await _load_external_catalog(db, current_user.id, catalog_id)
    item_count = await _refresh_external_catalog_or_502(db, catalog)
    return {
        "status": "ok",
        "item_count": item_count,
        "catalog": external_catalog_out(catalog),
    }


@router.post(
    "/custom-catalogs",
    status_code=status.HTTP_201_CREATED,
    summary="Create custom catalog",
    description="Create a new custom Stremio catalog.",
)
async def create_custom_catalog(
    payload: StremioCustomCatalogCreate,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    name = payload.name.strip()
    if not name:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Name is required")

    catalog = StremioCustomCatalog(
        user_id=current_user.id,
        name=name,
        slug=await _build_unique_slug(db, current_user.id, name),
        media_type=payload.media_type,
        order_by=_normalize_custom_order_by(payload.order_by),
        order_dir=_normalize_custom_order_dir(payload.order_dir),
    )
    db.add(catalog)
    await db.commit()
    await db.refresh(catalog)
    return _custom_catalog_out(catalog)


@router.patch(
    "/custom-catalogs/{catalog_id}",
    summary="Update custom catalog",
    description="Update a custom Stremio catalog.",
)
async def update_custom_catalog(
    catalog_id: str,
    payload: StremioCustomCatalogUpdate,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    catalog = await _load_custom_catalog(db, current_user.id, catalog_id)
    fields = payload.model_fields_set

    if "name" in fields:
        name = (payload.name or "").strip()
        if not name:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Name is required")
        if name != catalog.name:
            catalog.slug = await _build_unique_slug(
                db, current_user.id, name, exclude_custom_catalog_id=catalog.id
            )
        catalog.name = name

    if "media_type" in fields and payload.media_type:
        if payload.media_type not in CUSTOM_MEDIA_TYPES:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid media type"
            )
        if payload.media_type != catalog.media_type:
            item_count_result = await db.execute(
                select(func.count(StremioCustomCatalogItem.id)).where(
                    StremioCustomCatalogItem.catalog_id == catalog.id
                )
            )
            if int(item_count_result.scalar_one() or 0) > 0:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail="Cannot change media type while the catalog still has items",
                )
        catalog.media_type = payload.media_type

    if "order_by" in fields:
        catalog.order_by = _normalize_custom_order_by(payload.order_by)
    if "order_dir" in fields:
        catalog.order_dir = _normalize_custom_order_dir(payload.order_dir)

    catalog.updated_at = datetime.now(timezone.utc)
    db.add(catalog)
    await db.commit()
    await db.refresh(catalog)
    return _custom_catalog_out(catalog)


@router.delete(
    "/custom-catalogs/{catalog_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Delete custom catalog",
    description="Delete a custom Stremio catalog.",
)
async def delete_custom_catalog(
    catalog_id: str,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    catalog = await _load_custom_catalog(db, current_user.id, catalog_id)
    await db.delete(catalog)
    await db.commit()


@router.get(
    "/custom-catalogs/{catalog_id}/items",
    summary="List custom catalog items",
    description="List items in a custom Stremio catalog.",
)
async def list_custom_catalog_items(
    catalog_id: str,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    catalog = await _load_custom_catalog(db, current_user.id, catalog_id)
    result = await db.execute(
        select(StremioCustomCatalogItem, MediaItem)
        .join(MediaItem, MediaItem.id == StremioCustomCatalogItem.media_item_id)
        .where(StremioCustomCatalogItem.catalog_id == catalog.id)
        .order_by(
            StremioCustomCatalogItem.position.asc(),
            StremioCustomCatalogItem.created_at.asc(),
        )
    )
    items = [_custom_catalog_item_out(item, media) for item, media in result.all()]
    return {"items": items}


@router.post(
    "/custom-catalogs/{catalog_id}/items",
    status_code=status.HTTP_201_CREATED,
    summary="Add custom catalog item",
    description="Add a media item to a custom Stremio catalog.",
)
async def add_custom_catalog_item(
    catalog_id: str,
    payload: StremioCustomCatalogItemCreate,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    catalog = await _load_custom_catalog(db, current_user.id, catalog_id)
    media_item = await _resolve_custom_media_item(db, payload, catalog)
    existing_result = await db.execute(
        select(StremioCustomCatalogItem).where(
            StremioCustomCatalogItem.catalog_id == catalog.id,
            StremioCustomCatalogItem.media_item_id == media_item.id,
        )
    )
    if existing_result.scalars().first():
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Media item already in catalog",
        )
    position_result = await db.execute(
        select(func.coalesce(func.max(StremioCustomCatalogItem.position), 0)).where(
            StremioCustomCatalogItem.catalog_id == catalog.id
        )
    )
    next_position = int(position_result.scalar_one() or 0) + 1
    item = StremioCustomCatalogItem(
        catalog_id=catalog.id,
        media_item_id=media_item.id,
        position=next_position,
    )
    db.add(item)
    await db.commit()
    await db.refresh(item)
    return {"item": _custom_catalog_item_out(item, media_item)}


@router.delete(
    "/custom-catalogs/{catalog_id}/items/{media_item_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Remove custom catalog item",
    description="Remove a media item from a custom Stremio catalog.",
)
async def remove_custom_catalog_item(
    catalog_id: str,
    media_item_id: str,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    catalog = await _load_custom_catalog(db, current_user.id, catalog_id)
    result = await db.execute(
        select(StremioCustomCatalogItem).where(
            StremioCustomCatalogItem.catalog_id == catalog.id,
            StremioCustomCatalogItem.media_item_id == media_item_id,
        )
    )
    item = result.scalars().first()
    if not item:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Item not found")
    await db.delete(item)
    await db.commit()


@router.post(
    "/custom-catalogs/{catalog_id}/reorder",
    summary="Reorder custom catalog items",
    description="Reorder items in a custom Stremio catalog.",
)
async def reorder_custom_catalog(
    catalog_id: str,
    payload: StremioCustomCatalogReorder,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    catalog = await _load_custom_catalog(db, current_user.id, catalog_id)
    result = await db.execute(
        select(StremioCustomCatalogItem).where(StremioCustomCatalogItem.catalog_id == catalog.id)
    )
    items = result.scalars().all()
    existing_ids = [item.media_item_id for item in items]
    reorder_map = _build_reorder_map(existing_ids, payload.media_item_ids)
    if not reorder_map:
        return {"status": "ok"}
    for item in items:
        item.position = reorder_map.get(item.media_item_id, item.position)
    await db.commit()
    return {"status": "ok"}
