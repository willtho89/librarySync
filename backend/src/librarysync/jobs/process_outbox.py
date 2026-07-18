"""Process outbox jobs for downstream services."""

from __future__ import annotations

import json
import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Any

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from librarysync.config import settings
from librarysync.connectors.services.anilist import (
    AniListClient,
    AniListError,
    convert_rating_to_anilist_scale,
    has_required_anilist_fields,
)
from librarysync.connectors.services.letterboxd import (
    DEFAULT_LETTERBOXD_API_BASE_URL,
    LetterboxdClient,
    LetterboxdError,
    extract_member_id,
    extract_member_name,
    has_required_letterboxd_fields,
)
from librarysync.connectors.services.letterboxd import (
    is_token_expired as is_letterboxd_token_expired,
)
from librarysync.connectors.services.letterboxd import (
    parse_expires_at as parse_letterboxd_expires_at,
)
from librarysync.connectors.services.letterboxd import (
    token_to_secret_payload as letterboxd_token_to_secret_payload,
)
from librarysync.connectors.services.publicmetadb import (
    PublicMetaDbClient,
    PublicMetaDbError,
    has_required_publicmetadb_fields,
)
from librarysync.connectors.services.simkl import (
    SimklClient,
    SimklError,
    has_required_simkl_fields,
)
from librarysync.connectors.services.simkl import (
    is_token_expired as is_simkl_token_expired,
)
from librarysync.connectors.services.simkl import (
    parse_expires_at as parse_simkl_expires_at,
)
from librarysync.connectors.services.simkl import (
    token_to_secret_payload as simkl_token_to_secret_payload,
)
from librarysync.connectors.services.stremio import (
    DEFAULT_STREMIO_API_BASE_URL,
    StremioClient,
    StremioError,
    fetch_cinemeta_video_ids,
    has_required_stremio_fields,
)
from librarysync.connectors.services.stremio_watched_bitfield import (
    WatchedBitFieldError,
    watched_bitfield_from_array,
)
from librarysync.connectors.services.trakt import (
    TraktClient,
    TraktError,
    has_required_trakt_fields,
    is_token_expired,
    parse_expires_at,
    token_to_secret_payload,
)
from librarysync.core.import_control import load_blocked_outbox_users
from librarysync.core.integrations import load_integration_with_secrets
from librarysync.core.publicmetadb import is_publicmetadb_sync_enabled
from librarysync.core.rate_limiter import RATE_LIMITER
from librarysync.core.ratings import coerce_star_rating
from librarysync.core.security import encrypt_value
from librarysync.core.watch_pipeline import process_new_item_job, process_watchlist_update_job
from librarysync.db.models import (
    EpisodeItem,
    Integration,
    IntegrationSecret,
    MediaItem,
    OutboxJob,
    SyncAttempt,
    WatchedItem,
    WatchSync,
)
from librarysync.db.session import SessionLocal, init_session_factory

RETRYABLE_STATUSES = ("pending", "failed_retryable")
BATCHABLE_PROVIDERS = {"trakt", "simkl"}
BATCHABLE_JOB_TYPES = {"push_watched", "push_rating"}
MIXED_PROVIDER_ORDER = ("trakt", "simkl", "publicmetadb", "letterboxd", "stremio")
logger = logging.getLogger(__name__)


def _get_provider_batch_sizes() -> dict[str, int]:
    """Get provider batch sizes from settings.

    Returns a dictionary mapping provider names to their configured maximum
    batch sizes. Called during batch processing to retrieve current settings.
    """
    return {
        "trakt": settings.trakt_max_batch_size,
        "simkl": settings.simkl_max_batch_size,
    }


@dataclass(frozen=True)
class DeliveryResult:
    response_code: int | None
    external_id: str | None
    resolved_rewatch: bool | None = None


class OutboxHandler(ABC):
    provider: str

    @abstractmethod
    async def deliver(self, db: AsyncSession, job: OutboxJob) -> DeliveryResult:
        raise NotImplementedError


class OutboxHandlerRegistry:
    def __init__(self, handlers: list[OutboxHandler]) -> None:
        self._handlers = {handler.provider: handler for handler in handlers}

    def get(self, provider: str) -> OutboxHandler | None:
        return self._handlers.get(provider)


class OutboxDispatcher:
    def __init__(self, registry: OutboxHandlerRegistry) -> None:
        self._registry = registry

    async def deliver(self, db: AsyncSession, job: OutboxJob) -> DeliveryResult:
        handler = self._registry.get(job.target_provider)
        if not handler:
            raise ValueError(f"Unsupported outbox job {job.target_provider}:{job.job_type}")
        return await handler.deliver(db, job)


class LetterboxdOutboxHandler(OutboxHandler):
    provider = "letterboxd"

    async def deliver(self, db: AsyncSession, job: OutboxJob) -> DeliveryResult:
        if job.job_type == "push_watched":
            response_code, external_id, resolved_rewatch = await _deliver_letterboxd_watch(db, job)
            return DeliveryResult(response_code, external_id, resolved_rewatch)
        if job.job_type == "push_rating":
            response_code, external_id, resolved_rewatch = await _deliver_letterboxd_watch(
                db, job, force_update_rating=True
            )
            return DeliveryResult(response_code, external_id, resolved_rewatch)
        if job.job_type == "push_watchlist":
            response_code, external_id = await _deliver_letterboxd_watchlist(db, job)
            return DeliveryResult(response_code, external_id)
        if job.job_type == "remove_watchlist":
            response_code, external_id = await _deliver_letterboxd_watchlist_remove(db, job)
            return DeliveryResult(response_code, external_id)
        if job.job_type == "update_log_entry":
            response_code, external_id = await _deliver_letterboxd_log_update(db, job)
            return DeliveryResult(response_code, external_id)
        if job.job_type == "delete_log_entry":
            response_code, external_id = await _deliver_letterboxd_delete(db, job)
            return DeliveryResult(response_code, external_id)
        raise ValueError(f"Unsupported outbox job {job.target_provider}:{job.job_type}")


class TraktOutboxHandler(OutboxHandler):
    provider = "trakt"

    async def deliver(self, db: AsyncSession, job: OutboxJob) -> DeliveryResult:
        if job.job_type == "push_watched":
            response_code, external_id = await _deliver_trakt_watch(db, job)
            return DeliveryResult(response_code, external_id)
        if job.job_type == "push_rating":
            response_code, external_id = await _deliver_trakt_rating(db, job)
            return DeliveryResult(response_code, external_id)
        if job.job_type == "push_watchlist":
            response_code, external_id = await _deliver_trakt_watchlist(db, job)
            return DeliveryResult(response_code, external_id)
        if job.job_type == "remove_watchlist":
            response_code, external_id = await _deliver_trakt_watchlist_remove(db, job)
            return DeliveryResult(response_code, external_id)
        if job.job_type == "update_history":
            response_code, external_id = await _deliver_trakt_update(db, job)
            return DeliveryResult(response_code, external_id)
        if job.job_type == "remove_history":
            response_code, external_id = await _deliver_trakt_remove(db, job)
            return DeliveryResult(response_code, external_id)
        raise ValueError(f"Unsupported outbox job {job.target_provider}:{job.job_type}")


class SimklOutboxHandler(OutboxHandler):
    provider = "simkl"

    async def deliver(self, db: AsyncSession, job: OutboxJob) -> DeliveryResult:
        if job.job_type == "push_watched":
            response_code, external_id = await _deliver_simkl_watch(db, job)
            return DeliveryResult(response_code, external_id)
        if job.job_type == "push_rating":
            response_code, external_id = await _deliver_simkl_rating(db, job)
            return DeliveryResult(response_code, external_id)
        if job.job_type == "push_watchlist":
            response_code, external_id = await _deliver_simkl_watchlist(db, job)
            return DeliveryResult(response_code, external_id)
        if job.job_type == "remove_watchlist":
            response_code, external_id = await _deliver_simkl_watchlist_remove(db, job)
            return DeliveryResult(response_code, external_id)
        if job.job_type == "update_history":
            response_code, external_id = await _deliver_simkl_update(db, job)
            return DeliveryResult(response_code, external_id)
        if job.job_type == "remove_history":
            response_code, external_id = await _deliver_simkl_remove(db, job)
            return DeliveryResult(response_code, external_id)
        raise ValueError(f"Unsupported outbox job {job.target_provider}:{job.job_type}")


class StremioOutboxHandler(OutboxHandler):
    provider = "stremio"

    async def deliver(self, db: AsyncSession, job: OutboxJob) -> DeliveryResult:
        if job.job_type == "push_watched":
            response_code, external_id = await _deliver_stremio_watch(db, job)
            return DeliveryResult(response_code, external_id)
        if job.job_type == "remove_watched":
            response_code, external_id = await _deliver_stremio_remove(db, job)
            return DeliveryResult(response_code, external_id)
        raise ValueError(f"Unsupported outbox job {job.target_provider}:{job.job_type}")


class PublicMetaDbOutboxHandler(OutboxHandler):
    provider = "publicmetadb"

    async def deliver(self, db: AsyncSession, job: OutboxJob) -> DeliveryResult:
        if job.job_type == "push_watched":
            response_code, external_id = await _deliver_publicmetadb_watch(db, job)
            return DeliveryResult(response_code, external_id)
        if job.job_type == "push_rating":
            response_code, external_id = await _deliver_publicmetadb_rating(db, job)
            return DeliveryResult(response_code, external_id)
        if job.job_type == "push_watchlist":
            response_code, external_id = await _deliver_publicmetadb_watchlist(db, job)
            return DeliveryResult(response_code, external_id)
        if job.job_type == "remove_watchlist":
            response_code, external_id = await _deliver_publicmetadb_watchlist_remove(db, job)
            return DeliveryResult(response_code, external_id)
        if job.job_type == "update_history":
            response_code, external_id = await _deliver_publicmetadb_update(db, job)
            return DeliveryResult(response_code, external_id)
        if job.job_type == "remove_history":
            response_code, external_id = await _deliver_publicmetadb_remove(db, job)
            return DeliveryResult(response_code, external_id)
        raise ValueError(f"Unsupported outbox job {job.target_provider}:{job.job_type}")


class AniListOutboxHandler(OutboxHandler):
    provider = "anilist"

    async def deliver(self, db: AsyncSession, job: OutboxJob) -> DeliveryResult:
        if job.job_type == "push_watched":
            response_code, external_id = await _deliver_anilist_watch(db, job)
            return DeliveryResult(response_code, external_id)
        if job.job_type == "push_rating":
            response_code, external_id = await _deliver_anilist_rating(db, job)
            return DeliveryResult(response_code, external_id)
        if job.job_type == "remove_history":
            response_code, external_id = await _deliver_anilist_remove(db, job)
            return DeliveryResult(response_code, external_id)
        raise ValueError(f"Unsupported outbox job {job.target_provider}:{job.job_type}")


class InternalOutboxHandler(OutboxHandler):
    provider = "internal"

    async def deliver(self, db: AsyncSession, job: OutboxJob) -> DeliveryResult:
        if job.job_type == "new_item_added":
            await process_new_item_job(db, job)
            return DeliveryResult(None, None, None)
        if job.job_type == "watchlist_update":
            await process_watchlist_update_job(db, job)
            return DeliveryResult(None, None, None)
        raise ValueError(f"Unsupported outbox job {job.target_provider}:{job.job_type}")


OUTBOX_HANDLER_REGISTRY = OutboxHandlerRegistry(
    [
        LetterboxdOutboxHandler(),
        TraktOutboxHandler(),
        SimklOutboxHandler(),
        PublicMetaDbOutboxHandler(),
        StremioOutboxHandler(),
        AniListOutboxHandler(),
        InternalOutboxHandler(),
    ]
)
OUTBOX_DISPATCHER = OutboxDispatcher(OUTBOX_HANDLER_REGISTRY)


async def _deliver_anilist_watch(db: AsyncSession, job: OutboxJob) -> tuple[int | None, str | None]:
    """Deliver watched item to AniList."""
    payload = job.payload or {}
    anilist_id = payload.get("anilist_id")
    watched_at = _parse_datetime(payload.get("watched_at"))
    rating = payload.get("rating")
    is_episode = payload.get("is_episode", False)
    episode_number = payload.get("episode_number")

    if not anilist_id:
        raise AniListError("AniList ID is required")

    integration, secret_data = await load_integration_with_secrets(db, job.user_id, "anilist")
    if not integration or not secret_data:
        raise AniListError("AniList credentials are missing", status_code=401)
    if not has_required_anilist_fields(secret_data):
        raise AniListError("AniList credentials are incomplete", status_code=401)

    access_token = secret_data.get("access_token")
    if not isinstance(access_token, str) or not access_token:
        raise AniListError("AniList access token is missing", status_code=401)

    client = AniListClient(access_token=access_token)

    # Get viewer info to get user ID
    viewer = await client.get_viewer()
    user_id = viewer.get("id")
    if not user_id:
        raise AniListError("Failed to get AniList user ID")

    # Convert rating from 0.5-5.0 to 0-10 scale for AniList
    anilist_score = convert_rating_to_anilist_scale(rating)

    # For episodes, use CURRENT status and set progress
    # For movies or completed series, use COMPLETED status
    if is_episode and episode_number is not None:
        # For TV shows, set progress to the episode number and status to CURRENT
        result = await client.add_media_list_entry(
            media_id=int(anilist_id),
            status="CURRENT",
            score=anilist_score,
            progress=int(episode_number),
            started_at=watched_at,
        )
    else:
        # For movies, mark as completed
        result = await client.add_media_list_entry(
            media_id=int(anilist_id),
            status="COMPLETED",
            score=anilist_score,
            completed_at=watched_at,
        )

    entry_id = result.get("id")
    external_id = str(entry_id) if entry_id else None

    return 200, external_id


