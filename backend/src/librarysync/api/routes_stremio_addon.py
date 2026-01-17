from __future__ import annotations

from datetime import datetime, timezone
from typing import Literal

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from librarysync.api.deps import get_current_user, get_db
from librarysync.config import settings
from librarysync.core.stremio_addon import (
    build_default_catalogs,
    ensure_addon_config,
    rotate_addon_key,
)
from librarysync.db.models import StremioAddonConfig, StremioCustomCatalog, User

router = APIRouter(prefix="/api/stremio-addon", tags=["stremio-addon"])


class StremioCatalogFilters(BaseModel):
    statuses: list[str] | None = None


class StremioCatalogOrdering(BaseModel):
    order_by: str | None = None
    order_dir: Literal["asc", "desc"] | None = None


class StremioCatalogUpdate(BaseModel):
    id: str
    enabled: bool | None = None
    filters: StremioCatalogFilters | None = None
    ordering: StremioCatalogOrdering | None = None


class StremioAddonConfigUpdate(BaseModel):
    is_enabled: bool | None = None
    catalogs: list[StremioCatalogUpdate] | None = None


def _resolve_base_url(request: Request) -> str:
    if settings.base_url:
        return settings.base_url.rstrip("/")
    return str(request.base_url).rstrip("/")


def _build_manifest_links(base_url: str, addon_key: str) -> dict[str, str]:
    manifest_url = f"{base_url}/stremio-addon/{addon_key}/manifest.json"
    install_url = f"stremio://{manifest_url}"
    return {"manifest_url": manifest_url, "install_url": install_url}


def _merge_catalog_updates(
    existing: list[dict],
    updates: list[StremioCatalogUpdate],
) -> list[dict]:
    by_id = {catalog.get("id"): catalog for catalog in existing if catalog.get("id")}
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
    return list(by_id.values())


async def _ensure_default_catalogs(
    db: AsyncSession,
    config: StremioAddonConfig,
) -> list[dict]:
    catalogs = config.default_catalogs
    if not catalogs:
        catalogs = build_default_catalogs()
        config.default_catalogs = catalogs
        db.add(config)
        await db.commit()
        await db.refresh(config)
    return catalogs


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
    config, addon_key = await ensure_addon_config(db, current_user.id)
    catalogs = await _ensure_default_catalogs(db, config)
    custom_result = await db.execute(
        select(StremioCustomCatalog).where(StremioCustomCatalog.user_id == current_user.id)
    )
    custom_catalogs = [
        {
            "id": catalog.id,
            "name": catalog.name,
            "slug": catalog.slug,
            "media_type": catalog.media_type,
            "order_by": catalog.order_by,
            "order_dir": catalog.order_dir,
            "created_at": catalog.created_at,
            "updated_at": catalog.updated_at,
        }
        for catalog in custom_result.scalars().all()
    ]
    payload: dict[str, object] = {
        "is_enabled": bool(config.is_enabled),
        "addon_key_last_rotated_at": config.addon_key_last_rotated_at,
        "catalogs": catalogs,
        "custom_catalogs": custom_catalogs,
    }
    if addon_key:
        payload["addon_key"] = addon_key
        payload.update(_build_manifest_links(_resolve_base_url(request), addon_key))
    return payload


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
    result = await db.execute(
        select(StremioAddonConfig).where(StremioAddonConfig.user_id == current_user.id)
    )
    config = result.scalars().first()
    if not config:
        config, _ = await ensure_addon_config(db, current_user.id)
    catalogs = await _ensure_default_catalogs(db, config)

    fields = payload.model_fields_set
    if "is_enabled" in fields:
        config.is_enabled = bool(payload.is_enabled)
    if payload.catalogs:
        catalogs = _merge_catalog_updates(catalogs, payload.catalogs)
        config.default_catalogs = catalogs
    config.updated_at = datetime.now(timezone.utc)
    db.add(config)
    await db.commit()
    await db.refresh(config)
    return {
        "is_enabled": config.is_enabled,
        "catalogs": config.default_catalogs,
    }


@router.post(
    "/token/rotate",
    summary="Rotate Stremio addon key",
    description="Rotate the per-user addon key and return install links.",
)
async def rotate_stremio_addon_token(
    request: Request,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    result = await db.execute(
        select(StremioAddonConfig).where(StremioAddonConfig.user_id == current_user.id)
    )
    config = result.scalars().first()
    if not config:
        config, _ = await ensure_addon_config(db, current_user.id)
    addon_key = await rotate_addon_key(db, config)
    return {
        "addon_key": addon_key,
        "addon_key_last_rotated_at": config.addon_key_last_rotated_at,
        **_build_manifest_links(_resolve_base_url(request), addon_key),
    }
