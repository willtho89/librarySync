from __future__ import annotations

import json
from datetime import datetime, timezone

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from librarysync.connectors.metadata.base import MediaCandidate, MetadataProvider
from librarysync.connectors.metadata.imdb import ImdbMetadataProvider
from librarysync.connectors.metadata.kitsu import KitsuMetadataProvider
from librarysync.connectors.metadata.myanimelist import MyAnimeListMetadataProvider
from librarysync.connectors.metadata.tmdb import TmdbMetadataProvider
from librarysync.connectors.metadata.tvdb import TvdbMetadataProvider
from librarysync.connectors.metadata.tvmaze import TvmazeMetadataProvider
from librarysync.core.security import decrypt_value
from librarysync.db.models import (
    Integration,
    IntegrationSecret,
    MediaItem,
    MetadataLookupCandidate,
    MetadataLookupRequest,
    User,
)
from librarysync.db.session import SessionLocal, init_session_factory

DETAILS_ENRICH_LIMIT = 5
LOCAL_SEARCH_LIMIT = 10
LOCAL_PROVIDER = "local"


async def process_metadata_lookups_once(limit: int = 5) -> int:
    init_session_factory()
    async with SessionLocal() as db:
        requests = await _claim_pending_requests(db, limit)
        if not requests:
            return 0
        for request in requests:
            await _process_request(db, request)
        return len(requests)


async def _claim_pending_requests(
    db: AsyncSession, limit: int
) -> list[MetadataLookupRequest]:
    async with db.begin():
        result = await db.execute(
            select(MetadataLookupRequest)
            .where(MetadataLookupRequest.status == "pending")
            .order_by(MetadataLookupRequest.created_at)
            .limit(limit)
            .with_for_update(skip_locked=True)
        )
        requests = result.scalars().all()
        now = datetime.now(timezone.utc)
        for request in requests:
            request.status = "in_progress"
            request.updated_at = now
    return requests


async def _process_request(db: AsyncSession, request: MetadataLookupRequest) -> None:
    now = datetime.now(timezone.utc)
    try:
        local_candidates = await _lookup_local_candidates(db, request)
        providers = await _load_metadata_providers(db, request.user_id)
        if not providers and not local_candidates:
            await _mark_failed(
                db,
                request,
                "No metadata providers are enabled and no local matches were found",
            )
            return

        candidates: list[MediaCandidate] = []
        provider_names: list[str] = []
        errors: list[str] = []
        for provider in providers:
            provider_names.append(provider.provider)
            try:
                provider_candidates = await _run_lookup(provider, request)
            except Exception as exc:
                errors.append(f"{provider.provider}: {exc}")
                continue
            if provider_candidates:
                candidates.extend(provider_candidates)
        if local_candidates:
            provider_names.append(LOCAL_PROVIDER)
            candidates.extend(local_candidates)
        request.providers = provider_names

        await db.execute(
            delete(MetadataLookupCandidate).where(
                MetadataLookupCandidate.lookup_request_id == request.id
            )
        )
        for idx, candidate in enumerate(candidates, start=1):
            db.add(_candidate_to_model(request.id, candidate, idx))

        if not candidates:
            message = "No matches found"
            if errors:
                message = f"{message}. Provider errors: {', '.join(errors)}"
            await _mark_failed(db, request, message)
            return

        request.status = "completed"
        request.error = None
        request.completed_at = now
        request.updated_at = now
        await db.commit()
    except Exception as exc:
        await _mark_failed(db, request, f"Lookup failed: {exc}")


async def _run_lookup(
    provider: MetadataProvider, request: MetadataLookupRequest
) -> list[MediaCandidate]:
    query = request.query
    search_scope = _normalize_scope(request.search_scope)
    if request.query_type == "imdb":
        candidates = await provider.find_by_external_id(query, search_scope)
        return await _enrich_candidates(provider, candidates)
    if request.query_type == "tmdb":
        if provider.provider != "tmdb":
            return []
        return await _lookup_tmdb_id(provider, query, search_scope)
    candidates = await provider.search(query, search_scope)
    return await _enrich_candidates(provider, candidates)


async def _enrich_candidates(
    provider: MetadataProvider, candidates: list[MediaCandidate]
) -> list[MediaCandidate]:
    enriched: list[MediaCandidate] = []
    for idx, candidate in enumerate(candidates):
        if idx < DETAILS_ENRICH_LIMIT and candidate.provider_id:
            try:
                enriched.append(
                    await provider.get_details(candidate.provider_id, candidate.media_type)
                )
                continue
            except Exception:
                pass
        enriched.append(candidate)
    return enriched