async def _deliver_anilist_rating(
    db: AsyncSession, job: OutboxJob
) -> tuple[int | None, str | None]:
    """Update rating for an existing AniList entry."""
    payload = job.payload or {}
    entry_id = payload.get("entry_id")
    rating = payload.get("rating")
    anilist_id = payload.get("anilist_id")

    if not entry_id:
        raise AniListError("AniList entry ID is required for rating update")
    if not anilist_id:
        raise AniListError("AniList media ID is required")

    integration, secret_data = await load_integration_with_secrets(db, job.user_id, "anilist")
    if not integration or not secret_data:
        raise AniListError("AniList credentials are missing", status_code=401)
    if not has_required_anilist_fields(secret_data):
        raise AniListError("AniList credentials are incomplete", status_code=401)

    access_token = secret_data.get("access_token")
    if not isinstance(access_token, str) or not access_token:
        raise AniListError("AniList access token is missing", status_code=401)

    client = AniListClient(access_token=access_token)

    # Convert rating from 0.5-5.0 to 0-10 scale
    anilist_score = convert_rating_to_anilist_scale(rating)

    # Update the existing entry
    await client.add_media_list_entry(
        media_id=int(anilist_id),
        status="COMPLETED",
        score=anilist_score,
    )

    return 200, str(entry_id)


async def _deliver_anilist_remove(
    db: AsyncSession, job: OutboxJob
) -> tuple[int | None, str | None]:
    """Remove an entry from AniList."""
    payload = job.payload or {}
    entry_id = payload.get("entry_id")
    anilist_id = payload.get("anilist_id")

    if not entry_id and not anilist_id:
        raise AniListError("AniList entry ID or media ID is required for removal")

    integration, secret_data = await load_integration_with_secrets(db, job.user_id, "anilist")
    if not integration or not secret_data:
        raise AniListError("AniList credentials are missing", status_code=401)
    if not has_required_anilist_fields(secret_data):
        raise AniListError("AniList credentials are incomplete", status_code=401)

    access_token = secret_data.get("access_token")
    if not isinstance(access_token, str) or not access_token:
        raise AniListError("AniList access token is missing", status_code=401)

    client = AniListClient(access_token=access_token)

    if not entry_id:
        viewer = await client.get_viewer()
        user_id = viewer.get("id")
        if not user_id:
            raise AniListError("Failed to get AniList user ID")
        if not anilist_id:
            raise AniListError("AniList media ID is required for lookup")
        entry = await client.get_media_list_entry(int(anilist_id), int(user_id))
        if not entry or not entry.get("id"):
            return 200, None
        entry_id = entry.get("id")

    # Delete the entry
    success = await client.delete_media_list_entry(int(entry_id))

    if not success:
        raise AniListError("Failed to delete AniList entry")

    return 200, None


async def process_outbox_once(limit: int = 50) -> int:
    init_session_factory()
    async with SessionLocal() as db:
        jobs = await _claim_jobs(db, limit)
        if not jobs:
            return 0
        logger.info("Processing %s outbox job(s)", len(jobs))
        batch_groups, remaining = _group_batchable_jobs(jobs)
        for group in batch_groups:
            await _process_job_batch(db, group)
        for job in remaining:
            await _process_job(db, job)
        return len(jobs)


def _group_batchable_jobs(jobs: list[OutboxJob]) -> tuple[list[list[OutboxJob]], list[OutboxJob]]:
    grouped: dict[tuple[str, str, str], list[OutboxJob]] = {}
    remaining: list[OutboxJob] = []
    for job in jobs:
        if job.target_provider in BATCHABLE_PROVIDERS and job.job_type in BATCHABLE_JOB_TYPES:
            key = (job.user_id, job.target_provider, job.job_type)
            grouped.setdefault(key, []).append(job)
        else:
            remaining.append(job)
    batch_groups: list[list[OutboxJob]] = []
    for (user_id, provider, job_type), group in grouped.items():
        if len(group) < 2:
            remaining.extend(group)
            continue
        group.sort(key=lambda entry: entry.created_at)
        # Split large batches based on provider-specific limits
        max_batch_size = _get_provider_max_batch_size(provider)
        for batch in _chunk_jobs(group, max_batch_size):
            if len(batch) >= 2:
                batch_groups.append(batch)
            else:
                remaining.extend(batch)
    batch_groups.sort(key=lambda group: group[0].created_at)
    remaining.sort(key=lambda entry: entry.created_at)
    return batch_groups, remaining


def _get_provider_max_batch_size(provider: str) -> int:
    """Get the maximum batch size for a provider."""
    batch_sizes = _get_provider_batch_sizes()
    return batch_sizes.get(provider, 1000)  # Default fallback


def _chunk_jobs(jobs: list[OutboxJob], chunk_size: int) -> list[list[OutboxJob]]:
    """Split a list of jobs into chunks of a maximum size."""
    if chunk_size <= 0:
        chunk_size = 1000
    chunks: list[list[OutboxJob]] = []
    for i in range(0, len(jobs), chunk_size):
        chunks.append(jobs[i : i + chunk_size])
    return chunks


def _mixed_provider_limits(limit: int, providers: tuple[str, ...]) -> dict[str, int]:
    if limit <= 0 or not providers:
        return {}
    per_provider = limit // len(providers)
    remainder = limit % len(providers)
    limits: dict[str, int] = {}
    for idx, provider in enumerate(providers):
        quota = per_provider + (1 if idx < remainder else 0)
        if quota > 0:
            limits[provider] = quota
    return limits


async def _process_job_batch(db: AsyncSession, jobs: list[OutboxJob]) -> None:
    if not jobs:
        return
    now = datetime.now(timezone.utc)
    for job in jobs:
        job.attempts += 1
    status = "succeeded"
    response_code: int | None = None
    error_message: str | None = None

    try:
        response_code = await _deliver_batch(db, jobs)
    except LetterboxdError as exc:
        response_code = exc.status_code
        error_message = _format_letterboxd_error(exc)
        status = _classify_failure(exc.status_code, error_message)
    except TraktError as exc:
        response_code = exc.status_code
        error_message = _format_trakt_error(exc)
        status = _classify_failure(exc.status_code, error_message)
    except SimklError as exc:
        response_code = exc.status_code
        error_message = _format_simkl_error(exc)
        status = _classify_failure(exc.status_code, error_message)
    except PublicMetaDbError as exc:
        response_code = exc.status_code
        error_message = _format_publicmetadb_error(exc)
        status = _classify_failure(exc.status_code, error_message)
    except StremioError as exc:
        response_code = exc.status_code
        error_message = _format_stremio_error(exc)
        status = _classify_failure(exc.status_code, error_message)
    except AniListError as exc:
        response_code = exc.status_code
        error_message = _format_anilist_error(exc)
        status = _classify_failure(exc.status_code, error_message)
    except ValueError as exc:
        error_message = str(exc)
        status = "failed_permanent"
    except Exception as exc:
        error_message = str(exc)
        status = "failed_retryable"

    for job in jobs:
        job.status = status
        job.last_error = error_message
        if status == "failed_retryable":
            job.run_after = now + _next_retry_delay(job.attempts)
        else:
            job.run_after = None
        job.updated_at = now
        await _update_watch_sync(
            db,
            job,
            status,
            error_message,
            None,
            now,
        )
        db.add(
            SyncAttempt(
                job_id=job.id,
                status=status,
                response_code=response_code,
                error=error_message,
            )
        )
        logger.info(
            "Outbox job %s %s -> %s (attempt %s)",
            job.id,
            f"{job.target_provider}:{job.job_type}",
            status,
            job.attempts,
        )
        if error_message:
            logger.warning("Outbox job %s error: %s", job.id, error_message)
    await db.commit()


async def _deliver_batch(db: AsyncSession, jobs: list[OutboxJob]) -> int | None:
    job = jobs[0]
    if job.target_provider == "trakt":
        if job.job_type == "push_watched":
            return await _deliver_trakt_watch_batch(db, jobs)
        if job.job_type == "push_rating":
            return await _deliver_trakt_rating_batch(db, jobs)
    if job.target_provider == "simkl":
        if job.job_type == "push_watched":
            return await _deliver_simkl_watch_batch(db, jobs)
        if job.job_type == "push_rating":
            return await _deliver_simkl_rating_batch(db, jobs)
    raise ValueError(f"Unsupported outbox job batch {job.target_provider}:{job.job_type}")


async def _claim_jobs(db: AsyncSession, limit: int) -> list[OutboxJob]:
    now = datetime.now(timezone.utc)
    async with db.begin():
        if limit <= 0:
            return []
        blocked_users = await load_blocked_outbox_users(db)
        filters = [
            OutboxJob.status.in_(RETRYABLE_STATUSES),
            or_(OutboxJob.run_after.is_(None), OutboxJob.run_after <= now),
        ]
        if blocked_users:
            filters.append(~OutboxJob.user_id.in_(blocked_users))
        base_query = (
            select(OutboxJob)
            .where(*filters)
            .order_by(OutboxJob.user_id, OutboxJob.created_at)
            .with_for_update(skip_locked=True)
        )
        jobs: list[OutboxJob] = []
        for provider, provider_limit in _mixed_provider_limits(limit, MIXED_PROVIDER_ORDER).items():
            result = await db.execute(
                base_query.where(OutboxJob.target_provider == provider).limit(provider_limit)
            )
            jobs.extend(result.scalars().all())
        remaining_limit = limit - len(jobs)
        if remaining_limit > 0:
            claimed_ids = [job.id for job in jobs]
            remainder_filters = []
            if claimed_ids:
                remainder_filters.append(~OutboxJob.id.in_(claimed_ids))
            result = await db.execute(base_query.where(*remainder_filters).limit(remaining_limit))
            jobs.extend(result.scalars().all())
        for job in jobs:
            job.status = "in_progress"
            job.updated_at = now
    return jobs


async def _process_job(db: AsyncSession, job: OutboxJob) -> None:
    now = datetime.now(timezone.utc)
    rate_decision = await RATE_LIMITER.try_acquire(
        db,
        job.user_id,
        job.target_provider,
        now=now,
    )
    if rate_decision and not rate_decision.allowed:
        job.status = "pending"
        job.last_error = "rate_limited"
        job.run_after = rate_decision.retry_at
        job.updated_at = now
        await _update_watch_sync(
            db,
            job,
            "pending",
            job.last_error,
            None,
            now,
        )
        await db.commit()
        logger.info(
            "Outbox job %s %s rate-limited until %s",
            job.id,
            f"{job.target_provider}:{job.job_type}",
            rate_decision.retry_at.isoformat() if rate_decision.retry_at else "unknown",
        )
        return
    job.attempts += 1
    status = "succeeded"
    response_code: int | None = None
    error_message: str | None = None
    external_id: str | None = None
    resolved_rewatch: bool | None = None

    try:
        result = await OUTBOX_DISPATCHER.deliver(db, job)
        response_code = result.response_code
        external_id = result.external_id
        resolved_rewatch = result.resolved_rewatch
    except LetterboxdError as exc:
        response_code = exc.status_code
        error_message = _format_letterboxd_error(exc)
        status = _classify_failure(exc.status_code, error_message)
    except TraktError as exc:
        response_code = exc.status_code
        error_message = _format_trakt_error(exc)
        status = _classify_failure(exc.status_code, error_message)
    except SimklError as exc:
        response_code = exc.status_code
        error_message = _format_simkl_error(exc)
        status = _classify_failure(exc.status_code, error_message)
    except PublicMetaDbError as exc:
        response_code = exc.status_code
        error_message = _format_publicmetadb_error(exc)
        status = _classify_failure(exc.status_code, error_message)
    except StremioError as exc:
        response_code = exc.status_code
        error_message = _format_stremio_error(exc)
        status = _classify_failure(exc.status_code, error_message)
    except AniListError as exc:
        response_code = exc.status_code
        error_message = _format_anilist_error(exc)
        status = _classify_failure(exc.status_code, error_message)
    except ValueError as exc:
        error_message = str(exc)
        status = "failed_permanent"
    except Exception as exc:
        error_message = str(exc)
        status = "failed_retryable"

    job.status = status
    job.last_error = error_message
    if status == "failed_retryable":
        job.run_after = now + _next_retry_delay(job.attempts)
    else:
        job.run_after = None
    if status in {"succeeded", "failed_permanent"}:
        job.dedupe_key = None
    job.updated_at = now

    await _update_watch_sync(
        db,
        job,
        status,
        error_message,
        external_id,
        now,
        resolved_rewatch,
    )
    logger.info(
        "Outbox job %s %s -> %s (attempt %s)",
        job.id,
        f"{job.target_provider}:{job.job_type}",
        status,
        job.attempts,
    )
    if error_message:
        logger.warning("Outbox job %s error: %s", job.id, error_message)
    db.add(
        SyncAttempt(
            job_id=job.id,
            status=status,
            response_code=response_code,
            error=error_message,
        )
    )
    await db.commit()


async def _deliver_letterboxd_watch(
    db: AsyncSession, job: OutboxJob, force_update_rating: bool = False
) -> tuple[int | None, str | None, bool | None]:
    payload = job.payload or {}
    imdb_id = payload.get("imdb_id")
    tmdb_id = payload.get("tmdb_id")
    rating = coerce_star_rating(payload.get("rating"))
    tags = _normalize_tags(payload.get("tags"))
    like = _normalize_like(payload.get("like"))
    watched_at_raw = payload.get("watched_at")
    is_rewatch = bool(payload.get("is_rewatch"))
    entry_id = payload.get("entry_id")
    force_update_rating = force_update_rating or bool(payload.get("force_update_rating"))
    imdb_id = imdb_id.lower() if isinstance(imdb_id, str) and imdb_id else None
    tmdb_id = str(tmdb_id).strip() if tmdb_id is not None else None
    if tmdb_id == "":
        tmdb_id = None
    if not imdb_id and not tmdb_id:
        raise ValueError("Letterboxd sync requires an IMDb or TMDB ID")

    watched_at = _parse_datetime(watched_at_raw)
    integration, secret_data = await load_integration_with_secrets(db, job.user_id, "letterboxd")
    if not integration or not secret_data:
        raise LetterboxdError("Letterboxd credentials are missing", status_code=401)
    if not has_required_letterboxd_fields(secret_data):
        raise LetterboxdError("Letterboxd credentials are incomplete", status_code=401)
    api_base_url = DEFAULT_LETTERBOXD_API_BASE_URL
    if integration.config and integration.config.get("api_base_url"):
        api_base_url = str(integration.config["api_base_url"])
    client = LetterboxdClient(
        api_base_url=api_base_url,
        client_id=str(secret_data.get("client_id")),
        client_secret=str(secret_data.get("client_secret")),
        refresh_token=str(secret_data.get("refresh_token")),
        cookies=_safe_cookies(secret_data.get("cookies")),
    )
    access_token = await _ensure_letterboxd_access_token(db, integration.id, secret_data, client)
    if force_update_rating and rating is not None and entry_id:
        _, response_code = await client.update_log_entry_rating(
            str(entry_id), rating, access_token=access_token
        )
        return response_code, str(entry_id), None
    film_id = await client.resolve_film_id(access_token, imdb_id, tmdb_id)
    member_id = await _ensure_letterboxd_member(
        db,
        integration,
        client,
        access_token,
    )

    log_check = await client.check_log_entries_for_date(
        access_token,
        film_id,
        watched_at.date(),
        member_id=member_id,
    )
    if log_check.already_logged_today:
        if force_update_rating and rating is not None and log_check.entry_id:
            _, response_code = await client.update_log_entry_rating(
                log_check.entry_id,
                rating,
                access_token=access_token,
            )
            return response_code, log_check.entry_id, None
        return 200, log_check.entry_id, None
    effective_rewatch = is_rewatch or log_check.has_any_entries
    response, response_code = await client.log_watch(
        imdb_id=imdb_id,
        tmdb_id=tmdb_id,
        watched_at=watched_at,
        rewatch=effective_rewatch,
        rating=rating,
        tags=tags,
        like=like,
        access_token=access_token,
        film_id=film_id,
    )
    external_id = _extract_entry_id(response)
    return response_code, external_id, effective_rewatch


