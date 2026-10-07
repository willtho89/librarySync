"""Daily pruning of finished queue rows that otherwise grow without bound.

watch_events and watch_state_receipts are deliberately kept: imports and the
Watch State inbox use them to recognise entries they have already processed.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy import delete
from sqlalchemy.ext.asyncio import AsyncSession

from librarysync.config import settings
from librarysync.core.scheduler import (
    claim_scheduled_job,
    complete_scheduled_job,
    fail_scheduled_job,
)
from librarysync.db.models import MetadataLookupRequest, OutboxJob
from librarysync.db.session import SessionLocal, init_session_factory

logger = logging.getLogger(__name__)

RETENTION_JOB = "retention"
RETENTION_INTERVAL = timedelta(days=1)
RETENTION_LEASE = timedelta(hours=1)
RETENTION_RETRY_DELAY = timedelta(hours=1)
FINISHED_OUTBOX_STATUSES = ("succeeded", "failed_permanent", "superseded")
FINISHED_LOOKUP_STATUSES = ("completed", "failed")


async def process_retention_once() -> int:
    init_session_factory()
    async with SessionLocal() as db:
        job = await claim_scheduled_job(db, RETENTION_JOB, RETENTION_INTERVAL, RETENTION_LEASE)
        if not job:
            return 0
        job_name = job.name
        try:
            removed = await prune_finished_rows(db, datetime.now(timezone.utc))
        except Exception:
            logger.exception("Retention pruning failed")
            await fail_scheduled_job(db, job_name, RETENTION_RETRY_DELAY)
            return 0
        await complete_scheduled_job(db, job, RETENTION_INTERVAL)
    if any(removed.values()):
        logger.info("Retention pruned %s", ", ".join(f"{count} {name}" for name, count in removed.items()))
    return 1


async def prune_finished_rows(db: AsyncSession, now: datetime) -> dict[str, int]:
    removed = {"outbox jobs": 0, "metadata lookups": 0}
    if settings.outbox_retention_days > 0:
        cutoff = now - timedelta(days=settings.outbox_retention_days)
        # sync_attempts rows cascade with their job.
        result = await db.execute(
            delete(OutboxJob).where(
                OutboxJob.status.in_(FINISHED_OUTBOX_STATUSES),
                OutboxJob.updated_at < cutoff,
            )
        )
        removed["outbox jobs"] = result.rowcount or 0
    if settings.lookup_retention_days > 0:
        cutoff = now - timedelta(days=settings.lookup_retention_days)
        # Candidates cascade with their request.
        result = await db.execute(
            delete(MetadataLookupRequest).where(
                MetadataLookupRequest.status.in_(FINISHED_LOOKUP_STATUSES),
                MetadataLookupRequest.updated_at < cutoff,
            )
        )
        removed["metadata lookups"] = result.rowcount or 0
    await db.commit()
    return removed
