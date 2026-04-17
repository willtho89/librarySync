import logging
from datetime import timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from librarysync.config import settings
from librarysync.core.external_catalog import (
    mark_external_catalog_refresh_failed,
    refresh_external_catalog,
)
from librarysync.core.scheduler import (
    claim_scheduled_job,
    complete_scheduled_job,
    extend_scheduled_job,
    release_scheduled_job,
)
from librarysync.db.models import ScheduledJob, StremioExternalCatalog
from librarysync.db.session import SessionLocal, init_session_factory

logger = logging.getLogger(__name__)

EXTERNAL_CATALOG_REFRESH_JOB = "external_catalog_refresh"
EXTERNAL_CATALOG_REFRESH_INTERVAL = timedelta(hours=settings.external_catalog_refresh_hours)
EXTERNAL_CATALOG_REFRESH_LEASE = timedelta(minutes=30)
EXTERNAL_CATALOG_REFRESH_RETRY_DELAY = timedelta(minutes=5)


async def process_external_catalog_refresh_once() -> int:
    init_session_factory()
    async with SessionLocal() as db:
        job = await claim_scheduled_job(
            db,
            EXTERNAL_CATALOG_REFRESH_JOB,
            EXTERNAL_CATALOG_REFRESH_INTERVAL,
            EXTERNAL_CATALOG_REFRESH_LEASE,
        )
        if not job:
            return 0
        try:
            refreshed = await run_external_catalog_refresh(db, job)
        except Exception:
            logger.exception("External catalog refresh failed")
            await release_scheduled_job(db, job, EXTERNAL_CATALOG_REFRESH_RETRY_DELAY)
            return 0
        await complete_scheduled_job(db, job, EXTERNAL_CATALOG_REFRESH_INTERVAL)
    return refreshed


async def run_external_catalog_refresh(db: AsyncSession, job: ScheduledJob) -> int:
    logger.info("Starting external catalog refresh")
    result = await db.execute(
        select(StremioExternalCatalog)
        .where(StremioExternalCatalog.enabled.is_(True))
        .order_by(StremioExternalCatalog.updated_at.asc())
    )
    catalogs = result.scalars().all()
    refreshed = 0

    for catalog in catalogs:
        try:
            await refresh_external_catalog(db, catalog)
            await db.commit()
            refreshed += 1
        except Exception as exc:
            await db.rollback()
            await mark_external_catalog_refresh_failed(db, catalog, exc)
            await db.commit()
            logger.warning("External catalog refresh failed for %s: %s", catalog.slug, exc)
        await extend_scheduled_job(db, job, EXTERNAL_CATALOG_REFRESH_LEASE)

    logger.info("Finished external catalog refresh")
    return refreshed