async def _deliver_letterboxd_watchlist(
    db: AsyncSession, job: OutboxJob
) -> tuple[int | None, str | None]:
    return await _deliver_letterboxd_watchlist_change(db, job, in_watchlist=True)


async def _deliver_letterboxd_watchlist_remove(
    db: AsyncSession, job: OutboxJob
) -> tuple[int | None, str | None]:
    return await _deliver_letterboxd_watchlist_change(db, job, in_watchlist=False)


async def _deliver_letterboxd_watchlist_change(
    db: AsyncSession, job: OutboxJob, *, in_watchlist: bool
) -> tuple[int | None, str | None]:
    payload = job.payload or {}
    imdb_id = _coerce_str(payload.get("imdb_id"))
    tmdb_id = _coerce_str(payload.get("tmdb_id"))
    film_id = _coerce_str(payload.get("letterboxd_film_id"))
    if imdb_id:
        imdb_id = imdb_id.lower()
    if tmdb_id == "":
        tmdb_id = None
    if not imdb_id and not tmdb_id and not film_id:
        raise ValueError("Letterboxd watchlist sync requires an IMDb or TMDB ID")

    integration, secret_data = await load_integration_with_secrets(db, job.user_id, "letterboxd")
    if not integration or not secret_data:
        raise LetterboxdError("Letterboxd credentials are missing", status_code=401)
    if not has_required_letterboxd_fields(secret_data):
        raise LetterboxdError("Letterboxd credentials are incomplete", status_code=401)
    api_base_url = DEFAULT_LETTERBOXD_API_BASE_URL
    if integration.config and integration.config.get("api_base_url"):
        api_base_url = str(integration.config["api_base_url"])
    client = LetterboxdClient(
        api_base_url=api_base_url,
        client_id=str(secret_data.get("client_id")),
        client_secret=str(secret_data.get("client_secret")),
        refresh_token=str(secret_data.get("refresh_token")),
        cookies=_safe_cookies(secret_data.get("cookies")),
    )
    access_token = await _ensure_letterboxd_access_token(db, integration.id, secret_data, client)
    if not film_id:
        film_id = await client.resolve_film_id(access_token, imdb_id, tmdb_id)
    if in_watchlist:
        _, response_code = await client.add_to_watchlist(
            film_id=film_id,
            imdb_id=imdb_id,
            tmdb_id=tmdb_id,
            access_token=access_token,
        )
    else:
        _, response_code = await client.remove_from_watchlist(
            film_id=film_id,
            imdb_id=imdb_id,
            tmdb_id=tmdb_id,
            access_token=access_token,
        )
    return response_code, film_id


async def _deliver_letterboxd_log_update(
    db: AsyncSession, job: OutboxJob
) -> tuple[int | None, str | None]:
    payload = job.payload or {}
    entry_id = payload.get("entry_id")
    if not entry_id:
        raise ValueError("Letterboxd update requires a log entry id")
    watched_at = None
    if "watched_at" in payload and payload.get("watched_at") is not None:
        watched_at = _parse_datetime(payload.get("watched_at"))
    rating = None
    if "rating" in payload and payload.get("rating") is not None:
        rating = coerce_star_rating(payload.get("rating"))
    tags = _normalize_tags(payload.get("tags"))
    like = _normalize_like(payload.get("like"))
    if watched_at is None and rating is None and tags is None and like is None:
        raise ValueError("Letterboxd update requires at least one field to change")

    integration, secret_data = await load_integration_with_secrets(db, job.user_id, "letterboxd")
    if not integration or not secret_data:
        raise LetterboxdError("Letterboxd credentials are missing", status_code=401)
    if not has_required_letterboxd_fields(secret_data):
        raise LetterboxdError("Letterboxd credentials are incomplete", status_code=401)
    api_base_url = DEFAULT_LETTERBOXD_API_BASE_URL
    if integration.config and integration.config.get("api_base_url"):
        api_base_url = str(integration.config["api_base_url"])
    client = LetterboxdClient(
        api_base_url=api_base_url,
        client_id=str(secret_data.get("client_id")),
        client_secret=str(secret_data.get("client_secret")),
        refresh_token=str(secret_data.get("refresh_token")),
        cookies=_safe_cookies(secret_data.get("cookies")),
    )
    access_token = await _ensure_letterboxd_access_token(db, integration.id, secret_data, client)
    response, response_code = await client.update_log_entry(
        str(entry_id),
        watched_at=watched_at,
        rating=rating,
        tags=tags,
        like=like,
        access_token=access_token,
    )
    return response_code, str(entry_id)


async def _deliver_letterboxd_delete(
    db: AsyncSession, job: OutboxJob
) -> tuple[int | None, str | None]:
    payload = job.payload or {}
    entry_id = payload.get("entry_id")
    if not entry_id:
        raise ValueError("Letterboxd delete requires a log entry id")

    integration, secret_data = await load_integration_with_secrets(db, job.user_id, "letterboxd")
    if not integration or not secret_data:
        raise LetterboxdError("Letterboxd credentials are missing", status_code=401)
    if not has_required_letterboxd_fields(secret_data):
        raise LetterboxdError("Letterboxd credentials are incomplete", status_code=401)
    api_base_url = DEFAULT_LETTERBOXD_API_BASE_URL
    if integration.config and integration.config.get("api_base_url"):
        api_base_url = str(integration.config["api_base_url"])
    client = LetterboxdClient(
        api_base_url=api_base_url,
        client_id=str(secret_data.get("client_id")),
        client_secret=str(secret_data.get("client_secret")),
        refresh_token=str(secret_data.get("refresh_token")),
        cookies=_safe_cookies(secret_data.get("cookies")),
    )
    access_token = await _ensure_letterboxd_access_token(db, integration.id, secret_data, client)
    _, response_code = await client.delete_log_entry(str(entry_id), access_token=access_token)
    return response_code, str(entry_id)


async def _ensure_letterboxd_access_token(
    db: AsyncSession,
    integration_id: str,
    secret_data: dict[str, object],
    client: LetterboxdClient,
) -> str:
    access_token = secret_data.get("access_token")
    expires_at = parse_letterboxd_expires_at(secret_data.get("expires_at"))
    if (
        isinstance(access_token, str)
        and access_token
        and not is_letterboxd_token_expired(expires_at)
    ):
        return access_token
    token = await client.refresh_access_token_payload()
    updated = dict(secret_data)
    updated.update(letterboxd_token_to_secret_payload(token))
    await _save_integration_secret(db, integration_id, updated)
    return token.access_token


def _extract_letterboxd_member_id(integration: Integration) -> str | None:
    if not integration.config:
        return None
    member_id = integration.config.get("member_id")
    if member_id is None:
        return None
    cleaned = str(member_id).strip()
    return cleaned or None


async def _ensure_letterboxd_member(
    db: AsyncSession,
    integration: Integration,
    client: LetterboxdClient,
    access_token: str,
) -> str | None:
    member_id = _extract_letterboxd_member_id(integration)
    if member_id:
        return member_id
    me_payload = await client.fetch_me(access_token=access_token)
    member_id = extract_member_id(me_payload)
    member_name = extract_member_name(me_payload)
    if not member_id:
        raise LetterboxdError("Letterboxd /me response missing member id")
    if member_id or member_name:
        config = dict(integration.config or {})
        if member_id:
            config["member_id"] = member_id
        if member_name:
            config["member_name"] = member_name
        integration.config = config
        db.add(integration)
    return member_id


def _merge_payload_list(payloads: list[dict[str, Any]], keys: tuple[str, ...]) -> dict[str, Any]:
    merged: dict[str, Any] = {}
    for payload in payloads:
        for key in keys:
            items = payload.get(key)
            if isinstance(items, list) and items:
                merged.setdefault(key, []).extend(items)
    if not merged:
        raise ValueError("Batch payload did not include any items")
    return merged


async def _deliver_trakt_watch_batch(db: AsyncSession, jobs: list[OutboxJob]) -> int | None:
    integration, secret_data = await load_integration_with_secrets(db, jobs[0].user_id, "trakt")
    if not integration or not secret_data:
        raise TraktError("Trakt credentials are missing", status_code=401)
    if not has_required_trakt_fields(secret_data):
        raise TraktError("Trakt credentials are incomplete", status_code=401)
    if not settings.trakt_client_id or not settings.trakt_client_secret:
        raise ValueError("Trakt client ID/secret are not configured")

    client = TraktClient(
        client_id=settings.trakt_client_id,
        client_secret=settings.trakt_client_secret,
    )
    access_token = await _ensure_trakt_access_token(db, integration.id, secret_data, client)
    payloads: list[dict[str, Any]] = []
    for job in jobs:
        payload = job.payload or {}
        watched_at = _parse_datetime(payload.get("watched_at"))
        payloads.append(_build_trakt_history_payload(payload, watched_at))
    merged = _merge_payload_list(payloads, ("movies", "episodes", "shows"))
    _, response_code = await client.add_history(merged, access_token)
    return response_code


async def _deliver_trakt_rating_batch(db: AsyncSession, jobs: list[OutboxJob]) -> int | None:
    integration, secret_data = await load_integration_with_secrets(db, jobs[0].user_id, "trakt")
    if not integration or not secret_data:
        raise TraktError("Trakt credentials are missing", status_code=401)
    if not has_required_trakt_fields(secret_data):
        raise TraktError("Trakt credentials are incomplete", status_code=401)
    if not settings.trakt_client_id or not settings.trakt_client_secret:
        raise ValueError("Trakt client ID/secret are not configured")

    client = TraktClient(
        client_id=settings.trakt_client_id,
        client_secret=settings.trakt_client_secret,
    )
    access_token = await _ensure_trakt_access_token(db, integration.id, secret_data, client)
    payloads: list[dict[str, Any]] = []
    for job in jobs:
        payload = job.payload or {}
        rating = _normalize_trakt_rating(payload.get("rating"))
        payloads.append(_build_trakt_rating_payload(payload, rating))
    merged = _merge_payload_list(payloads, ("movies", "episodes", "shows"))
    _, response_code = await client.add_ratings(merged, access_token)
    return response_code


async def _deliver_simkl_watch_batch(db: AsyncSession, jobs: list[OutboxJob]) -> int | None:
    integration, secret_data = await load_integration_with_secrets(db, jobs[0].user_id, "simkl")
    if not integration or not secret_data:
        raise SimklError("SIMKL credentials are missing", status_code=401)
    if not has_required_simkl_fields(secret_data):
        raise SimklError("SIMKL credentials are incomplete", status_code=401)
    if not settings.simkl_client_id or not settings.simkl_client_secret:
        raise ValueError("SIMKL client ID/secret are not configured")

    client = SimklClient(
        client_id=settings.simkl_client_id,
        client_secret=settings.simkl_client_secret,
    )
    access_token = await _ensure_simkl_access_token(db, integration.id, secret_data, client)
    payloads: list[dict[str, Any]] = []
    for job in jobs:
        payload = job.payload or {}
        watched_at = _parse_datetime(payload.get("watched_at"))
        payloads.append(_build_simkl_history_payload(payload, watched_at))
    merged = _merge_payload_list(payloads, ("movies", "episodes", "shows"))
    _, response_code = await client.add_history(merged, access_token)
    return response_code


async def _deliver_simkl_rating_batch(db: AsyncSession, jobs: list[OutboxJob]) -> int | None:
    integration, secret_data = await load_integration_with_secrets(db, jobs[0].user_id, "simkl")
    if not integration or not secret_data:
        raise SimklError("SIMKL credentials are missing", status_code=401)
    if not has_required_simkl_fields(secret_data):
        raise SimklError("SIMKL credentials are incomplete", status_code=401)
    if not settings.simkl_client_id or not settings.simkl_client_secret:
        raise ValueError("SIMKL client ID/secret are not configured")

    client = SimklClient(
        client_id=settings.simkl_client_id,
        client_secret=settings.simkl_client_secret,
    )
    access_token = await _ensure_simkl_access_token(db, integration.id, secret_data, client)
    payloads: list[dict[str, Any]] = []
    for job in jobs:
        payload = job.payload or {}
        rating = _normalize_simkl_rating(payload.get("rating"))
        payloads.append(_build_simkl_rating_payload(payload, rating))
    merged = _merge_payload_list(payloads, ("movies", "episodes", "shows"))
    _, response_code = await client.add_ratings(merged, access_token)
    return response_code


async def _deliver_trakt_watch(db: AsyncSession, job: OutboxJob) -> tuple[int | None, str | None]:
    payload = job.payload or {}
    watched_at = _parse_datetime(payload.get("watched_at"))
    integration, secret_data = await load_integration_with_secrets(db, job.user_id, "trakt")
    if not integration or not secret_data:
        raise TraktError("Trakt credentials are missing", status_code=401)
    if not has_required_trakt_fields(secret_data):
        raise TraktError("Trakt credentials are incomplete", status_code=401)
    if not settings.trakt_client_id or not settings.trakt_client_secret:
        raise ValueError("Trakt client ID/secret are not configured")

    client = TraktClient(
        client_id=settings.trakt_client_id,
        client_secret=settings.trakt_client_secret,
    )
    access_token = await _ensure_trakt_access_token(db, integration.id, secret_data, client)
    existing_history_id = None
    existing_watched_at = None
    if job.attempts > 1:
        try:
            existing = await _find_trakt_history_for_day(client, access_token, payload, watched_at)
            if existing:
                existing_history_id, existing_watched_at = existing
        except TraktError as exc:
            logger.info("Trakt history precheck failed: %s", exc)
    if existing_history_id:
        if existing_watched_at:
            await _sync_local_watched_at(db, payload, existing_watched_at)
        return 200, existing_history_id
    history_payload = _build_trakt_history_payload(payload, watched_at)
    response, response_code = await client.add_history(history_payload, access_token)
    external_id = _extract_trakt_history_id(response, _coerce_str(payload.get("media_type")))
    return response_code, external_id


