from __future__ import annotations

import asyncio
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

import httpx
from sqlalchemy import delete, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from librarysync.config import settings
from librarysync.connectors.metadata.tmdb import TmdbMetadataProvider
from librarysync.connectors.metadata.tvdb import TvdbMetadataProvider
from librarysync.connectors.services.letterboxd import (
    DEFAULT_LETTERBOXD_API_BASE_URL,
    LetterboxdClient,
    has_required_letterboxd_fields,
)
from librarysync.core.catalog_ordering import CatalogOrderBy
from librarysync.core.http_client import get_http_client
from librarysync.core.integrations import load_integration_with_secrets
from librarysync.core.metadata_providers import MetadataProviderService
from librarysync.core.watchlist_links import (
    parse_imdb_chart_urls,
    parse_letterboxd_list_urls,
    parse_mdblist_urls,
    parse_tmdb_chart_urls,
    parse_tmdb_list_urls,
    parse_tvdb_list_urls,
)
from librarysync.db.models import MediaItem, StremioExternalCatalog, StremioExternalCatalogItem
from librarysync.jobs.letterboxd_import import build_letterboxd_list_candidate

EXTERNAL_CATALOG_ORDER_BY = {"source", "random", *CatalogOrderBy.__args__}
EXTERNAL_CATALOG_ORDER_DIR = {"asc", "desc"}
EXTERNAL_CATALOG_DEFAULT_PAGE_SIZE = 30
EXTERNAL_CATALOG_DEFAULT_SHOW_IN_HOME = True
EXTERNAL_CATALOG_DEFAULT_FILTERS = {"show_watched": False, "statuses": []}
EXTERNAL_CATALOG_FETCH_PAGE_SIZE = 100
EXTERNAL_SOURCE_KINDS = {"manifest", "list"}
EXTERNAL_LIST_PROVIDERS = {"tmdb", "tvdb", "letterboxd", "mdblist"}
TMDB_LIST_TYPE = "movie"
TMDB_CHART_ENDPOINTS = {
    "tmdb-chart:movie:top-rated": ("/movie/top_rated", "movie"),
    "tmdb-chart:movie:popular": ("/movie/popular", "movie"),
    "tmdb-chart:movie:now-playing": ("/movie/now_playing", "movie"),
    "tmdb-chart:movie:upcoming": ("/movie/upcoming", "movie"),
    "tmdb-chart:tv:top-rated": ("/tv/top_rated", "series"),
    "tmdb-chart:tv:popular": ("/tv/popular", "series"),
    "tmdb-chart:tv:on-the-air": ("/tv/on_the_air", "series"),
    "tmdb-chart:tv:airing-today": ("/tv/airing_today", "series"),
}


@dataclass(frozen=True)
class ExternalCatalogListItem:
    stremio_id: str
    stremio_type: str
    title: str | None
    year: int | None
    poster_url: str | None
    imdb_id: str | None = None
    tmdb_id: str | None = None
    tvdb_id: str | None = None


def normalize_external_manifest_url(value: str) -> str:
    manifest_url = (value or "").strip()
    if not manifest_url:
        return ""
    if manifest_url.endswith("/manifest.json"):
        return manifest_url
    return f"{manifest_url.rstrip('/')}/manifest.json"


def normalize_external_source_kind(value: str | None) -> str:
    normalized = (value or "manifest").strip().lower()
    return normalized if normalized in EXTERNAL_SOURCE_KINDS else "manifest"


