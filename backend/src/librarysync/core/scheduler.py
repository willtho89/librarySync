from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from librarysync.core.worker_identity import worker_instance_id
from librarysync.db.models import ScheduledJob


async def claim_scheduled_job(
    db: AsyncSession,
    name: str,
    interval: timedelta,
    lease_duration: timedelta,
    now: datetime | None = None,
) -> ScheduledJob | None:
    if now is None:
        now = datetime.now(timezone.utc)
    async with db.begin():
        result = await db.execute(
            select(ScheduledJob).where(ScheduledJob.name == name).with_for_update()
        )
        job = result.scalars().first()
        if not job:
            job = ScheduledJob(name=name, next_run_at=now)
            db.add(job)
            await db.flush()
        if job.lease_until and job.lease_until > now:
            return None
        next_run = job.next_run_at or now
        if next_run > now:
            return None
        job.lease_until = now + lease_duration
        job.lease_owner = worker_instance_id()
        job.updated_at = now
        return job


async def complete_scheduled_job(
    db: AsyncSession,
    job: ScheduledJob,
    interval: timedelta,
    now: datetime | None = None,
) -> None:
    if now is None:
        now = datetime.now(timezone.utc)
    job.last_run_at = now
    job.next_run_at = now + interval
    job.lease_until = None
    job.lease_owner = None
    job.updated_at = now
    await db.commit()


async def extend_scheduled_job(
    db: AsyncSession,
    job: ScheduledJob,
    lease_duration: timedelta,
    now: datetime | None = None,
) -> None:
    if now is None:
        now = datetime.now(timezone.utc)
    job.lease_until = now + lease_duration
    job.updated_at = now
    await db.commit()


async def release_scheduled_job(
    db: AsyncSession,
    job: ScheduledJob,
    retry_delay: timedelta,
    now: datetime | None = None,
) -> None:
    if now is None:
        now = datetime.now(timezone.utc)
    job.next_run_at = now + retry_delay
    job.lease_until = None
    job.lease_owner = None
    job.updated_at = now
    await db.commit()


async def fail_scheduled_job(
    db: AsyncSession,
    job_name: str,
    retry_delay: timedelta,
    now: datetime | None = None,
) -> None:
    """Roll back a broken session and reschedule the job for a later retry.

    Used from exception handlers where the failure may have poisoned the
    session transaction (e.g. an IntegrityError during flush). The job row is
    re-fetched after the rollback because the rollback expires the previously
    loaded instance.
    """
    await db.rollback()
    job = await db.get(ScheduledJob, job_name)
    if job is None:
        return
    await release_scheduled_job(db, job, retry_delay, now)
