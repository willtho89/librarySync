from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from librarysync.core.scheduler import (
    claim_scheduled_job,
    complete_scheduled_job,
    release_scheduled_job,
)
from librarysync.db.models import (
    MediaItem,
    MetadataLookupCandidate,
    MetadataLookupRequest,
    ScheduledJob,
)
from librarysync.db.session import SessionLocal, init_session_factory

logger = logging.getLogger(__name__)

METADATA_CACHE_JOB = "metadata_cache_refresh"
METADATA_CACHE_INTERVAL = timedelta(days=1)
METADATA_CACHE_LEASE = timedelta(hours=1)
METADATA_CACHE_RETRY_DELAY = timedelta(minutes=15)
METADATA_CACHE_BATCH_SIZE = 200
METADATA_CACHE_LOOKBACK_DAYS = 7
LOCAL_PROVIDER = "local"

PROVIDER_ID_COLUMNS = {
    "tmdb": "tmdb_id",
    "publicmetadb": "tmdb_id",
    "tvdb": "tvdb_id",
    "tvmaze": "tvmaze_id",
    "kitsu": "kitsu_id",
    "myanimelist": "myanimelist_id",
    "anilist": "anilist_id",
    "imdb": "imdb_id",
}


async def process_metadata_cache_refresh_once() -> int:
    init_session_factory()
    async with SessionLocal() as db:
        job = await claim_scheduled_job(
            db,
            METADATA_CACHE_JOB,
            METADATA_CACHE_INTERVAL,
            METADATA_CACHE_LEASE,
        )
        if not job:
            return 0
        try:
            logger.info("Starting metadata cache refresh")
            result = await run_metadata_cache_refresh(db, job)
            logger.info("Metadata cache refresh processed %d entries", result)
        except Exception:
            logger.exception("Metadata cache refresh failed")
            await release_scheduled_job(db, job, METADATA_CACHE_RETRY_DELAY)
            return 0
        await complete_scheduled_job(db, job, METADATA_CACHE_INTERVAL)
    return 1 if result else 0


async def run_metadata_cache_refresh(
    db: AsyncSession,
    _job: ScheduledJob,
    batch_size: int = METADATA_CACHE_BATCH_SIZE,
) -> int:
    cutoff = datetime.now(timezone.utc) - timedelta(days=METADATA_CACHE_LOOKBACK_DAYS)
    query = (
        select(MetadataLookupCandidate)
        .join(
            MetadataLookupRequest,
            MetadataLookupCandidate.lookup_request_id == MetadataLookupRequest.id,
        )
        .where(
            MetadataLookupRequest.status == "completed",
            MetadataLookupRequest.created_at >= cutoff,
            MetadataLookupCandidate.provider != LOCAL_PROVIDER,
        )
        .order_by(MetadataLookupRequest.created_at.desc())
        .limit(batch_size)
    )
    result = await db.execute(query)
    rows = result.scalars().all()
    logger.debug("Metadata cache refresh query returned %d candidates", len(rows))
    if not rows:
        return 0

    processed = 0
    seen: set[frozenset[tuple[str, str]]] = set()
    for candidate in rows:
        ids = _extract_candidate_ids(candidate)
        if not ids:
            continue
        key = frozenset(ids.items())
        if key in seen:
            continue
        seen.add(key)
        existing = await _find_existing_media_item(db, ids)
        if existing:
            continue
        fields = _build_media_item_fields(candidate, ids)
        if not fields:
            continue
        db.add(MediaItem(**fields))
        try:
            await db.commit()
            processed += 1
        except IntegrityError:
            await db.rollback()
            continue

    return processed


def _extract_candidate_ids(candidate: MetadataLookupCandidate) -> dict[str, str]:
    ids: dict[str, str] = {}
    provider = candidate.provider or ""
    provider_key = PROVIDER_ID_COLUMNS.get(provider)
    if provider_key and candidate.provider_item_id:
        ids[provider_key] = candidate.provider_item_id
    if candidate.imdb_id:
        ids.setdefault("imdb_id", candidate.imdb_id)
    raw = _as_dict(candidate.raw)
    for name in ("tmdb_id", "tmdbId", "tmdbID"):
        value = _raw_value(raw, name)
        if value and "tmdb_id" not in ids:
            ids["tmdb_id"] = value
            break
    for name in ("tvdb_id", "tvdbId", "tvdbID"):
        value = _raw_value(raw, name)
        if value and "tvdb_id" not in ids:
            ids["tvdb_id"] = value
            break
    for name in ("tvmaze_id", "tvmazeId", "tvmazeID"):
        value = _raw_value(raw, name)
        if value and "tvmaze_id" not in ids:
            ids["tvmaze_id"] = value
            break
    for name in ("kitsu_id", "kitsuId", "kitsuID"):
        value = _raw_value(raw, name)
        if value and "kitsu_id" not in ids:
            ids["kitsu_id"] = value
            break
    for name in ("myanimelist_id", "myanimelistId", "myanimelistID"):
        value = _raw_value(raw, name)
        if value and "myanimelist_id" not in ids:
            ids["myanimelist_id"] = value
            break
    for name in ("anilist_id", "anilistId", "anilistID", "mal_id"):
        value = _raw_value(raw, name)
        if value and "anilist_id" not in ids:
            ids["anilist_id"] = value
            break
    return {field: str(value) for field, value in ids.items() if value}


def _as_dict(value: object | None) -> dict[str, object]:
    if isinstance(value, dict):
        return dict(value)
    return {}


def _raw_value(raw: dict[str, object], key: str) -> str | None:
    value = raw.get(key)
    if value:
        return str(value)
    nested = raw.get("ids")
    if isinstance(nested, dict):
        nested_value = nested.get(key)
        if nested_value:
            return str(nested_value)
    return None


async def _find_existing_media_item(
    db: AsyncSession, ids: dict[str, str]
) -> MediaItem | None:
    for column_name, value in ids.items():
        column = getattr(MediaItem, column_name, None)
        if column is None:
            continue
        result = await db.execute(select(MediaItem).where(column == value))
        media_item = result.scalars().first()
        if media_item:
            return media_item
    return None


def _build_media_item_fields(
    candidate: MetadataLookupCandidate, ids: dict[str, str]
) -> dict[str, object]:
    title = candidate.title.strip() if candidate.title else ""
    if not title:
        return {}
    data: dict[str, object] = {
        "media_type": candidate.media_type or "movie",
        "title": title,
        "year": candidate.year,
        "poster_url": candidate.poster_url,
        "raw": _merged_raw(candidate),
    }
    data.update(ids)
    return data


def _merged_raw(candidate: MetadataLookupCandidate) -> dict[str, object]:
    raw = _as_dict(candidate.raw)
    raw.setdefault("source", "metadata_cache")
    raw.setdefault("provider", candidate.provider)
    raw.setdefault("lookup_candidate_id", candidate.id)
    return raw