def normalize_external_source_provider(value: str | None) -> str | None:
    normalized = (value or "").strip().lower()
    return normalized if normalized in EXTERNAL_LIST_PROVIDERS else None


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
    source_kind = normalize_external_source_kind(getattr(catalog, "source_kind", None))
    return {
        "id": catalog.id,
        "name": catalog.name,
        "slug": catalog.slug,
        "source_kind": source_kind,
        "source_provider": normalize_external_source_provider(
            getattr(catalog, "source_provider", None)
        ),
        "source_url": catalog.manifest_url,
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
    raise NotImplementedError("Use discover_external_catalog_source")


async def discover_external_catalog_source(
    db: AsyncSession,
    user_id: str,
    source_url: str,
) -> dict[str, Any]:
    cleaned_url = (source_url or "").strip()
    if not cleaned_url:
        raise ValueError("Source URL is required")

    tmdb_refs = parse_tmdb_list_urls([cleaned_url])
    if tmdb_refs:
        return await _discover_tmdb_list_catalogs(db, user_id, tmdb_refs[0])

    tmdb_chart_refs = parse_tmdb_chart_urls([cleaned_url])
    if tmdb_chart_refs:
        return await _discover_tmdb_chart_catalogs(tmdb_chart_refs[0])

    tvdb_refs = parse_tvdb_list_urls([cleaned_url])
    if tvdb_refs:
        return await _discover_tvdb_list_catalogs(db, user_id, tvdb_refs[0])

    letterboxd_refs = parse_letterboxd_list_urls([cleaned_url])
    if letterboxd_refs:
        return await _discover_letterboxd_list_catalogs(db, user_id, letterboxd_refs[0])

    mdblist_refs = parse_mdblist_urls([cleaned_url])
    if mdblist_refs:
        return await _discover_mdblist_catalogs(mdblist_refs[0])

    imdb_chart_refs = parse_imdb_chart_urls([cleaned_url])
    if imdb_chart_refs:
        raise ValueError(
            "IMDb chart pages are not supported. Use TMDB charts/lists, TVDB lists, "
            "Letterboxd lists, or a Stremio manifest."
        )

    return await _discover_manifest_catalogs(cleaned_url)


async def _discover_manifest_catalogs(manifest_url: str) -> dict[str, Any]:
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
                "source_kind": "manifest",
                "source_provider": None,
            }
        )

    return {
        "source_kind": "manifest",
        "source_provider": None,
        "source_url": normalized_url,
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
    if normalize_external_source_kind(getattr(catalog, "source_kind", None)) == "list":
        return await _refresh_external_list_catalog(db, catalog, max_items=max_items)

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

    metas = _dedupe_external_metas(metas)
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


async def _refresh_external_list_catalog(
    db: AsyncSession,
    catalog: StremioExternalCatalog,
    *,
    max_items: int | None = None,
) -> int:
    provider = normalize_external_source_provider(catalog.source_provider)
    if not provider:
        raise ValueError("List provider is required")

    max_catalog_items = max_items or settings.external_catalog_max_items
    if provider == "tmdb":
        items = await _load_tmdb_list_items(
            db,
            catalog.user_id,
            catalog.manifest_url,
            catalog.source_catalog_id,
            max_catalog_items,
        )
    elif provider == "tvdb":
        items = await _load_tvdb_list_items(
            db, catalog.user_id, catalog.manifest_url, max_catalog_items
        )
    elif provider == "letterboxd":
        items = await _load_letterboxd_list_items(
            db, catalog.user_id, catalog.manifest_url, max_catalog_items
        )
    elif provider == "mdblist":
        items = await _load_mdblist_items(catalog.manifest_url, max_catalog_items)
    else:
        raise ValueError("Unsupported list provider")

    items = [item for item in items if item.stremio_type == catalog.source_catalog_type]
    items = _dedupe_external_list_items(items)

    fetched_at = datetime.now(timezone.utc)
    await db.execute(
        delete(StremioExternalCatalogItem).where(
            StremioExternalCatalogItem.catalog_id == catalog.id
        )
    )

    created = 0
    for position, item in enumerate(items):
        media_item_id = await _resolve_external_media_item_id(
            db,
            item.stremio_id,
            item.imdb_id,
            item.stremio_type,
            tmdb_id=item.tmdb_id,
            tvdb_id=item.tvdb_id,
        )
        db.add(
            StremioExternalCatalogItem(
                id=str(uuid.uuid4()),
                catalog_id=catalog.id,
                media_item_id=media_item_id,
                stremio_id=item.stremio_id,
                stremio_type=item.stremio_type,
                title=item.title,
                year=item.year,
                poster_url=item.poster_url,
                imdb_id=item.imdb_id,
                position=position,
                fetched_at=fetched_at,
            )
        )
        created += 1

    catalog.last_refreshed_at = fetched_at
    catalog.last_refresh_error = None
    catalog.updated_at = fetched_at
    db.add(catalog)
    await db.flush()
    return created


def _dedupe_external_metas(metas: list[dict[str, Any]]) -> list[dict[str, Any]]:
    deduped: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    for meta in metas:
        stremio_id = str(meta.get("id") or "").strip()
        if not stremio_id or stremio_id in seen_ids:
            continue
        seen_ids.add(stremio_id)
        deduped.append(meta)
    return deduped


def _dedupe_external_list_items(
    items: list[ExternalCatalogListItem],
) -> list[ExternalCatalogListItem]:
    deduped: list[ExternalCatalogListItem] = []
    seen_ids: set[str] = set()
    for item in items:
        stremio_id = item.stremio_id.strip()
        if not stremio_id or stremio_id in seen_ids:
            continue
        seen_ids.add(stremio_id)
        deduped.append(item)
    return deduped


def _coerce_text(value: object) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _coerce_year(value: object) -> int | None:
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        cleaned = value.strip()
        if cleaned.isdigit():
            return int(cleaned)
        if len(cleaned) >= 4 and cleaned[:4].isdigit():
            return int(cleaned[:4])
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
    *,
    tmdb_id: str | None = None,
    tvdb_id: str | None = None,
) -> str | None:
    media_types = stremio_type_to_media_types(stremio_type)
    if not media_types:
        return None

    conditions = [MediaItem.raw["stremio_id"].as_string() == stremio_id]
    if imdb_id:
        conditions.append(MediaItem.imdb_id == imdb_id)
    if tmdb_id:
        conditions.append(MediaItem.tmdb_id == tmdb_id)
    if tvdb_id:
        conditions.append(MediaItem.tvdb_id == tvdb_id)
    result = await db.execute(
        select(MediaItem.id)
        .where(MediaItem.media_type.in_(media_types), or_(*conditions))
        .limit(1)
    )
    media_item_id = result.scalars().first()
    return str(media_item_id) if media_item_id else None