async def _deliver_trakt_rating(db: AsyncSession, job: OutboxJob) -> tuple[int | None, str | None]:
    payload = job.payload or {}
    rating = _normalize_trakt_rating(payload.get("rating"))
    integration, secret_data = await load_integration_with_secrets(db, job.user_id, "trakt")
    if not integration or not secret_data:
        raise TraktError("Trakt credentials are missing", status_code=401)
    if not has_required_trakt_fields(secret_data):
        raise TraktError("Trakt credentials are incomplete", status_code=401)
    if not settings.trakt_client_id or not settings.trakt_client_secret:
        raise ValueError("Trakt client ID/secret are not configured")

    client = TraktClient(
        client_id=settings.trakt_client_id,
        client_secret=settings.trakt_client_secret,
    )
    access_token = await _ensure_trakt_access_token(db, integration.id, secret_data, client)
    ratings_payload = _build_trakt_rating_payload(payload, rating)
    response, response_code = await client.add_ratings(ratings_payload, access_token)
    external_id = _extract_trakt_history_id(response, _coerce_str(payload.get("media_type")))
    return response_code, external_id


async def _deliver_trakt_watchlist(
    db: AsyncSession, job: OutboxJob
) -> tuple[int | None, str | None]:
    payload = job.payload or {}
    integration, secret_data = await load_integration_with_secrets(db, job.user_id, "trakt")
    if not integration or not secret_data:
        raise TraktError("Trakt credentials are missing", status_code=401)
    if not has_required_trakt_fields(secret_data):
        raise TraktError("Trakt credentials are incomplete", status_code=401)
    if not settings.trakt_client_id or not settings.trakt_client_secret:
        raise ValueError("Trakt client ID/secret are not configured")

    client = TraktClient(
        client_id=settings.trakt_client_id,
        client_secret=settings.trakt_client_secret,
    )
    access_token = await _ensure_trakt_access_token(db, integration.id, secret_data, client)
    watchlist_payload = _build_trakt_watchlist_payload(payload)
    _, response_code = await client.add_to_watchlist(watchlist_payload, access_token)
    return response_code, None


async def _deliver_trakt_watchlist_remove(
    db: AsyncSession, job: OutboxJob
) -> tuple[int | None, str | None]:
    payload = job.payload or {}
    integration, secret_data = await load_integration_with_secrets(db, job.user_id, "trakt")
    if not integration or not secret_data:
        raise TraktError("Trakt credentials are missing", status_code=401)
    if not has_required_trakt_fields(secret_data):
        raise TraktError("Trakt credentials are incomplete", status_code=401)
    if not settings.trakt_client_id or not settings.trakt_client_secret:
        raise ValueError("Trakt client ID/secret are not configured")

    client = TraktClient(
        client_id=settings.trakt_client_id,
        client_secret=settings.trakt_client_secret,
    )
    access_token = await _ensure_trakt_access_token(db, integration.id, secret_data, client)
    watchlist_payload = _build_trakt_watchlist_payload(payload)
    _, response_code = await client.remove_from_watchlist(watchlist_payload, access_token)
    return response_code, None


async def _deliver_trakt_update(db: AsyncSession, job: OutboxJob) -> tuple[int | None, str | None]:
    payload = job.payload or {}
    watched_at = _parse_datetime(payload.get("watched_at"))
    if not watched_at:
        raise ValueError("Trakt update requires watched_at")
    previous_watched_at = _parse_optional_datetime(payload.get("previous_watched_at"))
    history_id = _coerce_str(payload.get("history_id")) or _coerce_str(payload.get("external_id"))
    integration, secret_data = await load_integration_with_secrets(db, job.user_id, "trakt")
    if not integration or not secret_data:
        raise TraktError("Trakt credentials are missing", status_code=401)
    if not has_required_trakt_fields(secret_data):
        raise TraktError("Trakt credentials are incomplete", status_code=401)
    if not settings.trakt_client_id or not settings.trakt_client_secret:
        raise ValueError("Trakt client ID/secret are not configured")

    client = TraktClient(
        client_id=settings.trakt_client_id,
        client_secret=settings.trakt_client_secret,
    )
    access_token = await _ensure_trakt_access_token(
        db,
        integration.id,
        secret_data,
        client,
    )
    if previous_watched_at:
        if not history_id:
            match = await _resolve_trakt_history_match(
                client,
                access_token,
                payload,
                watched_at,
                previous_watched_at,
            )
            if match:
                history_id, _ = match
        if history_id:
            remove_payload = _build_trakt_remove_payload_for_id(history_id)
            _, response_code = await client.remove_history(remove_payload, access_token)
            if response_code and response_code >= 400:
                raise TraktError(
                    f"Trakt history remove returned {response_code}",
                    status_code=response_code,
                )
            history_payload = _build_trakt_history_payload(payload, watched_at)
            response, response_code = await client.add_history(history_payload, access_token)
            external_id = _extract_trakt_history_id(
                response,
                _coerce_str(payload.get("media_type")),
            )
            return response_code, external_id
    try:
        if history_id:
            _, response_code = await client.update_history(
                history_id,
                watched_at,
                access_token,
            )
            return response_code, history_id
    except TraktError as exc:
        if exc.status_code in {400, 404, 405}:
            history_id = None
        else:
            raise
    if history_id:
        remove_payload = _build_trakt_remove_payload_for_id(history_id)
        _, response_code = await client.remove_history(remove_payload, access_token)
        if response_code and response_code >= 400:
            raise TraktError(
                f"Trakt history remove returned {response_code}",
                status_code=response_code,
            )
    history_payload = _build_trakt_history_payload(payload, watched_at)
    response, response_code = await client.add_history(history_payload, access_token)
    external_id = _extract_trakt_history_id(
        response,
        _coerce_str(payload.get("media_type")),
    )
    return response_code, external_id


async def _deliver_trakt_remove(db: AsyncSession, job: OutboxJob) -> tuple[int | None, str | None]:
    payload = job.payload or {}
    integration, secret_data = await load_integration_with_secrets(db, job.user_id, "trakt")
    if not integration or not secret_data:
        raise TraktError("Trakt credentials are missing", status_code=401)
    if not has_required_trakt_fields(secret_data):
        raise TraktError("Trakt credentials are incomplete", status_code=401)
    if not settings.trakt_client_id or not settings.trakt_client_secret:
        raise ValueError("Trakt client ID/secret are not configured")

    client = TraktClient(
        client_id=settings.trakt_client_id,
        client_secret=settings.trakt_client_secret,
    )
    access_token = await _ensure_trakt_access_token(db, integration.id, secret_data, client)

    history_id = payload.get("history_id") or payload.get("external_id")
    history_ids: list[int | str] = []
    if history_id:
        values = history_id if isinstance(history_id, list) else [history_id]
        for value in values:
            if isinstance(value, (int, float)) and int(value) == value:
                history_ids.append(int(value))
            elif isinstance(value, str):
                cleaned = value.strip()
                if cleaned:
                    history_ids.append(int(cleaned) if cleaned.isdigit() else cleaned)
    if history_ids:
        remove_payload = {"ids": history_ids}
    else:
        remove_payload = _build_trakt_remove_payload(payload)
    _, response_code = await client.remove_history(remove_payload, access_token)
    return response_code, None


async def _deliver_simkl_watch(db: AsyncSession, job: OutboxJob) -> tuple[int | None, str | None]:
    payload = job.payload or {}
    watched_at = _parse_datetime(payload.get("watched_at"))
    integration, secret_data = await load_integration_with_secrets(db, job.user_id, "simkl")
    if not integration or not secret_data:
        raise SimklError("SIMKL credentials are missing", status_code=401)
    if not has_required_simkl_fields(secret_data):
        raise SimklError("SIMKL credentials are incomplete", status_code=401)
    if not settings.simkl_client_id or not settings.simkl_client_secret:
        raise ValueError("SIMKL client ID/secret are not configured")

    client = SimklClient(
        client_id=settings.simkl_client_id,
        client_secret=settings.simkl_client_secret,
    )
    access_token = await _ensure_simkl_access_token(db, integration.id, secret_data, client)
    history_payload = _build_simkl_history_payload(payload, watched_at)
    response, response_code = await client.add_history(history_payload, access_token)
    external_id = _extract_simkl_history_id(response)
    return response_code, external_id


async def _deliver_simkl_rating(db: AsyncSession, job: OutboxJob) -> tuple[int | None, str | None]:
    payload = job.payload or {}
    rating = _normalize_simkl_rating(payload.get("rating"))
    integration, secret_data = await load_integration_with_secrets(db, job.user_id, "simkl")
    if not integration or not secret_data:
        raise SimklError("SIMKL credentials are missing", status_code=401)
    if not has_required_simkl_fields(secret_data):
        raise SimklError("SIMKL credentials are incomplete", status_code=401)
    if not settings.simkl_client_id or not settings.simkl_client_secret:
        raise ValueError("SIMKL client ID/secret are not configured")

    client = SimklClient(
        client_id=settings.simkl_client_id,
        client_secret=settings.simkl_client_secret,
    )
    access_token = await _ensure_simkl_access_token(db, integration.id, secret_data, client)
    ratings_payload = _build_simkl_rating_payload(payload, rating)
    response, response_code = await client.add_ratings(ratings_payload, access_token)
    external_id = _extract_simkl_history_id(response)
    return response_code, external_id


async def _deliver_simkl_watchlist(
    db: AsyncSession, job: OutboxJob
) -> tuple[int | None, str | None]:
    payload = job.payload or {}
    integration, secret_data = await load_integration_with_secrets(db, job.user_id, "simkl")
    if not integration or not secret_data:
        raise SimklError("SIMKL credentials are missing", status_code=401)
    if not has_required_simkl_fields(secret_data):
        raise SimklError("SIMKL credentials are incomplete", status_code=401)
    if not settings.simkl_client_id or not settings.simkl_client_secret:
        raise ValueError("SIMKL client ID/secret are not configured")

    client = SimklClient(
        client_id=settings.simkl_client_id,
        client_secret=settings.simkl_client_secret,
    )
    access_token = await _ensure_simkl_access_token(db, integration.id, secret_data, client)
    watchlist_payload = _build_simkl_watchlist_payload(payload)
    _, response_code = await client.add_to_watchlist(watchlist_payload, access_token)
    return response_code, None


async def _deliver_simkl_watchlist_remove(
    db: AsyncSession, job: OutboxJob
) -> tuple[int | None, str | None]:
    payload = job.payload or {}
    integration, secret_data = await load_integration_with_secrets(db, job.user_id, "simkl")
    if not integration or not secret_data:
        raise SimklError("SIMKL credentials are missing", status_code=401)
    if not has_required_simkl_fields(secret_data):
        raise SimklError("SIMKL credentials are incomplete", status_code=401)
    if not settings.simkl_client_id or not settings.simkl_client_secret:
        raise ValueError("SIMKL client ID/secret are not configured")

    client = SimklClient(
        client_id=settings.simkl_client_id,
        client_secret=settings.simkl_client_secret,
    )
    access_token = await _ensure_simkl_access_token(db, integration.id, secret_data, client)
    media_type = _coerce_str(payload.get("media_type")) or "movie"
    if media_type in {"tv", "anime"}:
        watchlist_payload = _build_simkl_drop_watchlist_payload(payload)
        _, response_code = await client.add_to_list(watchlist_payload, access_token)
    else:
        watchlist_payload = _build_simkl_watchlist_payload(payload)
        _, response_code = await client.remove_from_watchlist(watchlist_payload, access_token)
    return response_code, None


async def _deliver_simkl_update(db: AsyncSession, job: OutboxJob) -> tuple[int | None, str | None]:
    return await _deliver_simkl_watch(db, job)


async def _deliver_simkl_remove(db: AsyncSession, job: OutboxJob) -> tuple[int | None, str | None]:
    payload = job.payload or {}
    integration, secret_data = await load_integration_with_secrets(db, job.user_id, "simkl")
    if not integration or not secret_data:
        raise SimklError("SIMKL credentials are missing", status_code=401)
    if not has_required_simkl_fields(secret_data):
        raise SimklError("SIMKL credentials are incomplete", status_code=401)
    if not settings.simkl_client_id or not settings.simkl_client_secret:
        raise ValueError("SIMKL client ID/secret are not configured")

    client = SimklClient(
        client_id=settings.simkl_client_id,
        client_secret=settings.simkl_client_secret,
    )
    access_token = await _ensure_simkl_access_token(db, integration.id, secret_data, client)
    remove_payload = _build_simkl_remove_payload(payload)
    _, response_code = await client.remove_history(remove_payload, access_token)
    return response_code, None


async def _load_publicmetadb_client(
    db: AsyncSession, user_id: str
) -> tuple[PublicMetaDbClient, str]:
    integration, secret_data = await load_integration_with_secrets(db, user_id, "publicmetadb")
    if not integration or not secret_data:
        raise PublicMetaDbError("PublicMetaDB credentials are missing", status_code=401)
    if not has_required_publicmetadb_fields(secret_data):
        raise PublicMetaDbError("PublicMetaDB credentials are incomplete", status_code=401)
    if not is_publicmetadb_sync_enabled(dict(integration.config or {})):
        raise PublicMetaDbError("PublicMetaDB sync is disabled", status_code=409)
    api_key = _coerce_str(secret_data.get("api_key"))
    if not api_key:
        raise PublicMetaDbError("PublicMetaDB API key is missing", status_code=401)
    return PublicMetaDbClient(), api_key


