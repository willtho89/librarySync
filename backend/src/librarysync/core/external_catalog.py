from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import delete, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from librarysync.config import settings
from librarysync.core.catalog_ordering import CatalogOrderBy
from librarysync.core.http_client import get_http_client
from librarysync.db.models import MediaItem, StremioExternalCatalog, StremioExternalCatalogItem

EXTERNAL_CATALOG_ORDER_BY = {"source", "random", *CatalogOrderBy.__args__}
EXTERNAL_CATALOG_ORDER_DIR = {"asc", "desc"}
EXTERNAL_CATALOG_DEFAULT_PAGE_SIZE = 30
EXTERNAL_CATALOG_DEFAULT_SHOW_IN_HOME = True
EXTERNAL_CATALOG_DEFAULT_FILTERS = {"show_watched": False, "statuses": []}
EXTERNAL_CATALOG_FETCH_PAGE_SIZE = 100


def normalize_external_manifest_url(value: str) -> str:
    manifest_url = (value or "").strip()
    if not manifest_url:
        return ""
    if manifest_url.endswith("/manifest.json"):
        return manifest_url
    return f"{manifest_url.rstrip('/')}/manifest.json"


def normalize_external_filters(filters: dict[str, Any] | None) -> dict[str, Any]:
    base = filters if isinstance(filters, dict) else {}
    statuses = base.get("statuses") if isinstance(base.get("statuses"), list) else []
    show_watched = base.get("show_watched") if isinstance(base.get("show_watched"), bool) else False
    return {"statuses": [str(value) for value in statuses if value], "show_watched": show_watched}


def normalize_external_order_by(value: str | None) -> str:
    normalized = value.strip().lower() if value else "source"
    return normalized if normalized in EXTERNAL_CATALOG_ORDER_BY else "source"


def normalize_external_order_dir(value: str | None) -> str:
    normalized = value.strip().lower() if value else "asc"
    return normalized if normalized in EXTERNAL_CATALOG_ORDER_DIR else "asc"


def normalize_external_page_size(value: int | None) -> int:
    if isinstance(value, int) and value > 0:
        return min(value, 100)
    return EXTERNAL_CATALOG_DEFAULT_PAGE_SIZE


def normalize_external_show_in_home(value: bool | None) -> bool:
    return value if isinstance(value, bool) else EXTERNAL_CATALOG_DEFAULT_SHOW_IN_HOME


def external_catalog_out(catalog: StremioExternalCatalog) -> dict[str, Any]:
    return {
        "id": catalog.id,
        "name": catalog.name,
        "slug": catalog.slug,
        "addon_name": catalog.addon_name,
        "manifest_url": catalog.manifest_url,
        "source_catalog_id": catalog.source_catalog_id,
        "source_catalog_type": catalog.source_catalog_type,
        "media_type": catalog.media_type,
        "enabled": catalog.enabled,
        "filters": normalize_external_filters(catalog.filters),
        "order_by": catalog.order_by,
        "order_dir": catalog.order_dir,
        "page_size": catalog.page_size,
        "show_in_home": catalog.show_in_home,
        "last_refreshed_at": catalog.last_refreshed_at,
        "last_refresh_error": catalog.last_refresh_error,
        "created_at": catalog.created_at,
        "updated_at": catalog.updated_at,
    }


def external_catalog_manifest_entry(catalog: StremioExternalCatalog) -> dict[str, Any]:
    return {
        "type": catalog.source_catalog_type,
        "id": catalog.slug,
        "name": catalog.name,
        "extraSupported": ["search", "skip", "limit"],
        "pageSize": normalize_external_page_size(catalog.page_size),
        "showInHome": normalize_external_show_in_home(catalog.show_in_home),
    }


def stremio_type_to_media_types(stremio_type: str) -> tuple[str, ...]:
    if stremio_type == "movie":
        return ("movie",)
    if stremio_type == "series":
        return ("tv", "anime")
    return ()


async def discover_external_catalogs(manifest_url: str) -> dict[str, Any]:
    normalized_url = normalize_external_manifest_url(manifest_url)
    if not normalized_url:
        raise ValueError("Manifest URL is required")

    async with get_http_client() as client:
        response = await client.get(normalized_url)
        response.raise_for_status()
        payload = response.json()

    addon_name = str(payload.get("name") or "External addon").strip() or "External addon"
    discovered: list[dict[str, str]] = []
    for catalog in payload.get("catalogs") or []:
        if not isinstance(catalog, dict):
            continue
        catalog_id = str(catalog.get("id") or "").strip()
        catalog_type = str(catalog.get("type") or "").strip().lower()
        if not catalog_id or catalog_type not in {"movie", "series"}:
            continue
        discovered.append(
            {
                "id": catalog_id,
                "name": str(catalog.get("name") or catalog_id),
                "type": catalog_type,
            }
        )

    return {
        "manifest_url": normalized_url,
        "addon_name": addon_name,
        "catalogs": sorted(discovered, key=lambda entry: (entry["type"], entry["name"].lower())),
    }


