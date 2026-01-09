from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, Query
from fastapi.responses import JSONResponse
from sqlalchemy import delete, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from librarysync.api.deps import get_admin_api_key, get_db
from librarysync.db.models import OutboxJob, ScheduledJob
from librarysync.jobs.watchlist_refresh import WATCHLIST_REFRESH_JOB

router = APIRouter(prefix="/api/admin", tags=["admin"])


@router.post(
    "/reset-outbox-jobs",
    summary="Reset stuck outbox jobs",
    description=(
        "Reset outbox jobs that have been in 'in_progress' status for longer "
        "than the specified timeout."
    ),
)
async def reset_outbox_jobs(
    timeout_minutes: int = Query(10, description="Timeout in minutes for stuck jobs", ge=1),
    db: AsyncSession = Depends(get_db),
    _: str = Depends(get_admin_api_key),
) -> JSONResponse:
    cutoff_time = datetime.now(timezone.utc) - timedelta(minutes=timeout_minutes)

    # Query for stuck jobs
    query = select(OutboxJob).where(
        OutboxJob.status == "in_progress",
        OutboxJob.updated_at < cutoff_time,
    )
    result = await db.execute(query)
    stuck_jobs = result.scalars().all()

    if not stuck_jobs:
        return JSONResponse({"message": "No stuck jobs found", "reset_count": 0})

    # Reset the jobs
    job_ids = [job.id for job in stuck_jobs]
    await db.execute(
        update(OutboxJob)
        .where(OutboxJob.id.in_(job_ids))
        .values(
            status="pending",
            run_after=datetime.now(timezone.utc),
            attempts=0,
            last_error=None,
        )
    )
    await db.commit()

    return JSONResponse(
        {
            "message": f"Reset {len(job_ids)} stuck jobs",
            "reset_count": len(job_ids),
            "job_ids": job_ids,
        }
    )


@router.delete(
    "/purge-jobs",
    summary="Purge outbox jobs",
    description=(
        "Delete outbox jobs matching the specified criteria. "
        "Use with caution - this is irreversible."
    ),
)
async def purge_jobs(
    status: str | None = Query(None, description="Filter by job status"),
    target_provider: str | None = Query(None, description="Filter by target provider"),
    older_than_hours: int | None = Query(
        None, description="Only delete jobs older than this many hours", ge=1
    ),
    limit: int = Query(1000, description="Maximum number of jobs to delete", ge=1, le=10000),
    dry_run: bool = Query(False, description="If true, return count without deleting"),
    db: AsyncSession = Depends(get_db),
    _: str = Depends(get_admin_api_key),
) -> JSONResponse:
    # Build the base query
    query = select(OutboxJob)

    # Apply filters
    conditions = []
    if status:
        conditions.append(OutboxJob.status == status)
    if target_provider:
        conditions.append(OutboxJob.target_provider == target_provider)
    if older_than_hours:
        cutoff_time = datetime.now(timezone.utc) - timedelta(hours=older_than_hours)
        conditions.append(OutboxJob.created_at < cutoff_time)

    if conditions:
        query = query.where(*conditions)

    # Apply limit
    query = query.limit(limit)

    # Get matching jobs
    result = await db.execute(query)
    matching_jobs = result.scalars().all()
    job_ids = [job.id for job in matching_jobs]

    if not job_ids:
        return JSONResponse(
            {
                "message": "No matching jobs found",
                "purge_count": 0,
                "job_ids": [],
            }
        )

    if dry_run:
        return JSONResponse(
            {
                "message": f"Dry run: would delete {len(job_ids)} jobs",
                "purge_count": 0,
                "job_ids": job_ids,
                "filters_applied": {
                    "status": status,
                    "target_provider": target_provider,
                    "older_than_hours": older_than_hours,
                    "limit": limit,
                },
            }
        )

    # Delete the jobs
    await db.execute(delete(OutboxJob).where(OutboxJob.id.in_(job_ids)))
    await db.commit()

    return JSONResponse(
        {
            "message": f"Successfully purged {len(job_ids)} jobs",
            "purge_count": len(job_ids),
            "job_ids": job_ids,
            "filters_applied": {
                "status": status,
                "target_provider": target_provider,
                "older_than_hours": older_than_hours,
                "limit": limit,
            },
        }
    )


@router.post(
    "/watchlist-refresh",
    summary="Schedule watchlist refresh",
    description="Schedule a watchlist refresh run for the worker to execute.",
)
async def schedule_watchlist_refresh(
    db: AsyncSession = Depends(get_db),
    _: str = Depends(get_admin_api_key),
) -> JSONResponse:
    now = datetime.now(timezone.utc)
    result = await db.execute(
        select(ScheduledJob).where(ScheduledJob.name == WATCHLIST_REFRESH_JOB)
    )
    job = result.scalars().first()
    if not job:
        job = ScheduledJob(name=WATCHLIST_REFRESH_JOB, next_run_at=now)
        db.add(job)
    else:
        job.next_run_at = now
        job.lease_until = None
        job.lease_owner = None
        job.updated_at = now
    await db.commit()
    return JSONResponse(
        {
            "message": "Watchlist refresh scheduled",
            "next_run_at": job.next_run_at.isoformat() if job.next_run_at else None,
        }
    )