def _extract_publicmetadb_entry_id(payload: object) -> str | None:
    if not isinstance(payload, dict):
        return None
    for key in ("id", "watched_id", "rating_id"):
        value = payload.get(key)
        if isinstance(value, (str, int)):
            cleaned = str(value).strip()
            if cleaned:
                return cleaned
    item = payload.get("item")
    if isinstance(item, dict):
        value = item.get("id")
        if isinstance(value, (str, int)):
            cleaned = str(value).strip()
            if cleaned:
                return cleaned
    return None


def _extract_publicmetadb_items(payload: object) -> list[dict[str, object]]:
    if not isinstance(payload, dict):
        return []
    items = payload.get("items")
    if not isinstance(items, list):
        return []
    return [item for item in items if isinstance(item, dict)]


def _extract_publicmetadb_watchlist_id(payload: object) -> str | None:
    if not isinstance(payload, dict):
        return None
    for key in ("id", "watchlist_id"):
        value = payload.get(key)
        if isinstance(value, (str, int)):
            cleaned = str(value).strip()
            if cleaned:
                return cleaned
    item = payload.get("item")
    if isinstance(item, dict):
        return _extract_publicmetadb_watchlist_id(item)
    return None


def _extract_publicmetadb_watchlist_items(payload: object) -> list[dict[str, object]]:
    if not isinstance(payload, dict):
        return []
    items = payload.get("items")
    if not isinstance(items, list):
        return []
    return [item for item in items if isinstance(item, dict)]


def _extract_publicmetadb_tmdb_id(payload: dict[str, object]) -> int | None:
    direct_id = _coerce_int(payload.get("tmdb_id"))
    if direct_id is not None:
        return direct_id
    movie_ids = payload.get("movie_ids")
    if isinstance(movie_ids, dict):
        tmdb_id = _coerce_int(movie_ids.get("tmdb"))
        if tmdb_id is not None:
            return tmdb_id
    show_ids = payload.get("show_ids")
    if isinstance(show_ids, dict):
        tmdb_id = _coerce_int(show_ids.get("tmdb"))
        if tmdb_id is not None:
            return tmdb_id
    return None


def _normalize_publicmetadb_media_type(payload: dict[str, object]) -> str:
    media_type = _coerce_str(payload.get("media_type")) or "movie"
    if media_type in {"tv", "series"}:
        return "tv"
    return "movie"


def _normalize_publicmetadb_rating(value: object) -> int:
    rating = coerce_star_rating(value)
    if rating is None:
        raise ValueError("PublicMetaDB rating must be between 0.5 and 5.0 stars")
    normalized = int(round(rating * 20))
    if normalized < 0:
        return 0
    if normalized > 100:
        return 100
    return normalized


def _normalize_publicmetadb_label(value: object) -> str:
    if isinstance(value, str):
        cleaned = value.strip()
        if cleaned:
            return cleaned
    return "overall"


def _match_publicmetadb_watched_item(
    item: dict[str, object],
    *,
    tmdb_id: int,
    media_type: str,
    season: int | None,
    episode: int | None,
) -> bool:
    item_tmdb_id = _coerce_int(item.get("tmdb_id"))
    if item_tmdb_id != tmdb_id:
        return False
    item_media_type = _coerce_str(item.get("media_type")) or "movie"
    if item_media_type == "series":
        item_media_type = "tv"
    if item_media_type != media_type:
        return False
    if media_type != "tv":
        return True
    item_season = _coerce_int(item.get("season"))
    item_episode = _coerce_int(item.get("episode"))
    return item_season == season and item_episode == episode


def _match_publicmetadb_watchlist_item(
    item: dict[str, object],
    *,
    tmdb_id: int,
    media_type: str,
) -> bool:
    item_tmdb_id = _coerce_int(item.get("tmdb_id"))
    if item_tmdb_id != tmdb_id:
        return False
    item_media_type = _coerce_str(item.get("media_type")) or "movie"
    if item_media_type == "series":
        item_media_type = "tv"
    return item_media_type == media_type


async def _resolve_publicmetadb_watched_id(
    client: PublicMetaDbClient,
    api_key: str,
    *,
    tmdb_id: int | None,
    media_type: str,
    season: int | None,
    episode: int | None,
) -> str | None:
    if tmdb_id is None:
        return None
    payload, _response_code = await client.list_watched(api_key)
    for item in _extract_publicmetadb_items(payload):
        if not _match_publicmetadb_watched_item(
            item,
            tmdb_id=tmdb_id,
            media_type=media_type,
            season=season,
            episode=episode,
        ):
            continue
        item_id = item.get("id")
        if isinstance(item_id, (str, int)):
            cleaned = str(item_id).strip()
            if cleaned:
                return cleaned
    return None


async def _resolve_publicmetadb_watchlist_id(
    client: PublicMetaDbClient,
    api_key: str,
    *,
    tmdb_id: int | None,
    media_type: str,
) -> str | None:
    if tmdb_id is None:
        return None
    payload, _response_code = await client.list_watchlist(api_key)
    for item in _extract_publicmetadb_watchlist_items(payload):
        if not _match_publicmetadb_watchlist_item(
            item,
            tmdb_id=tmdb_id,
            media_type=media_type,
        ):
            continue
        item_id = item.get("id") or item.get("watchlist_id")
        if isinstance(item_id, (str, int)):
            cleaned = str(item_id).strip()
            if cleaned:
                return cleaned
    return None


async def _deliver_publicmetadb_watch(
    db: AsyncSession, job: OutboxJob
) -> tuple[int | None, str | None]:
    payload = job.payload or {}
    tmdb_id = _extract_publicmetadb_tmdb_id(payload)
    if tmdb_id is None:
        raise ValueError("PublicMetaDB sync requires a TMDB ID")
    media_type = _normalize_publicmetadb_media_type(payload)
    season_number = _coerce_int(payload.get("season_number"))
    episode_number = _coerce_int(payload.get("episode_number"))
    watched_at = _parse_optional_datetime(payload.get("watched_at"))
    if media_type == "tv" and (season_number is None or episode_number is None):
        raise ValueError("PublicMetaDB TV sync requires season_number and episode_number")
    client, api_key = await _load_publicmetadb_client(db, job.user_id)
    if job.attempts > 1 and watched_at is not None:
        try:
            existing = await _find_publicmetadb_watched_for_retry(
                client,
                api_key,
                tmdb_id=tmdb_id,
                media_type=media_type,
                season=season_number,
                episode=episode_number,
                watched_at=watched_at,
            )
            if existing:
                existing_history_id, existing_watched_at = existing
                await _sync_local_watched_at(db, payload, existing_watched_at)
                return 200, existing_history_id
        except PublicMetaDbError as exc:
            logger.info("PublicMetaDB history precheck failed: %s", exc)
    try:
        response, response_code = await client.mark_watched(
            api_key,
            tmdb_id=tmdb_id,
            media_type=media_type,
            season=season_number,
            episode=episode_number,
            watched_at=watched_at,
        )
    except PublicMetaDbError as exc:
        if exc.status_code == 409:
            existing_id = await _resolve_publicmetadb_watched_id(
                client,
                api_key,
                tmdb_id=tmdb_id,
                media_type=media_type,
                season=season_number,
                episode=episode_number,
            )
            if existing_id:
                await client.delete_watched(api_key, existing_id)
                response, response_code = await client.mark_watched(
                    api_key,
                    tmdb_id=tmdb_id,
                    media_type=media_type,
                    season=season_number,
                    episode=episode_number,
                    watched_at=watched_at,
                )
            else:
                raise
        else:
            raise
    return response_code, _extract_publicmetadb_entry_id(response)


def _parse_publicmetadb_datetime(value: object) -> datetime | None:
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if not isinstance(value, str):
        return None
    cleaned = value.strip()
    if not cleaned:
        return None
    if cleaned.endswith("Z"):
        cleaned = f"{cleaned[:-1]}+00:00"
    return _parse_optional_datetime(cleaned)


def _extract_publicmetadb_watched_at(item: dict[str, object]) -> datetime | None:
    for key in ("watched_at", "watchedAt", "created_at", "createdAt"):
        parsed = _parse_publicmetadb_datetime(item.get(key))
        if parsed is not None:
            return parsed
    return None


def _same_utc_day(left: datetime, right: datetime) -> bool:
    left_utc = left if left.tzinfo else left.replace(tzinfo=timezone.utc)
    right_utc = right if right.tzinfo else right.replace(tzinfo=timezone.utc)
    return left_utc.astimezone(timezone.utc).date() == right_utc.astimezone(timezone.utc).date()


async def _find_publicmetadb_watched_for_retry(
    client: PublicMetaDbClient,
    api_key: str,
    *,
    tmdb_id: int,
    media_type: str,
    season: int | None,
    episode: int | None,
    watched_at: datetime,
) -> tuple[str, datetime] | None:
    payload, _response_code = await client.list_watched(api_key)
    for item in _extract_publicmetadb_items(payload):
        if not _match_publicmetadb_watched_item(
            item,
            tmdb_id=tmdb_id,
            media_type=media_type,
            season=season,
            episode=episode,
        ):
            continue
        existing_watched_at = _extract_publicmetadb_watched_at(item)
        if existing_watched_at is None or not _same_utc_day(existing_watched_at, watched_at):
            continue
        item_id = item.get("id")
        if isinstance(item_id, (str, int)):
            cleaned = str(item_id).strip()
            if cleaned:
                return cleaned, existing_watched_at
    return None


async def _deliver_publicmetadb_watchlist(
    db: AsyncSession, job: OutboxJob
) -> tuple[int | None, str | None]:
    payload = job.payload or {}
    tmdb_id = _extract_publicmetadb_tmdb_id(payload)
    if tmdb_id is None:
        raise ValueError("PublicMetaDB watchlist sync requires a TMDB ID")
    media_type = _normalize_publicmetadb_media_type(payload)
    client, api_key = await _load_publicmetadb_client(db, job.user_id)
    response, response_code = await client.add_to_watchlist(
        api_key,
        tmdb_id=tmdb_id,
        media_type=media_type,
    )
    return response_code, _extract_publicmetadb_watchlist_id(response)


async def _deliver_publicmetadb_watchlist_remove(
    db: AsyncSession, job: OutboxJob
) -> tuple[int | None, str | None]:
    payload = job.payload or {}
    client, api_key = await _load_publicmetadb_client(db, job.user_id)
    watchlist_id = _coerce_str(payload.get("watchlist_id") or payload.get("external_id"))
    tmdb_id = _extract_publicmetadb_tmdb_id(payload)
    media_type = _normalize_publicmetadb_media_type(payload)
    if not watchlist_id:
        watchlist_id = await _resolve_publicmetadb_watchlist_id(
            client,
            api_key,
            tmdb_id=tmdb_id,
            media_type=media_type,
        )
    if not watchlist_id:
        return 200, None
    try:
        _response_payload, response_code = await client.delete_watchlist(api_key, watchlist_id)
    except PublicMetaDbError as exc:
        if exc.status_code == 404:
            return 200, None
        raise
    return response_code, None


async def _deliver_publicmetadb_update(
    db: AsyncSession, job: OutboxJob
) -> tuple[int | None, str | None]:
    payload = job.payload or {}
    client, api_key = await _load_publicmetadb_client(db, job.user_id)
    history_id = _coerce_str(payload.get("history_id") or payload.get("external_id"))
    tmdb_id = _extract_publicmetadb_tmdb_id(payload)
    media_type = _normalize_publicmetadb_media_type(payload)
    season_number = _coerce_int(payload.get("season_number"))
    episode_number = _coerce_int(payload.get("episode_number"))
    watched_at = _parse_optional_datetime(payload.get("watched_at"))
    if media_type == "tv" and (season_number is None or episode_number is None):
        raise ValueError("PublicMetaDB TV sync requires season_number and episode_number")
    if not history_id:
        history_id = await _resolve_publicmetadb_watched_id(
            client,
            api_key,
            tmdb_id=tmdb_id,
            media_type=media_type,
            season=season_number,
            episode=episode_number,
        )
    if history_id:
        try:
            await client.delete_watched(api_key, history_id)
        except PublicMetaDbError as exc:
            if exc.status_code != 404:
                raise
    if tmdb_id is None:
        raise ValueError("PublicMetaDB sync requires a TMDB ID")
    response, response_code = await client.mark_watched(
        api_key,
        tmdb_id=tmdb_id,
        media_type=media_type,
        season=season_number,
        episode=episode_number,
        watched_at=watched_at,
    )
    return response_code, _extract_publicmetadb_entry_id(response)


async def _deliver_publicmetadb_remove(
    db: AsyncSession, job: OutboxJob
) -> tuple[int | None, str | None]:
    payload = job.payload or {}
    client, api_key = await _load_publicmetadb_client(db, job.user_id)
    history_id = _coerce_str(payload.get("history_id") or payload.get("external_id"))
    tmdb_id = _extract_publicmetadb_tmdb_id(payload)
    media_type = _normalize_publicmetadb_media_type(payload)
    season_number = _coerce_int(payload.get("season_number"))
    episode_number = _coerce_int(payload.get("episode_number"))
    if not history_id:
        history_id = await _resolve_publicmetadb_watched_id(
            client,
            api_key,
            tmdb_id=tmdb_id,
            media_type=media_type,
            season=season_number,
            episode=episode_number,
        )
    if not history_id:
        return 200, None
    try:
        _response_payload, response_code = await client.delete_watched(api_key, history_id)
    except PublicMetaDbError as exc:
        if exc.status_code == 404:
            return 200, None
        raise
    return response_code, None


