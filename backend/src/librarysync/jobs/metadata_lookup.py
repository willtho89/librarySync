from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from librarysync.connectors.metadata.base import MediaCandidate
from librarysync.core.metadata_lookup_engine import LookupRequest, MetadataLookupEngine
from librarysync.core.metadata_providers import MetadataProviderService
from librarysync.db.models import (
    MediaItem,
    MetadataLookupCandidate,
    MetadataLookupRequest,
)
from librarysync.db.session import SessionLocal, init_session_factory

DETAILS_ENRICH_LIMIT = 5
LOCAL_SEARCH_LIMIT = 10
LOCAL_PROVIDER = "local"
LOOKUP_ENGINE = MetadataLookupEngine(detail_limit=DETAILS_ENRICH_LIMIT)


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
        service = MetadataProviderService(db, request.user_id)
        providers = await service.load_enabled_providers()
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
        lookup_request = LookupRequest(
            query=request.query,
            query_type=request.query_type,
            scope=_normalize_scope(request.search_scope),
        )
        for provider in providers:
            provider_names.append(provider.provider)
            try:
                provider_candidates = await LOOKUP_ENGINE.lookup(provider, lookup_request)
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


async def _mark_failed(
    db: AsyncSession, request: MetadataLookupRequest, message: str
) -> None:
    now = datetime.now(timezone.utc)
    request.status = "failed"
    request.error = message[:500]
    request.completed_at = now
    request.updated_at = now
    await db.commit()