async def _discover_tmdb_list_catalogs(db: AsyncSession, user_id: str, ref) -> dict[str, Any]:
    provider = await _load_tmdb_provider(db, user_id)
    payload = await provider._get(f"/list/{ref.list_id}", {"page": 1})
    name = str(payload.get("name") or ref.name).strip() or ref.name
    return {
        "source_kind": "list",
        "source_provider": "tmdb",
        "source_url": ref.url,
        "manifest_url": ref.url,
        "addon_name": "TMDB List",
        "catalogs": [{"id": ref.external_id, "name": name, "type": TMDB_LIST_TYPE}],
    }


async def _discover_tmdb_chart_catalogs(ref) -> dict[str, Any]:
    return {
        "source_kind": "list",
        "source_provider": "tmdb",
        "source_url": ref.url,
        "manifest_url": ref.url,
        "addon_name": "TMDB Charts",
        "catalogs": [
            {
                "id": ref.external_id,
                "name": ref.name,
                "type": "movie" if ref.media_type == "movie" else "series",
            }
        ],
    }


async def _discover_tvdb_list_catalogs(db: AsyncSession, user_id: str, ref) -> dict[str, Any]:
    provider = await _load_tvdb_provider(db, user_id)
    payload = await _tvdb_get_list_payload(provider, ref.list_id)
    data = payload.get("data") if isinstance(payload, dict) else None
    name = str((data or {}).get("name") or ref.name).strip() or ref.name
    catalog_types = _detect_tvdb_catalog_types(data)
    return {
        "source_kind": "list",
        "source_provider": "tvdb",
        "source_url": ref.url,
        "manifest_url": ref.url,
        "addon_name": "TVDB List",
        "catalogs": [
            {
                "id": f"{ref.external_id}:{catalog_type}",
                "name": name,
                "type": catalog_type,
            }
            for catalog_type in catalog_types
        ],
    }


async def _discover_letterboxd_list_catalogs(db: AsyncSession, user_id: str, ref) -> dict[str, Any]:
    client, access_token = await _build_letterboxd_client(db, user_id)
    payload = await client.fetch_list_by_slug(access_token, ref.username, ref.slug)
    name = str(payload.get("name") or ref.name).strip() or ref.name
    return {
        "source_kind": "list",
        "source_provider": "letterboxd",
        "source_url": ref.url,
        "manifest_url": ref.url,
        "addon_name": "Letterboxd List",
        "catalogs": [{"id": ref.external_id, "name": name, "type": "movie"}],
    }