async def _deliver_publicmetadb_rating(
    db: AsyncSession, job: OutboxJob
) -> tuple[int | None, str | None]:
    payload = job.payload or {}
    tmdb_id = _extract_publicmetadb_tmdb_id(payload)
    if tmdb_id is None:
        raise ValueError("PublicMetaDB rating sync requires a TMDB ID")
    media_type = _normalize_publicmetadb_media_type(payload)
    score = _normalize_publicmetadb_rating(payload.get("rating"))
    label = _normalize_publicmetadb_label(payload.get("label"))
    season_number = _coerce_int(payload.get("season_number"))
    episode_number = _coerce_int(payload.get("episode_number"))
    client, api_key = await _load_publicmetadb_client(db, job.user_id)
    if media_type == "tv":
        if season_number is None or episode_number is None:
            raise ValueError(
                "PublicMetaDB TV rating sync requires season_number and episode_number"
            )
        existing_id = await _find_publicmetadb_rating_id(
            client,
            api_key,
            tmdb_id=tmdb_id,
            media_type=media_type,
            season=season_number,
            episode=episode_number,
            label=label,
        )
        if existing_id:
            await client.delete_episode_rating(api_key, existing_id)
        try:
            _response_payload, response_code = await client.create_episode_rating(
                api_key,
                tmdb_id=tmdb_id,
                media_type=media_type,
                season=season_number,
                episode=episode_number,
                score=score,
                label=label,
            )
        except PublicMetaDbError as exc:
            if exc.status_code == 409 and existing_id:
                await client.delete_episode_rating(api_key, existing_id)
                _response_payload, response_code = await client.create_episode_rating(
                    api_key,
                    tmdb_id=tmdb_id,
                    media_type=media_type,
                    season=season_number,
                    episode=episode_number,
                    score=score,
                    label=label,
                )
            else:
                raise
        return response_code, None
    existing_id = await _find_publicmetadb_rating_id(
        client,
        api_key,
        tmdb_id=tmdb_id,
        media_type=media_type,
        label=label,
    )
    if existing_id:
        await client.delete_rating(api_key, existing_id)
    try:
        _response_payload, response_code = await client.create_rating(
            api_key,
            tmdb_id=tmdb_id,
            media_type=media_type,
            score=score,
            label=label,
        )
    except PublicMetaDbError as exc:
        if exc.status_code == 409 and existing_id:
            await client.delete_rating(api_key, existing_id)
            _response_payload, response_code = await client.create_rating(
                api_key,
                tmdb_id=tmdb_id,
                media_type=media_type,
                score=score,
                label=label,
            )
        else:
            raise
    return response_code, None


async def _find_publicmetadb_rating_id(
    client: PublicMetaDbClient,
    api_key: str,
    *,
    tmdb_id: int,
    media_type: str,
    season: int | None = None,
    episode: int | None = None,
    label: str,
) -> str | None:
    if media_type == "tv" and season is not None and episode is not None:
        rating_list, _ = await client.list_episode_ratings(
            api_key,
            tmdb_id=tmdb_id,
            media_type=media_type,
            season=season,
            episode=episode,
            label=label,
        )
    else:
        rating_list, _ = await client.list_ratings(
            api_key,
            tmdb_id=tmdb_id,
            media_type=media_type,
            label=label,
        )
    items = rating_list.get("items") if isinstance(rating_list, dict) else []
    for item in items if isinstance(items, list) else []:
        item_id = item.get("id")
        if isinstance(item_id, (str, int)):
            return str(item_id)
    return None


async def _deliver_stremio_watch(db: AsyncSession, job: OutboxJob) -> tuple[int | None, str | None]:
    payload = job.payload or {}
    watched_at = _parse_datetime(payload.get("watched_at"))
    item_id = _coerce_str(payload.get("item_id") or payload.get("stremio_item_id"))
    if not item_id:
        raise ValueError("Stremio sync requires item_id")

    integration, secret_data = await load_integration_with_secrets(db, job.user_id, "stremio")
    if not integration or not secret_data:
        raise StremioError("Stremio credentials are missing", status_code=401)
    if not has_required_stremio_fields(secret_data):
        raise StremioError("Stremio credentials are incomplete", status_code=401)
    auth_key = _coerce_str(secret_data.get("auth_key"))
    if not auth_key:
        raise StremioError("Stremio auth key is missing", status_code=401)

    api_base_url = DEFAULT_STREMIO_API_BASE_URL
    if integration.config and integration.config.get("api_base_url"):
        api_base_url = str(integration.config["api_base_url"])
    client = StremioClient(api_base_url=api_base_url)
    if _is_series_payload(payload):
        if await _has_newer_stremio_series_job(db, job, item_id):
            external_id = _coerce_str(payload.get("video_id")) or item_id
            return 200, external_id
        change = await _build_stremio_series_change(
            db,
            client,
            auth_key,
            job.user_id,
            payload,
            watched_at,
            allow_empty=False,
        )
        if change is None:
            external_id = _coerce_str(payload.get("video_id")) or item_id
            return 200, external_id
        external_id = _coerce_str(payload.get("video_id")) or item_id
    else:
        change = _build_stremio_library_change(payload, watched_at)
        external_id = item_id
    await client.update_library_items(auth_key, [change])
    return 200, external_id


async def _deliver_stremio_remove(
    db: AsyncSession, job: OutboxJob
) -> tuple[int | None, str | None]:
    payload = job.payload or {}
    item_id = _coerce_str(payload.get("item_id") or payload.get("stremio_item_id"))
    if not item_id:
        raise ValueError("Stremio sync requires item_id")

    integration, secret_data = await load_integration_with_secrets(db, job.user_id, "stremio")
    if not integration or not secret_data:
        raise StremioError("Stremio credentials are missing", status_code=401)
    if not has_required_stremio_fields(secret_data):
        raise StremioError("Stremio credentials are incomplete", status_code=401)
    auth_key = _coerce_str(secret_data.get("auth_key"))
    if not auth_key:
        raise StremioError("Stremio auth key is missing", status_code=401)

    api_base_url = DEFAULT_STREMIO_API_BASE_URL
    if integration.config and integration.config.get("api_base_url"):
        api_base_url = str(integration.config["api_base_url"])
    client = StremioClient(api_base_url=api_base_url)
    if _is_series_payload(payload):
        if await _has_newer_stremio_series_job(db, job, item_id):
            external_id = _coerce_str(payload.get("video_id")) or item_id
            return 200, external_id
        try:
            change = await _build_stremio_series_change(
                db,
                client,
                auth_key,
                job.user_id,
                payload,
                datetime.now(timezone.utc),
                allow_empty=True,
            )
        except StremioError as exc:
            if str(exc) == "Cinemeta returned no episodes":
                external_id = _coerce_str(payload.get("video_id")) or item_id
                return 200, external_id
            raise
        if change is None:
            external_id = _coerce_str(payload.get("video_id")) or item_id
            return 200, external_id
        external_id = _coerce_str(payload.get("video_id")) or item_id
    else:
        change = _build_stremio_remove_change(payload)
        external_id = item_id
    await client.update_library_items(auth_key, [change])
    return 200, external_id


def _build_stremio_library_change(
    payload: dict[str, object], watched_at: datetime
) -> dict[str, object]:
    item_id = _coerce_str(payload.get("item_id") or payload.get("stremio_item_id"))
    if not item_id:
        raise ValueError("Stremio sync requires item_id")
    media_type = _coerce_str(payload.get("media_type")) or "movie"
    stremio_type = "series" if media_type in {"tv", "series"} else "movie"
    timestamp = _format_stremio_datetime(watched_at)
    state: dict[str, object] = {"lastWatched": timestamp}

    times_watched = _coerce_int(payload.get("times_watched"))
    flagged_watched = _coerce_int(payload.get("flagged_watched"))
    if times_watched is not None:
        state["timesWatched"] = max(times_watched, 1)
    else:
        state["timesWatched"] = 1
    if flagged_watched is not None:
        state["flaggedWatched"] = max(flagged_watched, 1)
    else:
        state["flaggedWatched"] = 1

    video_id = _coerce_str(payload.get("video_id"))
    if video_id:
        state["video_id"] = video_id
    season_number = _coerce_int(payload.get("season_number"))
    if season_number is not None:
        state["season"] = season_number
    episode_number = _coerce_int(payload.get("episode_number"))
    if episode_number is not None:
        state["episode"] = episode_number

    change: dict[str, object] = {
        "_id": item_id,
        "type": stremio_type,
        "state": state,
        "_ctime": timestamp,
        "_mtime": timestamp,
    }
    title = _coerce_str(payload.get("title"))
    if title:
        change["name"] = title
    year = payload.get("year")
    if isinstance(year, int):
        change["year"] = str(year)
    elif isinstance(year, str):
        cleaned = year.strip()
        if cleaned:
            change["year"] = cleaned
    poster = _coerce_str(payload.get("poster"))
    if poster:
        change["poster"] = poster
    return change


def _is_series_payload(payload: dict[str, object]) -> bool:
    media_type = _coerce_str(payload.get("media_type")) or ""
    if media_type in {"tv", "series"}:
        return True
    if _coerce_str(payload.get("video_id")):
        return True
    if _coerce_int(payload.get("season_number")) is not None:
        return True
    if _coerce_int(payload.get("episode_number")) is not None:
        return True
    return False


async def _build_stremio_series_change(
    db: AsyncSession,
    client: StremioClient,
    auth_key: str,
    user_id: str,
    payload: dict[str, object],
    watched_at: datetime,
    allow_empty: bool = False,
) -> dict[str, object] | None:
    item_id = _coerce_str(payload.get("item_id") or payload.get("stremio_item_id"))
    if not item_id:
        raise ValueError("Stremio sync requires item_id")
    video_ids = await fetch_cinemeta_video_ids(item_id)
    if not video_ids:
        raise StremioError("Cinemeta returned no episodes")

    watched_ids, latest_watched_at = await _load_series_watched_ids(
        db,
        user_id,
        item_id,
        _coerce_str(payload.get("watched_item_id")),
    )
    if not watched_ids and not allow_empty:
        return None
    wbf = watched_bitfield_from_array([False] * len(video_ids), video_ids)
    watched_set = set(watched_ids)
    for idx, video_id in enumerate(video_ids):
        if video_id in watched_set:
            wbf.set(idx, True)
    try:
        watched_str = wbf.to_string()
    except WatchedBitFieldError as exc:
        raise StremioError("Failed to serialize Stremio watched bitfield") from exc

    change_timestamp = _format_stremio_datetime(watched_at)

    existing_item = await _fetch_stremio_library_item(client, auth_key, item_id)
    existing_state = _coerce_mapping(existing_item.get("state")) if existing_item else None
    state = dict(existing_state or {})
    state["watched"] = watched_str

    last_watched = _parse_stremio_datetime(state.get("lastWatched"))
    if latest_watched_at and (not last_watched or latest_watched_at > last_watched):
        state["lastWatched"] = _format_stremio_datetime(latest_watched_at)

    next_video_id = wbf.get_next_unwatched_video_id()
    if next_video_id != _coerce_str(state.get("video_id")):
        state["video_id"] = next_video_id
        state["timeOffset"] = 0

    change: dict[str, object] = {
        "_id": item_id,
        "type": "series",
        "state": state,
        "_ctime": _select_stremio_ctime(existing_item, change_timestamp),
        "_mtime": change_timestamp,
    }
    title = _coerce_str(payload.get("title"))
    if title:
        change["name"] = title
    year = payload.get("year")
    if isinstance(year, int):
        change["year"] = str(year)
    elif isinstance(year, str):
        cleaned = year.strip()
        if cleaned:
            change["year"] = cleaned
    poster = _coerce_str(payload.get("poster"))
    if poster:
        change["poster"] = poster
    return change


async def _has_newer_stremio_series_job(
    db: AsyncSession,
    job: OutboxJob,
    item_id: str,
) -> bool:
    result = await db.execute(
        select(OutboxJob.id)
        .where(
            OutboxJob.user_id == job.user_id,
            OutboxJob.target_provider == "stremio",
            OutboxJob.job_type.in_(("push_watched", "remove_watched")),
            OutboxJob.status.in_(RETRYABLE_STATUSES),
            OutboxJob.created_at > job.created_at,
            or_(
                OutboxJob.payload["item_id"].as_string() == item_id,
                OutboxJob.payload["stremio_item_id"].as_string() == item_id,
            ),
        )
        .limit(1)
    )
    return result.scalars().first() is not None


def _build_stremio_remove_change(payload: dict[str, object]) -> dict[str, object]:
    item_id = _coerce_str(payload.get("item_id") or payload.get("stremio_item_id"))
    if not item_id:
        raise ValueError("Stremio sync requires item_id")
    media_type = _coerce_str(payload.get("media_type")) or "movie"
    stremio_type = "series" if media_type in {"tv", "series"} else "movie"
    timestamp = _format_stremio_datetime(datetime.now(timezone.utc))
    return {
        "_id": item_id,
        "type": stremio_type,
        "removed": True,
        "_ctime": timestamp,
        "_mtime": timestamp,
    }


async def _fetch_stremio_library_item(
    client: StremioClient, auth_key: str, item_id: str
) -> dict[str, object] | None:
    items = await client.get_library_items(auth_key, ids=[item_id])
    for item in items:
        if _coerce_str(item.get("_id") or item.get("id")) == item_id:
            return item
    return items[0] if items else None


async def _load_series_watched_ids(
    db: AsyncSession,
    user_id: str,
    item_id: str,
    watched_item_id: str | None,
) -> tuple[set[str], datetime | None]:
    show_media_item_id = await _resolve_show_media_item_id(db, item_id, watched_item_id)
    if not show_media_item_id:
        return set(), None
    result = await db.execute(
        select(
            EpisodeItem.season_number,
            EpisodeItem.episode_number,
            WatchedItem.watched_at,
        )
        .join(WatchedItem, WatchedItem.episode_item_id == EpisodeItem.id)
        .where(
            WatchedItem.user_id == user_id,
            EpisodeItem.show_media_item_id == show_media_item_id,
        )
    )
    watched_ids: set[str] = set()
    latest: datetime | None = None
    for season, episode, watched_at in result.all():
        if season is None or episode is None:
            continue
        if season < 1 or episode < 1:
            continue
        video_id = f"{item_id}:{season}:{episode}"
        watched_ids.add(video_id)
        if watched_at and (latest is None or watched_at > latest):
            latest = watched_at
    return watched_ids, latest


async def _resolve_show_media_item_id(
    db: AsyncSession,
    item_id: str,
    watched_item_id: str | None,
) -> str | None:
    if watched_item_id:
        result = await db.execute(
            select(EpisodeItem.show_media_item_id)
            .join(WatchedItem, WatchedItem.episode_item_id == EpisodeItem.id)
            .where(WatchedItem.id == watched_item_id)
            .limit(1)
        )
        show_media_item_id = result.scalars().first()
        if show_media_item_id:
            return str(show_media_item_id)
    result = await db.execute(select(MediaItem.id).where(MediaItem.imdb_id == item_id).limit(1))
    media_id = result.scalars().first()
    if media_id:
        return str(media_id)
    result = await db.execute(
        select(MediaItem.id).where(MediaItem.raw["stremio_id"].as_string() == item_id).limit(1)
    )
    media_id = result.scalars().first()
    if media_id:
        return str(media_id)
    return None