async def refresh_external_catalog(
    db: AsyncSession,
    catalog: StremioExternalCatalog,
    *,
    max_items: int | None = None,
) -> int:
    normalized_manifest_url = normalize_external_manifest_url(catalog.manifest_url)
    max_catalog_items = max_items or settings.external_catalog_max_items
    fetched_at = datetime.now(timezone.utc)

    async with get_http_client() as client:
        manifest_response = await client.get(normalized_manifest_url)
        manifest_response.raise_for_status()
        manifest_payload = manifest_response.json()
        catalog.addon_name = (
            str(manifest_payload.get("name") or catalog.addon_name or "").strip() or None
        )

        base_url = normalized_manifest_url[: -len("/manifest.json")]
        metas: list[dict[str, Any]] = []
        skip = 0
        while len(metas) < max_catalog_items:
            limit = min(EXTERNAL_CATALOG_FETCH_PAGE_SIZE, max_catalog_items - len(metas))
            catalog_url = (
                f"{base_url}/catalog/{catalog.source_catalog_type}/{catalog.source_catalog_id}"
                f"/skip={skip}&limit={limit}.json"
            )
            response = await client.get(catalog_url)
            response.raise_for_status()
            payload = response.json()
            batch = payload.get("metas") if isinstance(payload, dict) else None
            if not isinstance(batch, list) or not batch:
                break
            metas.extend(item for item in batch if isinstance(item, dict))
            if len(batch) < limit:
                break
            skip += limit

    await db.execute(
        delete(StremioExternalCatalogItem).where(
            StremioExternalCatalogItem.catalog_id == catalog.id
        )
    )

    created = 0
    for position, meta in enumerate(metas):
        stremio_id = str(meta.get("id") or "").strip()
        stremio_type = str(meta.get("type") or catalog.source_catalog_type or "").strip().lower()
        if not stremio_id or stremio_type not in {"movie", "series"}:
            continue
        imdb_id = _extract_imdb_id(meta, stremio_id)
        media_item_id = await _resolve_external_media_item_id(db, stremio_id, imdb_id, stremio_type)
        db.add(
            StremioExternalCatalogItem(
                id=str(uuid.uuid4()),
                catalog_id=catalog.id,
                media_item_id=media_item_id,
                stremio_id=stremio_id,
                stremio_type=stremio_type,
                title=_coerce_text(meta.get("name")),
                year=_coerce_year(meta.get("year")),
                poster_url=_coerce_text(meta.get("poster")),
                imdb_id=imdb_id,
                position=position,
                fetched_at=fetched_at,
            )
        )
        created += 1

    catalog.manifest_url = normalized_manifest_url
    catalog.last_refreshed_at = fetched_at
    catalog.last_refresh_error = None
    catalog.updated_at = fetched_at
    db.add(catalog)
    await db.flush()
    return created


async def mark_external_catalog_refresh_failed(
    db: AsyncSession,
    catalog: StremioExternalCatalog,
    error: Exception,
) -> None:
    catalog.last_refresh_error = str(error)
    catalog.updated_at = datetime.now(timezone.utc)
    db.add(catalog)
    await db.flush()


def _coerce_text(value: object) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _coerce_year(value: object) -> int | None:
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.strip().isdigit():
        return int(value.strip())
    return None


def _extract_imdb_id(meta: dict[str, Any], stremio_id: str) -> str | None:
    imdb_id = _coerce_text(meta.get("imdb_id"))
    if imdb_id:
        return imdb_id
    if stremio_id.startswith("tt"):
        return stremio_id
    return None


async def _resolve_external_media_item_id(
    db: AsyncSession,
    stremio_id: str,
    imdb_id: str | None,
    stremio_type: str,
) -> str | None:
    media_types = stremio_type_to_media_types(stremio_type)
    if not media_types:
        return None

    conditions = [MediaItem.raw["stremio_id"].as_string() == stremio_id]
    if imdb_id:
        conditions.append(MediaItem.imdb_id == imdb_id)
    result = await db.execute(
        select(MediaItem.id)
        .where(MediaItem.media_type.in_(media_types), or_(*conditions))
        .limit(1)
    )
    media_item_id = result.scalars().first()
    return str(media_item_id) if media_item_id else None