async def _discover_mdblist_catalogs(ref) -> dict[str, Any]:
    items = await _load_mdblist_items(ref.url, max_items=50)
    catalog_types = sorted({item.stremio_type for item in items})
    if not catalog_types:
        catalog_types = ["movie"]
    return {
        "source_kind": "list",
        "source_provider": "mdblist",
        "source_url": ref.url,
        "manifest_url": ref.url,
        "addon_name": "MDBList",
        "catalogs": [
            {
                "id": f"{ref.external_id}:{catalog_type}",
                "name": ref.name.replace("-", " ").title(),
                "type": catalog_type,
            }
            for catalog_type in catalog_types
        ],
    }


async def _load_tmdb_provider(db: AsyncSession, user_id: str) -> TmdbMetadataProvider:
    provider = await MetadataProviderService(db, user_id).load_provider("tmdb")
    if not isinstance(provider, TmdbMetadataProvider):
        raise ValueError("TMDB provider is not enabled or missing API key")
    return provider


async def _load_tvdb_provider(db: AsyncSession, user_id: str) -> TvdbMetadataProvider:
    provider = await MetadataProviderService(db, user_id).load_provider("tvdb")
    if not isinstance(provider, TvdbMetadataProvider):
        raise ValueError("TVDB provider is not enabled or missing API key")
    return provider


async def _load_tmdb_list_items(
    db: AsyncSession,
    user_id: str,
    source_url: str,
    source_catalog_id: str,
    max_items: int,
) -> list[ExternalCatalogListItem]:
    provider = await _load_tmdb_provider(db, user_id)
    chart_ref = TMDB_CHART_ENDPOINTS.get(source_catalog_id)
    if chart_ref:
        endpoint, stremio_type = chart_ref
        return await _load_tmdb_chart_items(provider, endpoint, stremio_type, max_items)

    refs = parse_tmdb_list_urls([source_url])
    if not refs:
        raise ValueError("Unsupported TMDB list URL")
    ref = refs[0]

    results: list[dict[str, Any]] = []
    page = 1
    while len(results) < max_items:
        payload = await provider._get(f"/list/{ref.list_id}", {"page": page})
        batch = payload.get("items") if isinstance(payload, dict) else None
        if not isinstance(batch, list) or not batch:
            break
        results.extend(item for item in batch if isinstance(item, dict))
        total_pages = int(payload.get("total_pages") or page)
        if page >= total_pages:
            break
        page += 1

    detail_items = await _load_tmdb_external_ids(provider, results[:max_items])
    return [item for item in detail_items if item.stremio_id]


async def _load_tmdb_chart_items(
    provider: TmdbMetadataProvider,
    endpoint: str,
    stremio_type: str,
    max_items: int,
) -> list[ExternalCatalogListItem]:
    results: list[dict[str, Any]] = []
    page = 1
    while len(results) < max_items:
        payload = await provider._get(endpoint, {"page": page})
        batch = payload.get("results") if isinstance(payload, dict) else None
        if not isinstance(batch, list) or not batch:
            break
        results.extend(item for item in batch if isinstance(item, dict))
        total_pages = int(payload.get("total_pages") or page)
        if page >= total_pages:
            break
        page += 1
    return await _load_tmdb_external_ids(
        provider,
        results[:max_items],
        stremio_type=stremio_type,
    )


async def _load_tmdb_external_ids(
    provider: TmdbMetadataProvider,
    items: list[dict[str, Any]],
    *,
    stremio_type: str = "movie",
) -> list[ExternalCatalogListItem]:
    semaphore = asyncio.Semaphore(8)

    async def _build(item: dict[str, Any]) -> ExternalCatalogListItem | None:
        tmdb_id = _coerce_text(item.get("id"))
        if not tmdb_id:
            return None
        details_path = "/movie" if stremio_type == "movie" else "/tv"
        async with semaphore:
            payload = await provider._get(f"{details_path}/{tmdb_id}/external_ids", {})
        imdb_id = _coerce_text(payload.get("imdb_id"))
        stremio_id = imdb_id
        if not stremio_id:
            return None
        return ExternalCatalogListItem(
            stremio_id=stremio_id,
            stremio_type=stremio_type,
            title=_coerce_text(
                item.get("title")
                or item.get("original_title")
                or item.get("name")
                or item.get("original_name")
            ),
            year=_coerce_year(
                item.get("release_date")
                or item.get("first_air_date")
                or item.get("year")
            ),
            poster_url=_tmdb_poster_url(item.get("poster_path")),
            imdb_id=imdb_id,
            tmdb_id=tmdb_id,
        )

    results = await asyncio.gather(*[_build(item) for item in items])
    return [item for item in results if item is not None]