def _candidate_to_model(
    request_id: str, candidate: MediaCandidate, rank: int
) -> MetadataLookupCandidate:
    return MetadataLookupCandidate(
        lookup_request_id=request_id,
        provider=candidate.provider,
        provider_item_id=candidate.provider_id,
        media_type=candidate.media_type,
        title=candidate.title,
        year=candidate.year,
        poster_url=candidate.poster_url,
        imdb_id=candidate.imdb_id,
        rank=rank,
        raw=candidate.raw,
    )


def _normalize_scope(value: str | None) -> str:
    if value in {"movie", "tv", "anime", "all"}:
        return value
    return "all"


async def _lookup_local_candidates(
    db: AsyncSession, request: MetadataLookupRequest
) -> list[MediaCandidate]:
    scope = _normalize_scope(request.search_scope)
    query = request.query
    criteria = []
    if scope != "all":
        criteria.append(MediaItem.media_type == scope)
    if request.query_type == "imdb":
        criteria.append(MediaItem.imdb_id == query)
    elif request.query_type == "tmdb":
        criteria.append(MediaItem.tmdb_id == query)
    else:
        criteria.append(MediaItem.title.ilike(f"%{query}%"))

    result = await db.execute(
        select(MediaItem)
        .where(*criteria)
        .order_by(MediaItem.year.desc(), MediaItem.title)
        .limit(LOCAL_SEARCH_LIMIT)
    )
    items = result.scalars().all()
    return [_media_item_to_candidate(item) for item in items]


def _media_item_to_candidate(item: MediaItem) -> MediaCandidate:
    raw = {
        "source": LOCAL_PROVIDER,
        "media_item_id": item.id,
        "imdb_id": item.imdb_id,
        "tmdb_id": item.tmdb_id,
        "tvdb_id": item.tvdb_id,
        "tvmaze_id": item.tvmaze_id,
        "kitsu_id": item.kitsu_id,
        "myanimelist_id": item.myanimelist_id,
    }
    return MediaCandidate(
        provider=LOCAL_PROVIDER,
        provider_id=item.id,
        media_type=item.media_type,
        title=item.title,
        year=item.year,
        poster_url=item.poster_url,
        imdb_id=item.imdb_id,
        raw=raw,
    )


async def _lookup_tmdb_id(
    provider: TmdbMetadataProvider, tmdb_id: str, scope: str
) -> list[MediaCandidate]:
    if scope == "movie":
        candidate = await provider.get_details(tmdb_id, "movie")
        return [candidate] if candidate.provider_id else []
    if scope == "tv":
        candidate = await provider.get_details(tmdb_id, "tv")
        return [candidate] if candidate.provider_id else []

    last_error: Exception | None = None
    for media_type in ("movie", "tv"):
        try:
            candidate = await provider.get_details(tmdb_id, media_type)
        except Exception as exc:
            last_error = exc
            continue
        if candidate.provider_id:
            return [candidate]
    if last_error:
        raise last_error
    return []


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
    data = json.loads(decrypt_value(secret.secret_data))
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
    data = json.loads(decrypt_value(secret.secret_data))
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
            Integration.user_id == user_id, Integration.provider == "myanimelist"
        )
    )
    integration = result.scalars().first()
    if not integration or not integration.config:
        return None
    if not integration.config.get("enabled"):
        return None
    return MyAnimeListMetadataProvider()


async def _load_metadata_providers(
    db: AsyncSession, user_id: str
) -> list[MetadataProvider]:
    providers: list[MetadataProvider] = []
    tmdb = await _load_tmdb_provider(db, user_id)
    if tmdb:
        providers.append(tmdb)
    tvdb = await _load_tvdb_provider(db, user_id)
    if tvdb:
        providers.append(tvdb)
    tvmaze = await _load_tvmaze_provider(db, user_id)
    if tvmaze:
        providers.append(tvmaze)
    imdb = await _load_imdb_provider(db, user_id)
    if imdb:
        providers.append(imdb)
    kitsu = await _load_kitsu_provider(db, user_id)
    if kitsu:
        providers.append(kitsu)
    myanimelist = await _load_myanimelist_provider(db, user_id)
    if myanimelist:
        providers.append(myanimelist)
    return providers


async def _mark_failed(
    db: AsyncSession, request: MetadataLookupRequest, message: str
) -> None:
    now = datetime.now(timezone.utc)
    request.status = "failed"
    request.error = message[:500]
    request.completed_at = now
    request.updated_at = now
    await db.commit()
