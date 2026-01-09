import re
from typing import Literal

import httpx
from fastapi import APIRouter, Depends, HTTPException, Path, Query, status
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from librarysync.api.deps import get_current_user, get_db
from librarysync.connectors.metadata.base import (
    EpisodeMetadataProvider,
    MediaCandidate,
    ProviderContext,
)
from librarysync.connectors.metadata.tmdb import TmdbMetadataProvider
from librarysync.connectors.metadata.tvdb import TvdbMetadataProvider
from librarysync.core.metadata_providers import (
    METADATA_PROVIDER_REGISTRY,
    AniListProviderSettings,
    ImdbProviderSettings,
    KitsuProviderSettings,
    MetadataProviderService,
    MyAnimeListProviderSettings,
    TmdbProviderSettings,
    TvdbProviderSettings,
    TvmazeProviderSettings,
)
from librarysync.db.models import MediaItem, MetadataLookupCandidate, MetadataLookupRequest, User

router = APIRouter(prefix="/api/metadata", tags=["metadata"])

IMDB_ID_RE = re.compile(r"^tt\d+$", re.IGNORECASE)
TMDB_ID_RE = re.compile(r"^\d+$")


class ProviderOut(BaseModel):
    provider: str
    enabled: bool
    config: dict
    has_credentials: bool


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
    anilist_id: str | None = None


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


class LocalLookupOut(BaseModel):
    query: str
    search_scope: str
    offset: int
    limit: int
    has_more: bool
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
        elif candidate.provider == "anilist":
            ids["anilist_id"] = provider_id
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
    anilist_id = _candidate_raw_id(
        candidate, ("anilist_id", "anilistId", "anilistID")
    )
    if anilist_id:
        ids.setdefault("anilist_id", anilist_id)
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
    _merge_anime_groups(groups)
    groups.sort(key=lambda g: g["rank"])
    return groups


def _merge_anime_groups(groups: list[dict]) -> None:
    anime_providers = {"anilist", "kitsu", "myanimelist"}
    title_map: dict[str, dict | None] = {}
    for group in groups:
        if _is_anime_group(group, anime_providers):
            continue
        key = _group_title_year_key(group)
        if not key:
            continue
        if key in title_map:
            title_map[key] = None
            continue
        title_map[key] = group

    for group in list(groups):
        if not _is_anime_group(group, anime_providers):
            continue
        key = _group_title_year_key(group)
        if not key:
            continue
        target = title_map.get(key)
        if not target or target is group:
            continue
        _merge_anime_into_group(target, group)
        if group in groups:
            groups.remove(group)


def _merge_anime_into_group(target: dict, other: dict) -> None:
    primary = target.get("primary")
    rank = target.get("rank")
    _merge_group_data(target, other)
    if primary is not None:
        target["primary"] = primary
    if rank is not None:
        target["rank"] = rank


def _is_anime_group(group: dict, anime_providers: set[str]) -> bool:
    if group.get("media_type") == "anime":
        return True
    providers = group.get("providers") or set()
    return any(provider in anime_providers for provider in providers)


def _group_title_year_key(group: dict) -> str | None:
    title = group.get("title")
    year = group.get("year")
    if not title or year is None:
        return None
    title_key = _normalize_title_key(title)
    if not title_key:
        return None
    return f"{title_key}:{year}"


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
        anilist_id=ids.get("anilist_id"),
    )