async def _load_tvdb_list_items(
    db: AsyncSession,
    user_id: str,
    source_url: str,
    max_items: int,
) -> list[ExternalCatalogListItem]:
    refs = parse_tvdb_list_urls([source_url])
    if not refs:
        raise ValueError("Unsupported TVDB list URL")
    ref = refs[0]
    provider = await _load_tvdb_provider(db, user_id)
    payload = await _tvdb_get_list_payload(provider, ref.list_id)
    data = payload.get("data") if isinstance(payload, dict) else None
    entities = (data or {}).get("entities") if isinstance(data, dict) else None
    if not isinstance(entities, list):
        return []

    items: list[ExternalCatalogListItem] = []
    for entry in entities[:max_items]:
        if not isinstance(entry, dict):
            continue
        media_type = _normalize_tvdb_entity_type(entry)
        if not media_type:
            continue
        imdb_id = _extract_remote_id(entry, "imdb")
        tvdb_id = _coerce_text(entry.get("id"))
        stremio_id = imdb_id
        if not stremio_id:
            continue
        items.append(
            ExternalCatalogListItem(
                stremio_id=stremio_id,
                stremio_type="movie" if media_type == "movie" else "series",
                title=_coerce_text(entry.get("name") or entry.get("title") or entry.get("slug")),
                year=_coerce_year(
                    entry.get("year")
                    or entry.get("firstAired")
                    or entry.get("first_aired")
                    or entry.get("releaseDate")
                ),
                poster_url=_coerce_text(
                    entry.get("image") or entry.get("image_url") or entry.get("imageUrl")
                ),
                imdb_id=imdb_id,
                tmdb_id=_extract_remote_id(entry, "tmdb"),
                tvdb_id=tvdb_id,
            )
        )
    return items


async def _load_letterboxd_list_items(
    db: AsyncSession,
    user_id: str,
    source_url: str,
    max_items: int,
) -> list[ExternalCatalogListItem]:
    refs = parse_letterboxd_list_urls([source_url])
    if not refs:
        raise ValueError("Unsupported Letterboxd list URL")
    ref = refs[0]
    client, access_token = await _build_letterboxd_client(db, user_id)
    entries = await client.get_list_entries(
        access_token,
        ref.username,
        ref.slug,
        per_page=50,
        max_pages=10,
    )
    items: list[ExternalCatalogListItem] = []
    list_context = {"name": ref.name, "url": ref.url, "type": "list"}
    for entry in entries:
        candidate = build_letterboxd_list_candidate(entry, list_context=list_context)
        if not candidate:
            continue
        imdb_id = candidate.ids.get("imdb_id")
        stremio_id = imdb_id
        if not stremio_id:
            continue
        items.append(
            ExternalCatalogListItem(
                stremio_id=stremio_id,
                stremio_type="movie",
                title=candidate.title,
                year=candidate.year,
                poster_url=candidate.poster_url,
                imdb_id=imdb_id,
                tmdb_id=candidate.ids.get("tmdb_id"),
            )
        )
        if len(items) >= max_items:
            break
    return items