def _coerce_mapping(value: object) -> dict[str, object] | None:
    if isinstance(value, dict):
        return value
    return None


def _parse_stremio_datetime(value: object) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if isinstance(value, (int, float)):
        return _parse_stremio_timestamp(float(value))
    if isinstance(value, str):
        cleaned = value.strip()
        if not cleaned:
            return None
        if cleaned.isdigit():
            try:
                return _parse_stremio_timestamp(float(cleaned))
            except ValueError:
                return None
        if cleaned.endswith("Z"):
            cleaned = f"{cleaned[:-1]}+00:00"
        try:
            parsed = datetime.fromisoformat(cleaned)
        except ValueError:
            return None
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
    return None


def _parse_stremio_timestamp(value: float) -> datetime:
    if value > 10_000_000_000:
        value /= 1000.0
    return datetime.fromtimestamp(value, tz=timezone.utc)


def _select_stremio_ctime(existing_item: dict[str, object] | None, fallback: str) -> str:
    if not existing_item:
        return fallback
    existing_ctime = existing_item.get("_ctime")
    if isinstance(existing_ctime, str):
        cleaned = existing_ctime.strip()
        if cleaned:
            return cleaned
    parsed = _parse_stremio_datetime(existing_ctime)
    if parsed:
        return _format_stremio_datetime(parsed)
    return fallback


def _build_trakt_history_payload(
    payload: dict[str, object], watched_at: datetime
) -> dict[str, Any]:
    media_type = _coerce_str(payload.get("media_type")) or "movie"
    watched_at_value = watched_at.isoformat()
    if media_type == "movie":
        movie_ids = _normalize_trakt_ids(payload.get("movie_ids"))
        if not movie_ids:
            raise ValueError("Trakt sync requires movie ids")
        return {
            "movies": [
                {
                    "ids": movie_ids,
                    "watched_at": watched_at_value,
                }
            ]
        }

    show_ids = _normalize_trakt_ids(payload.get("show_ids"))
    episode_ids = _normalize_trakt_ids(payload.get("episode_ids"))
    season_number = _coerce_int(payload.get("season_number"))
    episode_number = _coerce_int(payload.get("episode_number"))
    if show_ids and season_number is not None and episode_number is not None:
        return {
            "shows": [
                {
                    "ids": show_ids,
                    "seasons": [
                        {
                            "number": season_number,
                            "episodes": [
                                {
                                    "number": episode_number,
                                    "watched_at": watched_at_value,
                                }
                            ],
                        }
                    ],
                }
            ]
        }
    if episode_ids:
        return {
            "episodes": [
                {
                    "ids": episode_ids,
                    "watched_at": watched_at_value,
                }
            ]
        }
    raise ValueError("Trakt sync requires show or episode ids")


def _build_trakt_watchlist_payload(payload: dict[str, object]) -> dict[str, Any]:
    media_type = _coerce_str(payload.get("media_type")) or "movie"
    if media_type in {"movie", "anime"}:
        movie_ids = _normalize_trakt_ids(payload.get("movie_ids"))
        if not movie_ids:
            movie_ids = _normalize_trakt_ids(
                {
                    "imdb": payload.get("imdb_id"),
                    "tmdb": payload.get("tmdb_id"),
                    "tvdb": payload.get("tvdb_id"),
                }
            )
        if not movie_ids:
            raise ValueError("Trakt watchlist sync requires movie ids")
        return {"movies": [{"ids": movie_ids}]}

    show_ids = _normalize_trakt_ids(payload.get("show_ids"))
    if not show_ids:
        show_ids = _normalize_trakt_ids(
            {
                "imdb": payload.get("imdb_id"),
                "tmdb": payload.get("tmdb_id"),
                "tvdb": payload.get("tvdb_id"),
            }
        )
    if not show_ids:
        raise ValueError("Trakt watchlist sync requires show ids")
    return {"shows": [{"ids": show_ids}]}


def _build_simkl_watchlist_payload(payload: dict[str, object]) -> dict[str, Any]:
    media_type = _coerce_str(payload.get("media_type")) or "movie"
    if media_type == "movie":
        movie_ids = _normalize_simkl_ids(payload.get("movie_ids"))
        if not movie_ids:
            movie_ids = _normalize_simkl_ids(
                {
                    "imdb": payload.get("imdb_id"),
                    "tmdb": payload.get("tmdb_id"),
                    "tvdb": payload.get("tvdb_id"),
                    "simkl": payload.get("simkl_id"),
                }
            )
        if not movie_ids:
            raise ValueError("SIMKL watchlist sync requires movie ids")
        return {"movies": [{"ids": movie_ids}]}

    show_ids = _normalize_simkl_ids(payload.get("show_ids"))
    if not show_ids:
        show_ids = _normalize_simkl_ids(
            {
                "imdb": payload.get("imdb_id"),
                "tmdb": payload.get("tmdb_id"),
                "tvdb": payload.get("tvdb_id"),
                "simkl": payload.get("simkl_id"),
            }
        )
    if not show_ids:
        raise ValueError("SIMKL watchlist sync requires show ids")
    container_key = "anime" if media_type == "anime" else "shows"
    return {container_key: [{"ids": show_ids}]}


def _build_simkl_drop_watchlist_payload(payload: dict[str, object]) -> dict[str, Any]:
    media_type = _coerce_str(payload.get("media_type")) or "movie"
    if media_type not in {"tv", "anime"}:
        raise ValueError("SIMKL drop watchlist requires tv or anime media type")
    show_ids = _normalize_simkl_ids(payload.get("show_ids"))
    if not show_ids:
        show_ids = _normalize_simkl_ids(
            {
                "imdb": payload.get("imdb_id"),
                "tmdb": payload.get("tmdb_id"),
                "tvdb": payload.get("tvdb_id"),
                "simkl": payload.get("simkl_id"),
            }
        )
    if not show_ids:
        raise ValueError("SIMKL drop watchlist requires show ids")
    container_key = "anime" if media_type == "anime" else "shows"
    return {"to": "dropped", container_key: [{"ids": show_ids}]}


def _build_trakt_remove_payload(payload: dict[str, object]) -> dict[str, Any]:
    media_type = _coerce_str(payload.get("media_type")) or "movie"
    if media_type == "movie":
        movie_ids = _normalize_trakt_ids(payload.get("movie_ids"))
        if not movie_ids:
            raise ValueError("Trakt remove requires movie ids")
        return {"movies": [{"ids": movie_ids}]}

    show_ids = _normalize_trakt_ids(payload.get("show_ids"))
    episode_ids = _normalize_trakt_ids(payload.get("episode_ids"))
    season_number = _coerce_int(payload.get("season_number"))
    episode_number = _coerce_int(payload.get("episode_number"))
    if episode_ids:
        return {"episodes": [{"ids": episode_ids}]}
    if show_ids and season_number is not None and episode_number is not None:
        return {
            "shows": [
                {
                    "ids": show_ids,
                    "seasons": [
                        {
                            "number": season_number,
                            "episodes": [{"number": episode_number}],
                        }
                    ],
                }
            ]
        }
    raise ValueError("Trakt remove requires show or episode ids")


def _build_trakt_remove_payload_for_id(history_id: str) -> dict[str, Any]:
    cleaned = history_id.strip()
    if not cleaned:
        raise ValueError("Trakt remove requires history id")
    if cleaned.isdigit():
        return {"ids": [int(cleaned)]}
    return {"ids": [cleaned]}


def _build_simkl_history_payload(
    payload: dict[str, object], watched_at: datetime
) -> dict[str, Any]:
    media_type = _coerce_str(payload.get("media_type")) or "movie"
    watched_at_value = watched_at.isoformat()
    if media_type == "movie":
        movie_ids = _normalize_simkl_ids(payload.get("movie_ids"))
        if not movie_ids:
            raise ValueError("SIMKL sync requires movie ids")
        return {
            "movies": [
                {
                    "ids": movie_ids,
                    "watched_at": watched_at_value,
                }
            ]
        }

    show_ids = _normalize_simkl_ids(payload.get("show_ids"))
    episode_ids = _normalize_simkl_ids(payload.get("episode_ids"))
    season_number = _coerce_int(payload.get("season_number"))
    episode_number = _coerce_int(payload.get("episode_number"))
    if show_ids and season_number is not None and episode_number is not None:
        return {
            "shows": [
                {
                    "ids": show_ids,
                    "seasons": [
                        {
                            "number": season_number,
                            "episodes": [
                                {
                                    "number": episode_number,
                                    "watched_at": watched_at_value,
                                }
                            ],
                        }
                    ],
                }
            ]
        }
    if episode_ids:
        return {
            "episodes": [
                {
                    "ids": episode_ids,
                    "watched_at": watched_at_value,
                }
            ]
        }
    raise ValueError("SIMKL sync requires show or episode ids")


def _build_simkl_remove_payload(payload: dict[str, object]) -> dict[str, Any]:
    media_type = _coerce_str(payload.get("media_type")) or "movie"
    if media_type == "movie":
        movie_ids = _normalize_simkl_ids(payload.get("movie_ids"))
        if not movie_ids:
            raise ValueError("SIMKL remove requires movie ids")
        return {"movies": [{"ids": movie_ids}]}

    show_ids = _normalize_simkl_ids(payload.get("show_ids"))
    episode_ids = _normalize_simkl_ids(payload.get("episode_ids"))
    season_number = _coerce_int(payload.get("season_number"))
    episode_number = _coerce_int(payload.get("episode_number"))
    if episode_ids:
        return {"episodes": [{"ids": episode_ids}]}
    if show_ids and season_number is not None and episode_number is not None:
        return {
            "shows": [
                {
                    "ids": show_ids,
                    "seasons": [
                        {
                            "number": season_number,
                            "episodes": [{"number": episode_number}],
                        }
                    ],
                }
            ]
        }
    raise ValueError("SIMKL remove requires show or episode ids")


def _build_trakt_rating_payload(payload: dict[str, object], rating: int) -> dict[str, Any]:
    media_type = _coerce_str(payload.get("media_type")) or "movie"
    if media_type == "movie":
        movie_ids = _normalize_trakt_ids(payload.get("movie_ids"))
        if not movie_ids:
            raise ValueError("Trakt rating requires movie ids")
        return {"movies": [{"ids": movie_ids, "rating": rating}]}

    show_ids = _normalize_trakt_ids(payload.get("show_ids"))
    episode_ids = _normalize_trakt_ids(payload.get("episode_ids"))
    season_number = _coerce_int(payload.get("season_number"))
    episode_number = _coerce_int(payload.get("episode_number"))
    if episode_ids:
        return {"episodes": [{"ids": episode_ids, "rating": rating}]}
    if show_ids and season_number is not None and episode_number is not None:
        return {
            "shows": [
                {
                    "ids": show_ids,
                    "seasons": [
                        {
                            "number": season_number,
                            "episodes": [{"number": episode_number, "rating": rating}],
                        }
                    ],
                }
            ]
        }
    raise ValueError("Trakt rating requires show or episode ids")


def _build_simkl_rating_payload(payload: dict[str, object], rating: int) -> dict[str, Any]:
    media_type = _coerce_str(payload.get("media_type")) or "movie"
    if media_type == "movie":
        movie_ids = _normalize_simkl_ids(payload.get("movie_ids"))
        if not movie_ids:
            raise ValueError("SIMKL rating requires movie ids")
        return {"movies": [{"ids": movie_ids, "rating": rating}]}

    show_ids = _normalize_simkl_ids(payload.get("show_ids"))
    episode_ids = _normalize_simkl_ids(payload.get("episode_ids"))
    season_number = _coerce_int(payload.get("season_number"))
    episode_number = _coerce_int(payload.get("episode_number"))
    if episode_ids:
        return {"episodes": [{"ids": episode_ids, "rating": rating}]}
    if show_ids and season_number is not None and episode_number is not None:
        return {
            "shows": [
                {
                    "ids": show_ids,
                    "seasons": [
                        {
                            "number": season_number,
                            "episodes": [{"number": episode_number, "rating": rating}],
                        }
                    ],
                }
            ]
        }
    raise ValueError("SIMKL rating requires show or episode ids")


def _normalize_trakt_ids(value: object) -> dict[str, object]:
    if not isinstance(value, dict):
        return {}
    ids: dict[str, object] = {}
    for key in ("imdb", "tmdb", "tvdb", "trakt"):
        if key not in value:
            continue
        entry = value.get(key)
        if entry is None or entry == "":
            continue
        if key == "imdb":
            ids[key] = str(entry).lower()
        else:
            ids[key] = entry
    return ids


def _normalize_simkl_ids(value: object) -> dict[str, object]:
    if not isinstance(value, dict):
        return {}
    ids: dict[str, object] = {}
    for key in ("imdb", "tmdb", "tvdb", "simkl"):
        if key not in value:
            continue
        entry = value.get(key)
        if entry is None or entry == "":
            continue
        if key == "imdb":
            ids[key] = str(entry).lower()
        else:
            parsed_int = _coerce_int(entry)
            ids[key] = parsed_int if parsed_int is not None else entry
    return ids


def _normalize_trakt_rating(value: object) -> int:
    rating = coerce_star_rating(value)
    if rating is None:
        raise ValueError("Trakt rating must be between 0.5 and 5.0 stars")
    normalized = int(round(rating * 2))
    if normalized < 1 or normalized > 10:
        raise ValueError("Trakt rating must be between 1 and 10")
    return normalized


def _normalize_simkl_rating(value: object) -> int:
    rating = coerce_star_rating(value)
    if rating is None:
        raise ValueError("SIMKL rating must be between 0.5 and 5.0 stars")
    normalized = int(round(rating * 2))
    if normalized < 1 or normalized > 10:
        raise ValueError("SIMKL rating must be between 1 and 10")
    return normalized


async def _find_trakt_history_for_day(
    client: TraktClient,
    access_token: str,
    payload: dict[str, object],
    watched_at: datetime,
) -> tuple[str, datetime] | None:
    media_type = _coerce_str(payload.get("media_type")) or "movie"
    history_type = "movies" if media_type == "movie" else "episodes"
    start_at, end_at = _day_bounds(watched_at)
    page = 1
    limit = 50
    while True:
        items, headers = await client.fetch_history(
            access_token,
            history_type=history_type,
            start_at=start_at,
            end_at=end_at,
            page=page,
            limit=limit,
        )
        if not items:
            return None
        for entry in items:
            matched_watched_at = _match_trakt_entry_for_payload(
                entry, payload, media_type, start_at, end_at
            )
            if matched_watched_at:
                history_id = _coerce_str(entry.get("id"))
                if history_id:
                    return history_id, matched_watched_at
        page_count = _parse_trakt_page_count(headers)
        if page_count is not None and page >= page_count:
            break
        if len(items) < limit:
            break
        page += 1
    return None