def _media_item_to_candidate_out(item: MediaItem) -> CandidateOut:
    return CandidateOut(
        id=item.id,
        provider="local",
        provider_item_id=item.id,
        providers=["local"],
        media_type=item.media_type,
        title=item.title,
        year=item.year,
        poster_url=item.poster_url,
        imdb_id=item.imdb_id,
        tmdb_id=item.tmdb_id,
        tvdb_id=item.tvdb_id,
        tvmaze_id=item.tvmaze_id,
        kitsu_id=item.kitsu_id,
        myanimelist_id=item.myanimelist_id,
        anilist_id=item.anilist_id,
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
    if ids.get("anilist_id") and not item.anilist_id:
        item.anilist_id = ids["anilist_id"]


@router.get(
    "/providers",
    summary="List metadata providers",
    description="Return metadata provider status and stored settings for the user.",
)
async def list_providers(
    _current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    service = MetadataProviderService(db, current_user.id, METADATA_PROVIDER_REGISTRY)
    states = await service.list_provider_states()
    providers = [
        ProviderOut(
            provider=state.provider,
            enabled=state.enabled,
            config=state.config,
            has_credentials=state.has_credentials,
        ).model_dump()
        for state in states
    ]
    return {"providers": providers}


@router.post(
    "/providers/tmdb",
    summary="Save TMDB provider settings",
    description="Enable/disable TMDB and store credentials and locale settings.",
)
async def save_tmdb_provider(
    payload: TmdbProviderSettings,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    if "api_key" in payload.model_fields_set and payload.api_key:
        await _validate_tmdb_credentials(payload.api_key, payload.language, payload.region)
    service = MetadataProviderService(db, current_user.id, METADATA_PROVIDER_REGISTRY)
    state = await service.save_provider_settings("tmdb", payload)
    return ProviderOut(
        provider=state.provider,
        enabled=state.enabled,
        config=state.config,
        has_credentials=state.has_credentials,
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
    service = MetadataProviderService(db, current_user.id, METADATA_PROVIDER_REGISTRY)
    provider = await service.load_provider("tmdb")
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
    payload: TvdbProviderSettings,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    if "api_key" in payload.model_fields_set and payload.api_key:
        await _validate_tvdb_credentials(payload.api_key, payload.pin, payload.language)
    service = MetadataProviderService(db, current_user.id, METADATA_PROVIDER_REGISTRY)
    state = await service.save_provider_settings("tvdb", payload)
    return ProviderOut(
        provider=state.provider,
        enabled=state.enabled,
        config=state.config,
        has_credentials=state.has_credentials,
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
    service = MetadataProviderService(db, current_user.id, METADATA_PROVIDER_REGISTRY)
    provider = await service.load_provider("tvdb")
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
    payload: KitsuProviderSettings,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    service = MetadataProviderService(db, current_user.id, METADATA_PROVIDER_REGISTRY)
    state = await service.save_provider_settings("kitsu", payload)
    return ProviderOut(
        provider=state.provider,
        enabled=state.enabled,
        config=state.config,
        has_credentials=state.has_credentials,
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
    service = MetadataProviderService(db, current_user.id, METADATA_PROVIDER_REGISTRY)
    provider = await service.load_provider("kitsu")
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
    payload: TvmazeProviderSettings,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    service = MetadataProviderService(db, current_user.id, METADATA_PROVIDER_REGISTRY)
    state = await service.save_provider_settings("tvmaze", payload)
    return ProviderOut(
        provider=state.provider,
        enabled=state.enabled,
        config=state.config,
        has_credentials=state.has_credentials,
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
    service = MetadataProviderService(db, current_user.id, METADATA_PROVIDER_REGISTRY)
    provider = await service.load_provider("tvmaze")
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
    payload: ImdbProviderSettings,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    service = MetadataProviderService(db, current_user.id, METADATA_PROVIDER_REGISTRY)
    state = await service.save_provider_settings("imdb", payload)
    return ProviderOut(
        provider=state.provider,
        enabled=state.enabled,
        config=state.config,
        has_credentials=state.has_credentials,
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
    service = MetadataProviderService(db, current_user.id, METADATA_PROVIDER_REGISTRY)
    provider = await service.load_provider("imdb")
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
    payload: MyAnimeListProviderSettings,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    service = MetadataProviderService(db, current_user.id, METADATA_PROVIDER_REGISTRY)
    state = await service.save_provider_settings("myanimelist", payload)
    return ProviderOut(
        provider=state.provider,
        enabled=state.enabled,
        config=state.config,
        has_credentials=state.has_credentials,
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
    service = MetadataProviderService(db, current_user.id, METADATA_PROVIDER_REGISTRY)
    provider = await service.load_provider("myanimelist")
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
    "/providers/anilist",
    summary="Save AniList provider settings",
    description="Enable/disable AniList provider access.",
)
async def save_anilist_provider(
    payload: AniListProviderSettings,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    service = MetadataProviderService(db, current_user.id, METADATA_PROVIDER_REGISTRY)
    state = await service.save_provider_settings("anilist", payload)
    return ProviderOut(
        provider=state.provider,
        enabled=state.enabled,
        config=state.config,
        has_credentials=state.has_credentials,
    ).model_dump()


@router.post(
    "/providers/anilist/test",
    summary="Test AniList provider",
    description="Validate the AniList provider configuration.",
)
async def test_anilist_provider(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    service = MetadataProviderService(db, current_user.id, METADATA_PROVIDER_REGISTRY)
    provider = await service.load_provider("anilist")
    if not provider:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="AniList provider is not enabled",
        )
    try:
        await provider.validate_credentials()
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"AniList error: {exc}",
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
    "/lookup/local",
    response_model=LocalLookupOut,
    summary="Search local metadata",
    description="Search the local library cache before querying external providers.",
)
async def lookup_local(
    query: str = Query(..., min_length=1, max_length=255),
    search_scope: Literal["all", "movie", "tv", "anime"] = "all",
    offset: int = Query(0, ge=0, le=10_000),
    limit: int = Query(8, ge=1, le=50),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> LocalLookupOut:
    normalized = query.strip()
    if not normalized:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Query is required",
        )
    query_type, normalized = _classify_query(normalized)
    criteria = []
    if search_scope != "all":
        criteria.append(MediaItem.media_type == search_scope)
    if query_type == "imdb":
        criteria.append(MediaItem.imdb_id == normalized)
    elif query_type == "tmdb":
        criteria.append(MediaItem.tmdb_id == normalized)
    else:
        criteria.append(MediaItem.title.ilike(f"%{normalized}%"))

    result = await db.execute(
        select(MediaItem)
        .where(*criteria)
        .order_by(MediaItem.year.desc(), MediaItem.title)
        .offset(offset)
        .limit(limit + 1)
    )
    items = result.scalars().all()
    has_more = len(items) > limit
    if has_more:
        items = items[:limit]
    candidates = [_media_item_to_candidate_out(item) for item in items]
    return LocalLookupOut(
        query=normalized,
        search_scope=search_scope,
        offset=offset,
        limit=limit,
        has_more=has_more,
        candidates=candidates,
    )


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
    service = MetadataProviderService(db, current_user.id, METADATA_PROVIDER_REGISTRY)
    provider_instance = await service.load_provider(normalized)
    if not provider_instance:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Metadata provider is not enabled for this user",
        )
    if not isinstance(provider_instance, EpisodeMetadataProvider):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Episode lookup is not supported for this provider",
        )
    seasons = await provider_instance.list_seasons(provider_item_id)
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
    service = MetadataProviderService(db, current_user.id, METADATA_PROVIDER_REGISTRY)
    provider_instance = await service.load_provider(normalized)
    if not provider_instance:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Metadata provider is not enabled for this user",
        )
    if not isinstance(provider_instance, EpisodeMetadataProvider):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Episode lookup is not supported for this provider",
        )
    episodes = await provider_instance.list_episodes(provider_item_id, season_number)
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
        provider = TmdbMetadataProvider.from_settings(
            {"language": language, "region": region, "include_adult": False},
            {"api_key": api_key},
            ProviderContext(user_id="validation", include_adult=False),
        )
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
        provider = TvdbMetadataProvider.from_settings(
            {"language": language},
            {"api_key": api_key, "pin": pin},
            ProviderContext(user_id="validation", include_adult=False),
        )
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