async def _load_mdblist_items(source_url: str, max_items: int) -> list[ExternalCatalogListItem]:
    refs = parse_mdblist_urls([source_url])
    if not refs:
        raise ValueError("Unsupported MDBList URL")
    ref = refs[0]
    json_url = f"https://mdblist.com/lists/{ref.username}/{ref.slug}/json/"
    async with get_http_client() as client:
        response = await client.get(json_url)
        response.raise_for_status()
        payload = response.json()

    if not isinstance(payload, list):
        raise ValueError("MDBList list payload is invalid")

    items: list[ExternalCatalogListItem] = []
    for entry in payload[:max_items]:
        if not isinstance(entry, dict):
            continue
        imdb_id = _coerce_text(entry.get("imdb_id"))
        stremio_id = imdb_id
        if not stremio_id:
            continue
        media_type = _coerce_text(entry.get("mediatype")) or "movie"
        items.append(
            ExternalCatalogListItem(
                stremio_id=stremio_id,
                stremio_type="movie" if media_type == "movie" else "series",
                title=_coerce_text(entry.get("title")),
                year=_coerce_year(entry.get("release_year") or entry.get("year")),
                poster_url=None,
                imdb_id=imdb_id,
                tmdb_id=_coerce_text(
                    entry.get("tmdb_id") or entry.get("tmdbid") or entry.get("id")
                ),
                tvdb_id=_coerce_text(entry.get("tvdb_id") or entry.get("tvdbid")),
            )
        )
    return items


async def _build_letterboxd_client(
    db: AsyncSession, user_id: str
) -> tuple[LetterboxdClient, str]:
    integration, secret_data = await load_integration_with_secrets(db, user_id, "letterboxd")
    if not integration or integration.status == "disconnected":
        raise ValueError("Letterboxd integration is not connected")
    if not secret_data or not has_required_letterboxd_fields(secret_data):
        raise ValueError("Letterboxd credentials are incomplete")
    api_base_url = DEFAULT_LETTERBOXD_API_BASE_URL
    if integration.config and integration.config.get("api_base_url"):
        api_base_url = str(integration.config["api_base_url"])
    client = LetterboxdClient(
        api_base_url=api_base_url,
        client_id=str(secret_data.get("client_id")),
        client_secret=str(secret_data.get("client_secret")),
        refresh_token=str(secret_data.get("refresh_token")),
    )
    access_token = await client.refresh_access_token()
    return client, access_token


async def _tvdb_get_list_payload(provider: TvdbMetadataProvider, list_id: str) -> dict[str, Any]:
    try:
        return await provider._get(f"/lists/{list_id}/extended", {})
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code != 404:
            raise
        return await provider._get(f"/lists/slug/{list_id}", {})


def _detect_tvdb_catalog_types(data: dict[str, Any] | None) -> list[str]:
    entities = (data or {}).get("entities") if isinstance(data, dict) else None
    if not isinstance(entities, list):
        return ["series"]
    has_movie = False
    has_series = False
    for entry in entities:
        media_type = _normalize_tvdb_entity_type(entry) if isinstance(entry, dict) else None
        if media_type == "movie":
            has_movie = True
        elif media_type == "tv":
            has_series = True
    if has_movie and has_series:
        return ["movie", "series"]
    if has_movie:
        return ["movie"]
    return ["series"]


def _normalize_tvdb_entity_type(entry: dict[str, Any]) -> str | None:
    raw = _coerce_text(entry.get("type") or entry.get("recordType"))
    if not raw:
        raw = _coerce_text(entry.get("companyType") or entry.get("kind"))
    if not raw:
        return None
    lowered = raw.lower()
    if lowered in {"movie", "movies"}:
        return "movie"
    if lowered in {"series", "tv", "show"}:
        return "tv"
    return None


def _extract_remote_id(entry: dict[str, Any], provider: str) -> str | None:
    remote_ids = entry.get("remoteIds") or entry.get("remote_ids") or []
    if not isinstance(remote_ids, list):
        return None
    for remote in remote_ids:
        if not isinstance(remote, dict):
            continue
        source_name = str(remote.get("sourceName") or "").lower()
        source = str(remote.get("source") or "").lower()
        entry_type = str(remote.get("type") or "").lower()
        if provider == "imdb" and (
            source_name == "imdb" or source == "imdb" or entry_type == "imdb"
        ):
            return _coerce_text(remote.get("id") or remote.get("value"))
        if provider == "tmdb" and (
            source_name == "tmdb"
            or "themoviedb" in source_name
            or source == "tmdb"
            or entry_type == "tmdb"
        ):
            return _coerce_text(remote.get("id") or remote.get("value"))
    return None


def _tmdb_poster_url(path: object) -> str | None:
    value = _coerce_text(path)
    if not value:
        return None
    if value.startswith("http://") or value.startswith("https://"):
        return value
    return f"https://image.tmdb.org/t/p/w185{value}"
