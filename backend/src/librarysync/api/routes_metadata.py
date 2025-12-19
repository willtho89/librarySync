import json
import re
from typing import Literal

import httpx
from fastapi import APIRouter, Depends, HTTPException, Path, status
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from librarysync.api.deps import get_current_user, get_db
from librarysync.connectors.metadata.base import MediaCandidate, MetadataProvider
from librarysync.connectors.metadata.imdb import ImdbMetadataProvider
from librarysync.connectors.metadata.kitsu import KitsuMetadataProvider
from librarysync.connectors.metadata.myanimelist import MyAnimeListMetadataProvider
from librarysync.connectors.metadata.tmdb import TmdbMetadataProvider
from librarysync.connectors.metadata.tvdb import TvdbMetadataProvider
from librarysync.connectors.metadata.tvmaze import TvmazeMetadataProvider
from librarysync.core.security import decrypt_value, encrypt_value
from librarysync.db.models import (
    Integration,
    IntegrationSecret,
    MediaItem,
    MetadataLookupCandidate,
    MetadataLookupRequest,
    User,
)

router = APIRouter(prefix="/api/metadata", tags=["metadata"])

IMDB_ID_RE = re.compile(r"^tt\d+$", re.IGNORECASE)
TMDB_ID_RE = re.compile(r"^\d+$")
METADATA_PROVIDERS = ("tmdb", "tvdb", "tvmaze", "imdb", "kitsu", "myanimelist")
PROVIDERS_WITH_SECRETS = {"tmdb", "tvdb"}


class ProviderOut(BaseModel):
    provider: str
    enabled: bool
    config: dict
    has_credentials: bool


class TmdbSettingsIn(BaseModel):
    enabled: bool = True
    api_key: str | None = None
    language: str | None = None
    region: str | None = None


class TvdbSettingsIn(BaseModel):
    enabled: bool = True
    api_key: str | None = None
    pin: str | None = None
    language: str | None = None


class KitsuSettingsIn(BaseModel):
    enabled: bool = True
    language: str | None = None


class TvmazeSettingsIn(BaseModel):
    enabled: bool = True


class ImdbSettingsIn(BaseModel):
    enabled: bool = True


class MyAnimeListSettingsIn(BaseModel):
    enabled: bool = True


class LookupCreateIn(BaseModel):
    query: str = Field(..., min_length=1, max_length=255)
    search_scope: Literal["all", "movie", "tv", "anime"] = "all"


class LookupCreateOut(BaseModel):
    lookup_id: str
    status: str


class CandidateOut(BaseModel):
    id: str
    provider: str
    provider_item_id: str
    providers: list[str] = Field(default_factory=list)
    media_type: str
    title: str
    year: int | None
    poster_url: str | None
    imdb_id: str | None
    tmdb_id: str | None = None
    tvdb_id: str | None = None
    tvmaze_id: str | None = None
    kitsu_id: str | None = None
    myanimelist_id: str | None = None


class SeasonOut(BaseModel):
    season_number: int
    name: str | None
    episode_count: int | None
    air_date: str | None
    poster_url: str | None


class EpisodeOut(BaseModel):
    episode_number: int
    title: str | None
    tmdb_id: str | None
    air_date: str | None
    still_url: str | None


class LookupStatusOut(BaseModel):
    lookup_id: str
    status: str
    error: str | None = None
    query: str
    query_type: str
    search_scope: str
    candidates: list[CandidateOut]