async def _resolve_trakt_history_match(
    client: TraktClient,
    access_token: str,
    payload: dict[str, object],
    watched_at: datetime,
    previous_watched_at: datetime | None,
) -> tuple[str, datetime] | None:
    seen_dates: set[date] = set()
    for candidate in (previous_watched_at, watched_at):
        if not candidate:
            continue
        candidate_date = candidate.date()
        if candidate_date in seen_dates:
            continue
        seen_dates.add(candidate_date)
        match = await _find_trakt_history_for_day(
            client,
            access_token,
            payload,
            candidate,
        )
        if match:
            return match
    return None


def _day_bounds(value: datetime) -> tuple[datetime, datetime]:
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    start_at = value.replace(hour=0, minute=0, second=0, microsecond=0)
    end_at = start_at + timedelta(days=1)
    return start_at, end_at


def _match_trakt_entry_for_payload(
    entry: object,
    payload: dict[str, object],
    media_type: str,
    start_at: datetime,
    end_at: datetime,
) -> datetime | None:
    if not isinstance(entry, dict):
        return None
    entry_watched_at = _parse_trakt_datetime(entry.get("watched_at"))
    if not entry_watched_at or not (start_at <= entry_watched_at < end_at):
        return None
    if media_type == "movie":
        movie_ids = _normalize_trakt_ids(payload.get("movie_ids"))
        entry_movie_ids = _extract_trakt_entry_ids(entry.get("movie"))
        if _ids_match(movie_ids, entry_movie_ids):
            return entry_watched_at
        return None

    episode_ids = _normalize_trakt_ids(payload.get("episode_ids"))
    show_ids = _normalize_trakt_ids(payload.get("show_ids"))
    entry_episode = entry.get("episode") if isinstance(entry.get("episode"), dict) else {}
    entry_show = entry.get("show") if isinstance(entry.get("show"), dict) else {}
    if episode_ids:
        if _ids_match(episode_ids, _extract_trakt_entry_ids(entry_episode)):
            return entry_watched_at
    if show_ids and _ids_match(show_ids, _extract_trakt_entry_ids(entry_show)):
        season_number = _coerce_int(payload.get("season_number"))
        episode_number = _coerce_int(payload.get("episode_number"))
        if season_number is None and episode_number is None:
            return entry_watched_at
        entry_season = _coerce_int(entry_episode.get("season"))
        entry_number = _coerce_int(entry_episode.get("number"))
        if season_number is not None and entry_season != season_number:
            return None
        if episode_number is not None and entry_number != episode_number:
            return None
        return entry_watched_at
    return None


def _extract_trakt_entry_ids(entry: object) -> dict[str, object]:
    if not isinstance(entry, dict):
        return {}
    ids = entry.get("ids")
    if not isinstance(ids, dict):
        return {}
    normalized: dict[str, object] = {}
    for key in ("imdb", "tmdb", "tvdb", "trakt"):
        value = ids.get(key)
        if value is None or value == "":
            continue
        if key == "imdb":
            normalized[key] = str(value).lower()
        else:
            normalized[key] = value
    return normalized


def _ids_match(left: dict[str, object], right: dict[str, object]) -> bool:
    if not left or not right:
        return False
    for key in ("trakt", "imdb", "tmdb", "tvdb"):
        if key not in left or key not in right:
            continue
        left_value = left[key]
        right_value = right[key]
        if key == "imdb":
            if str(left_value).lower() == str(right_value).lower():
                return True
        else:
            if str(left_value) == str(right_value):
                return True
    return False


def _parse_trakt_datetime(value: object) -> datetime | None:
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if not isinstance(value, str):
        return None
    cleaned = value.strip()
    if not cleaned:
        return None
    if cleaned.endswith("Z"):
        cleaned = f"{cleaned[:-1]}+00:00"
    try:
        parsed = datetime.fromisoformat(cleaned)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _parse_trakt_page_count(headers: dict[str, str]) -> int | None:
    value = headers.get("x-pagination-page-count") or headers.get("X-Pagination-Page-Count")
    if not value:
        return None
    try:
        return int(value)
    except ValueError:
        return None


async def _sync_local_watched_at(
    db: AsyncSession, payload: dict[str, object], watched_at: datetime
) -> None:
    watched_item_id = _coerce_str(payload.get("watched_item_id"))
    if not watched_item_id:
        return
    watched = await db.get(WatchedItem, watched_item_id)
    if not watched:
        return
    if watched_at.tzinfo is None:
        watched_at = watched_at.replace(tzinfo=timezone.utc)
    if watched.watched_at != watched_at:
        watched.watched_at = watched_at
        db.add(watched)


async def _ensure_trakt_access_token(
    db: AsyncSession,
    integration_id: str,
    secret_data: dict[str, object],
    client: TraktClient,
) -> str:
    access_token = secret_data.get("access_token")
    refresh_token = secret_data.get("refresh_token")
    if not isinstance(access_token, str) or not access_token:
        raise TraktError("Trakt access token is missing", status_code=401)
    if not isinstance(refresh_token, str) or not refresh_token:
        raise TraktError("Trakt refresh token is missing", status_code=401)
    expires_at = parse_expires_at(secret_data.get("expires_at"))
    if not is_token_expired(expires_at):
        return access_token
    token = await client.refresh_access_token(refresh_token)
    updated = dict(secret_data)
    updated.update(token_to_secret_payload(token))
    await _save_integration_secret(db, integration_id, updated)
    return token.access_token


async def _ensure_simkl_access_token(
    db: AsyncSession,
    integration_id: str,
    secret_data: dict[str, object],
    client: SimklClient,
) -> str:
    access_token = secret_data.get("access_token")
    refresh_token = secret_data.get("refresh_token")
    if not isinstance(access_token, str) or not access_token:
        raise SimklError("SIMKL access token is missing", status_code=401)
    if not isinstance(refresh_token, str) or not refresh_token:
        return access_token
    expires_at = parse_simkl_expires_at(secret_data.get("expires_at"))
    if not is_simkl_token_expired(expires_at):
        return access_token
    token = await client.refresh_access_token(refresh_token)
    updated = dict(secret_data)
    updated.update(simkl_token_to_secret_payload(token))
    await _save_integration_secret(db, integration_id, updated)
    return token.access_token


async def _save_integration_secret(
    db: AsyncSession, integration_id: str, secret_data: dict[str, object]
) -> None:
    encrypted = encrypt_value(json.dumps(secret_data))
    result = await db.execute(
        select(IntegrationSecret).where(IntegrationSecret.integration_id == integration_id)
    )
    secret = result.scalars().first()
    if not secret:
        secret = IntegrationSecret(integration_id=integration_id, secret_data=encrypted)
    else:
        secret.secret_data = encrypted
    db.add(secret)


def _extract_trakt_history_id(payload: object, media_type: str | None) -> str | None:
    if not isinstance(payload, dict):
        return None
    history = payload.get("history")
    if isinstance(history, dict):
        key = "movies" if media_type == "movie" else "episodes"
        entries = history.get(key)
        if isinstance(entries, list) and entries:
            return _coerce_str(entries[0])
        for fallback in ("movies", "episodes"):
            entries = history.get(fallback)
            if isinstance(entries, list) and entries:
                return _coerce_str(entries[0])
    if isinstance(history, list) and history:
        return _coerce_str(history[0])
    for key in ("id", "history_id", "historyId"):
        value = payload.get(key)
        if isinstance(value, (str, int)):
            return str(value)
    return None


def _extract_simkl_history_id(payload: object) -> str | None:
    if not isinstance(payload, dict):
        return None
    for key in ("history", "id", "history_id", "historyId"):
        value = payload.get(key)
        if isinstance(value, (str, int)):
            return str(value)
    ids = payload.get("ids")
    if isinstance(ids, dict):
        for key in ("simkl", "imdb", "tmdb", "tvdb"):
            value = ids.get(key)
            if isinstance(value, (str, int)):
                return str(value)
    return None


async def _update_watch_sync(
    db: AsyncSession,
    job: OutboxJob,
    status: str,
    error_message: str | None,
    external_id: str | None,
    now: datetime,
    resolved_rewatch: bool | None = None,
) -> None:
    payload = job.payload or {}
    watch_sync_id = payload.get("watch_sync_id")
    if not watch_sync_id:
        return
    result = await db.execute(select(WatchSync).where(WatchSync.id == str(watch_sync_id)))
    watch_sync = result.scalars().first()
    if not watch_sync:
        return
    if resolved_rewatch is not None:
        watch_sync.is_rewatch = resolved_rewatch
    watch_sync.status = status
    watch_sync.last_error = error_message
    if status == "succeeded":
        watch_sync.last_synced_at = now
        if external_id:
            watch_sync.external_id = external_id
    watch_sync.updated_at = now


def _classify_failure(status_code: int | None, message: str | None) -> str:
    if status_code in {400, 401, 403, 404, 405, 409, 422}:
        return "failed_permanent"
    if message and "unsupported" in message.lower():
        return "failed_permanent"
    return "failed_retryable"


def _next_retry_delay(attempts: int) -> timedelta:
    delay = min(60 * (2 ** max(attempts - 1, 0)), 3600)
    return timedelta(seconds=delay)


def _parse_datetime(value: object) -> datetime:
    if isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value)
        except ValueError:
            parsed = None
        if parsed:
            if parsed.tzinfo is None:
                return parsed.replace(tzinfo=timezone.utc)
            return parsed
    return datetime.now(timezone.utc)


def _parse_optional_datetime(value: object) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if isinstance(value, str):
        cleaned = value.strip()
        if not cleaned:
            return None
        try:
            parsed = datetime.fromisoformat(cleaned)
        except ValueError:
            return None
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
    return None


def _format_stremio_datetime(value: datetime) -> str:
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    utc_value = value.astimezone(timezone.utc)
    return utc_value.isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _coerce_str(value: object) -> str | None:
    if value is None:
        return None
    if isinstance(value, str):
        cleaned = value.strip()
        return cleaned or None
    if isinstance(value, (int, float)):
        return str(value)
    return None


def _coerce_int(value: object) -> int | None:
    if value is None:
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        cleaned = value.strip()
        if cleaned.isdigit():
            return int(cleaned)
    return None


def _normalize_tags(value: object) -> list[str] | None:
    if value is None:
        return None
    if isinstance(value, str):
        items = [entry.strip() for entry in value.split(",")]
    elif isinstance(value, list):
        items = [str(entry).strip() for entry in value]
    else:
        return None
    tags = [entry for entry in items if entry]
    return tags or None


def _normalize_like(value: object) -> bool | None:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        cleaned = value.strip().lower()
        if cleaned in {"true", "1", "yes"}:
            return True
        if cleaned in {"false", "0", "no"}:
            return False
    return None


def _safe_cookies(value: object) -> dict[str, str] | None:
    if not isinstance(value, dict):
        return None
    cookies: dict[str, str] = {}
    for key, entry in value.items():
        if entry is None:
            continue
        cookies[str(key)] = str(entry)
    return cookies or None


def _extract_entry_id(payload: object) -> str | None:
    if isinstance(payload, dict):
        for key in ("id", "entryId", "diaryEntryId", "logEntryId"):
            value = payload.get(key)
            if isinstance(value, (str, int)):
                return str(value)
        entry = payload.get("entry")
        if isinstance(entry, dict):
            for key in ("id", "entryId", "logEntryId"):
                value = entry.get(key)
                if isinstance(value, (str, int)):
                    return str(value)
    return None


def _format_letterboxd_error(error: LetterboxdError) -> str:
    message = str(error)
    status_code = error.status_code
    response_body = error.response_body
    if response_body:
        response_body = _shorten(response_body)
    if status_code and response_body:
        return f"{message} (status={status_code}, body={response_body})"
    if status_code:
        return f"{message} (status={status_code})"
    if response_body:
        return f"{message} (body={response_body})"
    return message


def _format_trakt_error(error: TraktError) -> str:
    message = str(error)
    status_code = error.status_code
    response_body = error.response_body
    if response_body:
        response_body = _shorten(response_body)
    if status_code and response_body:
        return f"{message} (status={status_code}, body={response_body})"
    if status_code:
        return f"{message} (status={status_code})"
    if response_body:
        return f"{message} (body={response_body})"
    return message


def _format_simkl_error(error: SimklError) -> str:
    message = str(error)
    status_code = error.status_code
    response_body = error.response_body
    if response_body:
        response_body = _shorten(response_body)
    if status_code and response_body:
        return f"{message} (status={status_code}, body={response_body})"
    if status_code:
        return f"{message} (status={status_code})"
    if response_body:
        return f"{message} (body={response_body})"
    return message


def _format_publicmetadb_error(error: PublicMetaDbError) -> str:
    message = str(error)
    status_code = error.status_code
    response_body = error.response_body
    if response_body:
        response_body = _shorten(response_body)
    if status_code and response_body:
        return f"{message} (status={status_code}, body={response_body})"
    if status_code:
        return f"{message} (status={status_code})"
    if response_body:
        return f"{message} (body={response_body})"
    return message


def _format_stremio_error(error: StremioError) -> str:
    message = str(error)
    status_code = error.status_code
    response_body = error.response_body
    if response_body:
        response_body = _shorten(response_body)
    if status_code and response_body:
        return f"{message} (status={status_code}, body={response_body})"
    if status_code:
        return f"{message} (status={status_code})"
    if response_body:
        return f"{message} (body={response_body})"
    return message


def _format_anilist_error(error: AniListError) -> str:
    message = str(error)
    status_code = error.status_code
    response_body = error.response_body
    if response_body:
        response_body = _shorten(response_body)
    if status_code and response_body:
        return f"{message} (status={status_code}, body={response_body})"
    if status_code:
        return f"{message} (status={status_code})"
    if response_body:
        return f"{message} (body={response_body})"
    return message


def _shorten(value: str, limit: int = 300) -> str:
    trimmed = value.strip()
    if len(trimmed) > limit:
        return f"{trimmed[:limit]}..."
    return trimmed