def _normalize_title_key(title: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", title.lower()).strip()


def _candidate_imdb_id(candidate: MetadataLookupCandidate) -> str | None:
    imdb_id = candidate.imdb_id
    if not imdb_id and candidate.provider == "imdb":
        imdb_id = candidate.provider_item_id
    return imdb_id.lower() if imdb_id else None


def _candidate_tmdb_id(candidate: MetadataLookupCandidate) -> str | None:
    raw = candidate.raw if isinstance(candidate.raw, dict) else {}
    value = raw.get("tmdb_id") or raw.get("tmdbId") or raw.get("tmdbID")
    if value:
        return str(value)
    remote_ids = raw.get("remoteIds") or raw.get("remote_ids") or []
    if isinstance(remote_ids, list):
        for entry in remote_ids:
            if not isinstance(entry, dict):
                continue
            source_name = str(entry.get("sourceName") or "").lower()
            entry_type = entry.get("type")
            entry_source = str(entry.get("source") or "").lower()
            if "themoviedb" in source_name or source_name == "tmdb":
                tmdb_value = entry.get("id") or entry.get("value")
                if tmdb_value:
                    return str(tmdb_value)
            if entry_source == "tmdb":
                tmdb_value = entry.get("id") or entry.get("value")
                if tmdb_value:
                    return str(tmdb_value)
            if str(entry_type).lower() == "tmdb" or entry_type == 10:
                tmdb_value = entry.get("id") or entry.get("value")
                if tmdb_value:
                    return str(tmdb_value)
    return None


def _candidate_raw_id(
    candidate: MetadataLookupCandidate, keys: tuple[str, ...]
) -> str | None:
    raw = candidate.raw if isinstance(candidate.raw, dict) else {}
    for key in keys:
        value = raw.get(key)
        if value:
            return str(value)
    nested = raw.get("ids")
    if isinstance(nested, dict):
        for key in keys:
            value = nested.get(key)
            if value:
                return str(value)
    return None


def _candidate_ids(candidate: MetadataLookupCandidate) -> dict[str, str]:
    ids: dict[str, str] = {}
    provider_id = candidate.provider_item_id
    if provider_id:
        if candidate.provider == "tmdb":
            ids["tmdb_id"] = provider_id
        elif candidate.provider == "tvdb":
            ids["tvdb_id"] = provider_id
        elif candidate.provider == "tvmaze":
            ids["tvmaze_id"] = provider_id
        elif candidate.provider == "kitsu":
            ids["kitsu_id"] = provider_id
        elif candidate.provider == "myanimelist":
            ids["myanimelist_id"] = provider_id
        elif candidate.provider == "imdb":
            ids["imdb_id"] = provider_id
    tmdb_id = _candidate_tmdb_id(candidate)
    if tmdb_id:
        ids.setdefault("tmdb_id", tmdb_id)
    if candidate.imdb_id:
        ids.setdefault("imdb_id", candidate.imdb_id)
    tvdb_id = _candidate_raw_id(candidate, ("tvdb_id", "tvdbId", "tvdbID"))
    if tvdb_id:
        ids.setdefault("tvdb_id", tvdb_id)
    tvmaze_id = _candidate_raw_id(candidate, ("tvmaze_id", "tvmazeId", "tvmazeID"))
    if tvmaze_id:
        ids.setdefault("tvmaze_id", tvmaze_id)
    kitsu_id = _candidate_raw_id(candidate, ("kitsu_id", "kitsuId", "kitsuID"))
    if kitsu_id:
        ids.setdefault("kitsu_id", kitsu_id)
    myanimelist_id = _candidate_raw_id(
        candidate, ("myanimelist_id", "myanimelistId", "myanimelistID")
    )
    if myanimelist_id:
        ids.setdefault("myanimelist_id", myanimelist_id)
    return ids


def _candidate_keys(candidate: MetadataLookupCandidate) -> list[tuple[str, str]]:
    keys: list[tuple[str, str]] = []
    imdb_id = _candidate_imdb_id(candidate)
    if imdb_id:
        keys.append(("imdb", imdb_id))
    for key, value in _candidate_ids(candidate).items():
        if value:
            keys.append((key, value))
    if candidate.provider_item_id:
        keys.append((f"{candidate.provider}_id", candidate.provider_item_id))
    title_key = _normalize_title_key(candidate.title or "")
    if title_key:
        year_key = str(candidate.year) if candidate.year is not None else ""
        media_type = candidate.media_type or ""
        keys.append(("title_year", f"{title_key}:{year_key}:{media_type}"))
    return keys


def _rank_value(candidate: MetadataLookupCandidate) -> int:
    return candidate.rank if candidate.rank is not None else 1_000_000


def _init_candidate_group(
    candidate: MetadataLookupCandidate, keys: list[tuple[str, str]]
) -> dict:
    return {
        "primary": candidate,
        "members": [candidate],
        "member_ids": {candidate.id},
        "providers": {candidate.provider},
        "ids": _candidate_ids(candidate),
        "keys": set(keys),
        "rank": _rank_value(candidate),
        "title": candidate.title,
        "year": candidate.year,
        "media_type": candidate.media_type,
        "poster_url": candidate.poster_url,
    }


def _merge_group_data(target: dict, other: dict) -> None:
    target["members"].extend(other["members"])
    target["member_ids"].update(other["member_ids"])
    target["providers"].update(other["providers"])
    target["keys"].update(other["keys"])
    for key, value in other["ids"].items():
        if value and not target["ids"].get(key):
            target["ids"][key] = value
    if not target["title"] and other["title"]:
        target["title"] = other["title"]
    if target["year"] is None and other["year"] is not None:
        target["year"] = other["year"]
    if not target["media_type"] and other["media_type"]:
        target["media_type"] = other["media_type"]
    if not target["poster_url"] and other["poster_url"]:
        target["poster_url"] = other["poster_url"]
    if other["rank"] < target["rank"]:
        target["rank"] = other["rank"]
        target["primary"] = other["primary"]


def _add_candidate_to_group(
    group: dict, candidate: MetadataLookupCandidate, keys: list[tuple[str, str]]
) -> None:
    group["members"].append(candidate)
    group["member_ids"].add(candidate.id)
    group["providers"].add(candidate.provider)
    group["keys"].update(keys)
    for key, value in _candidate_ids(candidate).items():
        if value and not group["ids"].get(key):
            group["ids"][key] = value
    if not group["title"] and candidate.title:
        group["title"] = candidate.title
    if group["year"] is None and candidate.year is not None:
        group["year"] = candidate.year
    if not group["media_type"] and candidate.media_type:
        group["media_type"] = candidate.media_type
    if not group["poster_url"] and candidate.poster_url:
        group["poster_url"] = candidate.poster_url
    if _rank_value(candidate) < group["rank"]:
        group["rank"] = _rank_value(candidate)
        group["primary"] = candidate


def _merge_lookup_candidates(
    candidates: list[MetadataLookupCandidate],
) -> list[dict]:
    groups: list[dict] = []
    key_to_group: dict[tuple[str, str], dict] = {}
    for candidate in candidates:
        keys = _candidate_keys(candidate)
        matched_groups: list[dict] = []
        seen_groups: set[int] = set()
        for key in keys:
            group = key_to_group.get(key)
            if not group:
                continue
            group_id = id(group)
            if group_id in seen_groups:
                continue
            matched_groups.append(group)
            seen_groups.add(group_id)
        if not matched_groups:
            group = _init_candidate_group(candidate, keys)
            groups.append(group)
        else:
            group = min(matched_groups, key=lambda g: g["rank"])
            for other in matched_groups:
                if other is group:
                    continue
                _merge_group_data(group, other)
                if other in groups:
                    groups.remove(other)
                for key in other["keys"]:
                    key_to_group[key] = group
            _add_candidate_to_group(group, candidate, keys)
        for key in keys:
            key_to_group[key] = group
    groups.sort(key=lambda g: g["rank"])
    return groups


def _candidate_group_to_out(group: dict) -> CandidateOut:
    primary = group["primary"]
    ids = group["ids"]
    return CandidateOut(
        id=primary.id,
        provider=primary.provider,
        provider_item_id=primary.provider_item_id,
        providers=sorted(group["providers"]),
        media_type=group["media_type"] or primary.media_type,
        title=group["title"] or primary.title,
        year=group["year"] if group["year"] is not None else primary.year,
        poster_url=group["poster_url"] or primary.poster_url,
        imdb_id=ids.get("imdb_id"),
        tmdb_id=ids.get("tmdb_id"),
        tvdb_id=ids.get("tvdb_id"),
        tvmaze_id=ids.get("tvmaze_id"),
        kitsu_id=ids.get("kitsu_id"),
        myanimelist_id=ids.get("myanimelist_id"),
    )


def _apply_candidate_ids(item: MediaItem, ids: dict[str, str]) -> None:
    if ids.get("imdb_id") and not item.imdb_id:
        item.imdb_id = ids["imdb_id"]
    if ids.get("tmdb_id") and not item.tmdb_id:
        item.tmdb_id = ids["tmdb_id"]
    if ids.get("tvdb_id") and not item.tvdb_id:
        item.tvdb_id = ids["tvdb_id"]
    if ids.get("tvmaze_id") and not item.tvmaze_id:
        item.tvmaze_id = ids["tvmaze_id"]
    if ids.get("kitsu_id") and not item.kitsu_id:
        item.kitsu_id = ids["kitsu_id"]
    if ids.get("myanimelist_id") and not item.myanimelist_id:
        item.myanimelist_id = ids["myanimelist_id"]


@router.get(
    "/providers",
    summary="List metadata providers",
    description="Return metadata provider status and stored settings for the user.",
)
async def list_providers(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    result = await db.execute(
        select(Integration).where(
            Integration.user_id == current_user.id,
            Integration.provider.in_(METADATA_PROVIDERS),
        )
    )
    integrations = {integration.provider: integration for integration in result.scalars().all()}
    integration_ids = [integration.id for integration in integrations.values()]
    if integration_ids:
        result = await db.execute(
            select(IntegrationSecret.integration_id).where(
                IntegrationSecret.integration_id.in_(integration_ids)
            )
        )
        secret_ids = set(result.scalars().all())
    else:
        secret_ids = set()

    providers: list[dict] = []
    for provider in METADATA_PROVIDERS:
        integration = integrations.get(provider)
        config = integration.config if integration and integration.config else {}
        enabled = bool(config.get("enabled")) if integration else False
        requires_credentials = provider in PROVIDERS_WITH_SECRETS
        has_credentials = False
        if integration:
            has_credentials = integration.id in secret_ids or not requires_credentials
        providers.append(
            ProviderOut(
                provider=provider,
                enabled=enabled,
                config=config,
                has_credentials=has_credentials,
            ).model_dump()
        )
    return {"providers": providers}


@router.post(
    "/providers/tmdb",
    summary="Save TMDB provider settings",
    description="Enable/disable TMDB and store credentials and locale settings.",
)
async def save_tmdb_provider(
    payload: TmdbSettingsIn,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    result = await db.execute(
        select(Integration).where(
            Integration.user_id == current_user.id, Integration.provider == "tmdb"
        )
    )
    integration = result.scalars().first()
    if not integration:
        integration = Integration(
            user_id=current_user.id,
            provider="tmdb",
        )

    language = payload.language.strip() if payload.language else None
    region = payload.region.strip() if payload.region else None
    api_key = payload.api_key.strip() if payload.api_key is not None else None
    if api_key:
        await _validate_tmdb_credentials(api_key, language, region)
    config = dict(integration.config or {})
    config.update(
        {
            "enabled": payload.enabled,
            "language": language,
            "region": region,
        }
    )
    integration.config = config
    integration.status = "enabled" if payload.enabled else "disabled"
    db.add(integration)
    await db.flush()

    has_credentials = False
    if payload.api_key is not None:
        result = await db.execute(
            select(IntegrationSecret).where(
                IntegrationSecret.integration_id == integration.id
            )
        )
        secret = result.scalars().first()
        if api_key:
            encrypted = encrypt_value(json.dumps({"api_key": api_key}))
            if not secret:
                secret = IntegrationSecret(
                    integration_id=integration.id,
                    secret_data=encrypted,
                )
            else:
                secret.secret_data = encrypted
            db.add(secret)
            has_credentials = True
        else:
            if secret:
                await db.delete(secret)
            has_credentials = False
    else:
        result = await db.execute(
            select(IntegrationSecret.integration_id).where(
                IntegrationSecret.integration_id == integration.id
            )
        )
        has_credentials = result.scalar_one_or_none() is not None

    await db.commit()
    return ProviderOut(
        provider="tmdb",
        enabled=payload.enabled,
        config=config,
        has_credentials=has_credentials,
    ).model_dump()


@router.post(
    "/providers/tmdb/test",
    summary="Test TMDB provider",
    description="Validate the stored TMDB credentials.",
)
async def test_tmdb_provider(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    provider = await _load_tmdb_provider(db, current_user.id)
    if not provider:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="TMDB provider is not enabled or missing API key",
        )
    try:
        await provider.validate_credentials()
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY, detail=f"TMDB error: {exc}"
        ) from exc
    return {"status": "ok"}


@router.post(
    "/providers/tvdb",
    summary="Save TVDB provider settings",
    description="Enable/disable TVDB and store credentials and locale settings.",
)
async def save_tvdb_provider(
    payload: TvdbSettingsIn,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    result = await db.execute(
        select(Integration).where(
            Integration.user_id == current_user.id, Integration.provider == "tvdb"
        )
    )
    integration = result.scalars().first()
    if not integration:
        integration = Integration(
            user_id=current_user.id,
            provider="tvdb",
        )

    language = payload.language.strip() if payload.language else None
    api_key = payload.api_key.strip() if payload.api_key is not None else None
    pin = payload.pin.strip() if payload.pin else None
    if api_key:
        await _validate_tvdb_credentials(api_key, pin, language)
    config = dict(integration.config or {})
    config.update(
        {
            "enabled": payload.enabled,
            "language": language,
        }
    )
    integration.config = config
    integration.status = "enabled" if payload.enabled else "disabled"
    db.add(integration)
    await db.flush()

    has_credentials = False
    if payload.api_key is not None:
        result = await db.execute(
            select(IntegrationSecret).where(
                IntegrationSecret.integration_id == integration.id
            )
        )
        secret = result.scalars().first()
        if api_key:
            encrypted = encrypt_value(json.dumps({"api_key": api_key, "pin": pin}))
            if not secret:
                secret = IntegrationSecret(
                    integration_id=integration.id,
                    secret_data=encrypted,
                )
            else:
                secret.secret_data = encrypted
            db.add(secret)
            has_credentials = True
        else:
            if secret:
                await db.delete(secret)
            has_credentials = False
    else:
        result = await db.execute(
            select(IntegrationSecret.integration_id).where(
                IntegrationSecret.integration_id == integration.id
            )
        )
        has_credentials = result.scalar_one_or_none() is not None

    await db.commit()
    return ProviderOut(
        provider="tvdb",
        enabled=payload.enabled,
        config=config,
        has_credentials=has_credentials,
    ).model_dump()


@router.post(
    "/providers/tvdb/test",
    summary="Test TVDB provider",
    description="Validate the stored TVDB credentials.",
)
async def test_tvdb_provider(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    provider = await _load_tvdb_provider(db, current_user.id)
    if not provider:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="TVDB provider is not enabled or missing API key",
        )
    try:
        await provider.validate_credentials()
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY, detail=f"TVDB error: {exc}"
        ) from exc
    return {"status": "ok"}


@router.post(
    "/providers/kitsu",
    summary="Save Kitsu provider settings",
    description="Enable/disable Kitsu and store preferences.",
)
async def save_kitsu_provider(
    payload: KitsuSettingsIn,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    result = await db.execute(
        select(Integration).where(
            Integration.user_id == current_user.id, Integration.provider == "kitsu"
        )
    )
    integration = result.scalars().first()
    if not integration:
        integration = Integration(
            user_id=current_user.id,
            provider="kitsu",
        )

    language = payload.language.strip() if payload.language else None
    config = dict(integration.config or {})
    config.update(
        {
            "enabled": payload.enabled,
            "language": language,
        }
    )
    integration.config = config
    integration.status = "enabled" if payload.enabled else "disabled"
    db.add(integration)
    await db.commit()
    return ProviderOut(
        provider="kitsu",
        enabled=payload.enabled,
        config=config,
        has_credentials=True,
    ).model_dump()


@router.post(
    "/providers/kitsu/test",
    summary="Test Kitsu provider",
    description="Validate the Kitsu provider configuration.",
)
async def test_kitsu_provider(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    provider = await _load_kitsu_provider(db, current_user.id)
    if not provider:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Kitsu provider is not enabled",
        )
    try:
        await provider.validate_credentials()
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY, detail=f"Kitsu error: {exc}"
        ) from exc
    return {"status": "ok"}


@router.post(
    "/providers/tvmaze",
    summary="Save TVMaze provider settings",
    description="Enable/disable TVMaze provider access.",
)
async def save_tvmaze_provider(
    payload: TvmazeSettingsIn,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    result = await db.execute(
        select(Integration).where(
            Integration.user_id == current_user.id, Integration.provider == "tvmaze"
        )
    )
    integration = result.scalars().first()
    if not integration:
        integration = Integration(
            user_id=current_user.id,
            provider="tvmaze",
        )

    config = dict(integration.config or {})
    config.update({"enabled": payload.enabled})
    integration.config = config
    integration.status = "enabled" if payload.enabled else "disabled"
    db.add(integration)
    await db.commit()
    return ProviderOut(
        provider="tvmaze",
        enabled=payload.enabled,
        config=config,
        has_credentials=True,
    ).model_dump()


@router.post(
    "/providers/tvmaze/test",
    summary="Test TVMaze provider",
    description="Validate the TVMaze provider configuration.",
)
async def test_tvmaze_provider(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    provider = await _load_tvmaze_provider(db, current_user.id)
    if not provider:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="TVMaze provider is not enabled",
        )
    try:
        await provider.validate_credentials()
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY, detail=f"TVMaze error: {exc}"
        ) from exc
    return {"status": "ok"}


@router.post(
    "/providers/imdb",
    summary="Save IMDb provider settings",
    description="Enable/disable IMDb provider access.",
)
async def save_imdb_provider(
    payload: ImdbSettingsIn,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    result = await db.execute(
        select(Integration).where(
            Integration.user_id == current_user.id, Integration.provider == "imdb"
        )
    )
    integration = result.scalars().first()
    if not integration:
        integration = Integration(
            user_id=current_user.id,
            provider="imdb",
        )

    config = dict(integration.config or {})
    config.update({"enabled": payload.enabled})
    integration.config = config
    integration.status = "enabled" if payload.enabled else "disabled"
    db.add(integration)
    await db.commit()
    return ProviderOut(
        provider="imdb",
        enabled=payload.enabled,
        config=config,
        has_credentials=True,
    ).model_dump()


@router.post(
    "/providers/imdb/test",
    summary="Test IMDb provider",
    description="Validate the IMDb provider configuration.",
)
async def test_imdb_provider(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    provider = await _load_imdb_provider(db, current_user.id)
    if not provider:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="IMDb provider is not enabled",
        )
    try:
        await provider.validate_credentials()
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY, detail=f"IMDb error: {exc}"
        ) from exc
    return {"status": "ok"}


@router.post(
    "/providers/myanimelist",
    summary="Save MyAnimeList provider settings",
    description="Enable/disable MyAnimeList provider access.",
)
async def save_myanimelist_provider(
    payload: MyAnimeListSettingsIn,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    result = await db.execute(
        select(Integration).where(
            Integration.user_id == current_user.id,
            Integration.provider == "myanimelist",
        )
    )
    integration = result.scalars().first()
    if not integration:
        integration = Integration(
            user_id=current_user.id,
            provider="myanimelist",
        )

    config = dict(integration.config or {})
    config.update({"enabled": payload.enabled})
    integration.config = config
    integration.status = "enabled" if payload.enabled else "disabled"
    db.add(integration)
    await db.commit()
    return ProviderOut(
        provider="myanimelist",
        enabled=payload.enabled,
        config=config,
        has_credentials=True,
    ).model_dump()


@router.post(
    "/providers/myanimelist/test",
    summary="Test MyAnimeList provider",
    description="Validate the MyAnimeList provider configuration.",
)
async def test_myanimelist_provider(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    provider = await _load_myanimelist_provider(db, current_user.id)
    if not provider:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="MyAnimeList provider is not enabled",
        )
    try:
        await provider.validate_credentials()
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"MyAnimeList error: {exc}",
        ) from exc
    return {"status": "ok"}


@router.post(
    "/lookup",
    response_model=LookupCreateOut,
    summary="Create metadata lookup",
    description="Create an asynchronous lookup request for a title or ID.",
)
async def create_lookup(
    payload: LookupCreateIn,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> LookupCreateOut:
    query = payload.query.strip()
    if not query:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="Query is required"
        )
    query_type, normalized = _classify_query(query)
    request = MetadataLookupRequest(
        user_id=current_user.id,
        query=normalized,
        query_type=query_type,
        search_scope=payload.search_scope,
        status="pending",
    )
    db.add(request)
    await db.commit()
    await db.refresh(request)
    return LookupCreateOut(lookup_id=request.id, status=request.status)


@router.get(
    "/lookup/{lookup_id}",
    response_model=LookupStatusOut,
    summary="Get lookup status",
    description="Return lookup status and any resolved candidates.",
)
async def get_lookup_status(
    lookup_id: str,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> LookupStatusOut:
    result = await db.execute(
        select(MetadataLookupRequest).where(
            MetadataLookupRequest.id == lookup_id,
            MetadataLookupRequest.user_id == current_user.id,
        )
    )
    request = result.scalars().first()
    if not request:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Lookup not found")

    result = await db.execute(
        select(MetadataLookupCandidate)
        .where(MetadataLookupCandidate.lookup_request_id == request.id)
        .order_by(MetadataLookupCandidate.rank)
    )
    rows = result.scalars().all()
    candidates = [_candidate_group_to_out(group) for group in _merge_lookup_candidates(rows)]
    return LookupStatusOut(
        lookup_id=request.id,
        status=request.status,
        error=request.error,
        query=request.query,
        query_type=request.query_type,
        search_scope=request.search_scope or "all",
        candidates=candidates,
    )


@router.get(
    "/tv/{provider}/{provider_item_id}/seasons",
    response_model=list[SeasonOut],
    summary="List TV seasons",
    description="List seasons for a TV series using a provider item ID.",
)
async def list_tv_seasons(
    provider: str,
    provider_item_id: str,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> list[SeasonOut]:
    normalized = provider.lower()
    if normalized != "tmdb":
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Episode lookup is only supported for TMDB right now",
        )
    tmdb = await _load_tmdb_provider(db, current_user.id)
    if not tmdb:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="TMDB provider is not enabled for this user",
        )
    seasons = await tmdb.list_seasons(provider_item_id)
    return [
        SeasonOut(
            season_number=season.season_number,
            name=season.name,
            episode_count=season.episode_count,
            air_date=season.air_date,
            poster_url=season.poster_url,
        )
        for season in seasons
    ]


@router.get(
    "/tv/{provider}/{provider_item_id}/seasons/{season_number}/episodes",
    response_model=list[EpisodeOut],
    summary="List TV episodes",
    description="List episodes for a season using a provider item ID.",
)
async def list_tv_episodes(
    provider: str,
    provider_item_id: str,
    season_number: int = Path(..., ge=0),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> list[EpisodeOut]:
    normalized = provider.lower()
    if normalized != "tmdb":
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Episode lookup is only supported for TMDB right now",
        )
    tmdb = await _load_tmdb_provider(db, current_user.id)
    if not tmdb:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="TMDB provider is not enabled for this user",
        )
    episodes = await tmdb.list_episodes(provider_item_id, season_number)
    return [
        EpisodeOut(
            episode_number=episode.episode_number,
            title=episode.title,
            tmdb_id=episode.provider_id,
            air_date=episode.air_date,
            still_url=episode.still_url,
        )
        for episode in episodes
    ]


def _classify_query(query: str) -> tuple[str, str]:
    if IMDB_ID_RE.match(query):
        return "imdb", query.lower()
    if TMDB_ID_RE.match(query):
        return "tmdb", query
    return "title", query


async def _load_tmdb_provider(
    db: AsyncSession, user_id: str
) -> TmdbMetadataProvider | None:
    result = await db.execute(
        select(User.include_adult_in_search).where(User.id == user_id)
    )
    include_adult = result.scalar_one_or_none() or False
    result = await db.execute(
        select(Integration).where(
            Integration.user_id == user_id, Integration.provider == "tmdb"
        )
    )
    integration = result.scalars().first()
    if not integration or not integration.config:
        return None
    if not integration.config.get("enabled"):
        return None

    result = await db.execute(
        select(IntegrationSecret).where(
            IntegrationSecret.integration_id == integration.id
        )
    )
    secret = result.scalars().first()
    if not secret:
        return None
    try:
        data = json.loads(decrypt_value(secret.secret_data))
    except (ValueError, json.JSONDecodeError):
        return None
    api_key = data.get("api_key")
    if not api_key:
        return None

    language = integration.config.get("language")
    region = integration.config.get("region")
    return TmdbMetadataProvider(
        api_key=api_key,
        language=language,
        region=region,
        include_adult=include_adult,
    )


async def _load_tvdb_provider(
    db: AsyncSession, user_id: str
) -> TvdbMetadataProvider | None:
    result = await db.execute(
        select(Integration).where(
            Integration.user_id == user_id, Integration.provider == "tvdb"
        )
    )
    integration = result.scalars().first()
    if not integration or not integration.config:
        return None
    if not integration.config.get("enabled"):
        return None

    result = await db.execute(
        select(IntegrationSecret).where(
            IntegrationSecret.integration_id == integration.id
        )
    )
    secret = result.scalars().first()
    if not secret:
        return None
    try:
        data = json.loads(decrypt_value(secret.secret_data))
    except (ValueError, json.JSONDecodeError):
        return None
    api_key = data.get("api_key")
    pin = data.get("pin")
    if not api_key:
        return None

    language = integration.config.get("language")
    return TvdbMetadataProvider(api_key=api_key, pin=pin, language=language)


async def _load_kitsu_provider(
    db: AsyncSession, user_id: str
) -> KitsuMetadataProvider | None:
    result = await db.execute(
        select(Integration).where(
            Integration.user_id == user_id, Integration.provider == "kitsu"
        )
    )
    integration = result.scalars().first()
    if not integration or not integration.config:
        return None
    if not integration.config.get("enabled"):
        return None

    language = integration.config.get("language")
    return KitsuMetadataProvider(language=language)


async def _load_tvmaze_provider(
    db: AsyncSession, user_id: str
) -> TvmazeMetadataProvider | None:
    result = await db.execute(
        select(Integration).where(
            Integration.user_id == user_id, Integration.provider == "tvmaze"
        )
    )
    integration = result.scalars().first()
    if not integration or not integration.config:
        return None
    if not integration.config.get("enabled"):
        return None
    return TvmazeMetadataProvider()


async def _load_imdb_provider(
    db: AsyncSession, user_id: str
) -> ImdbMetadataProvider | None:
    result = await db.execute(
        select(Integration).where(
            Integration.user_id == user_id, Integration.provider == "imdb"
        )
    )
    integration = result.scalars().first()
    if not integration or not integration.config:
        return None
    if not integration.config.get("enabled"):
        return None
    return ImdbMetadataProvider()


async def _load_myanimelist_provider(
    db: AsyncSession, user_id: str
) -> MyAnimeListMetadataProvider | None:
    result = await db.execute(
        select(Integration).where(
            Integration.user_id == user_id,
            Integration.provider == "myanimelist",
        )
    )
    integration = result.scalars().first()
    if not integration or not integration.config:
        return None
    if not integration.config.get("enabled"):
        return None
    return MyAnimeListMetadataProvider()


async def _load_metadata_provider(
    db: AsyncSession, user_id: str, provider_name: str
) -> MetadataProvider | None:
    if provider_name == "tmdb":
        return await _load_tmdb_provider(db, user_id)
    if provider_name == "tvdb":
        return await _load_tvdb_provider(db, user_id)
    if provider_name == "kitsu":
        return await _load_kitsu_provider(db, user_id)
    if provider_name == "tvmaze":
        return await _load_tvmaze_provider(db, user_id)
    if provider_name == "imdb":
        return await _load_imdb_provider(db, user_id)
    if provider_name == "myanimelist":
        return await _load_myanimelist_provider(db, user_id)
    return None


async def _upsert_media_item(db: AsyncSession, candidate: MediaCandidate) -> MediaItem:
    provider_id = candidate.provider_id or None
    tmdb_id = provider_id if candidate.provider == "tmdb" else None
    tvdb_id = provider_id if candidate.provider == "tvdb" else None
    kitsu_id = provider_id if candidate.provider == "kitsu" else None
    tvmaze_id = provider_id if candidate.provider == "tvmaze" else None
    myanimelist_id = provider_id if candidate.provider == "myanimelist" else None
    imdb_id = candidate.imdb_id or (provider_id if candidate.provider == "imdb" else None)

    item = None
    if tmdb_id:
        result = await db.execute(
            select(MediaItem).where(
                MediaItem.tmdb_id == tmdb_id,
                MediaItem.media_type == candidate.media_type,
            )
        )
        item = result.scalars().first()
    if not item and tvdb_id:
        result = await db.execute(
            select(MediaItem).where(
                MediaItem.tvdb_id == tvdb_id,
                MediaItem.media_type == candidate.media_type,
            )
        )
        item = result.scalars().first()
    if not item and kitsu_id:
        result = await db.execute(
            select(MediaItem).where(
                MediaItem.kitsu_id == kitsu_id,
                MediaItem.media_type == candidate.media_type,
            )
        )
        item = result.scalars().first()
    if not item and tvmaze_id:
        result = await db.execute(
            select(MediaItem).where(
                MediaItem.tvmaze_id == tvmaze_id,
                MediaItem.media_type == candidate.media_type,
            )
        )
        item = result.scalars().first()
    if not item and myanimelist_id:
        result = await db.execute(
            select(MediaItem).where(
                MediaItem.myanimelist_id == myanimelist_id,
                MediaItem.media_type == candidate.media_type,
            )
        )
        item = result.scalars().first()
    if not item and imdb_id:
        result = await db.execute(select(MediaItem).where(MediaItem.imdb_id == imdb_id))
        item = result.scalars().first()

    if not item:
        item = MediaItem(
            media_type=candidate.media_type,
            title=candidate.title,
            year=candidate.year,
            tmdb_id=tmdb_id,
            tvdb_id=tvdb_id,
            kitsu_id=kitsu_id,
            tvmaze_id=tvmaze_id,
            myanimelist_id=myanimelist_id,
            imdb_id=imdb_id,
            poster_url=candidate.poster_url,
            raw=candidate.raw,
        )
    else:
        item.title = candidate.title
        item.year = candidate.year
        item.media_type = candidate.media_type or item.media_type
        item.tmdb_id = tmdb_id or item.tmdb_id
        item.tvdb_id = tvdb_id or item.tvdb_id
        item.kitsu_id = kitsu_id or item.kitsu_id
        item.tvmaze_id = tvmaze_id or item.tvmaze_id
        item.myanimelist_id = myanimelist_id or item.myanimelist_id
        item.imdb_id = imdb_id or item.imdb_id
        item.poster_url = candidate.poster_url or item.poster_url
        item.raw = candidate.raw or item.raw
    db.add(item)
    await db.flush()
    return item


async def _validate_tmdb_credentials(
    api_key: str, language: str | None, region: str | None
) -> None:
    try:
        provider = TmdbMetadataProvider(api_key=api_key, language=language, region=region)
        await provider.validate_credentials()
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code in {401, 403}:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="TMDB API key is invalid",
            ) from exc
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY, detail=f"TMDB error: {exc}"
        ) from exc
    except httpx.RequestError as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="TMDB request failed",
        ) from exc


async def _validate_tvdb_credentials(
    api_key: str, pin: str | None, language: str | None
) -> None:
    try:
        provider = TvdbMetadataProvider(api_key=api_key, pin=pin, language=language)
        await provider.validate_credentials()
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code in {401, 403}:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="TVDB API key or PIN is invalid",
            ) from exc
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY, detail=f"TVDB error: {exc}"
        ) from exc
    except httpx.RequestError as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="TVDB request failed",
        ) from exc
