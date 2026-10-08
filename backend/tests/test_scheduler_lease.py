from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pytest
import pytest_asyncio
from librarysync.core import scheduler
from librarysync.db.models import Base, ScheduledJob
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

NOW = datetime(2026, 10, 7, 12, 0, tzinfo=timezone.utc)


@pytest_asyncio.fixture
async def factory():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    yield async_sessionmaker(engine, autoflush=False, expire_on_commit=False)
    await engine.dispose()


async def _take_over(factory, owner: str) -> None:
    async with factory() as db:
        job = await db.get(ScheduledJob, "backfill")
        job.lease_owner = owner
        job.lease_until = NOW + timedelta(hours=1)
        await db.commit()


@pytest.mark.asyncio
async def test_worker_that_lost_its_lease_does_not_clobber_the_new_owner(factory):
    async with factory() as db_a:
        with patch.object(scheduler, "worker_instance_id", return_value="worker-a"):
            job = await scheduler.claim_scheduled_job(db_a, "backfill", timedelta(hours=1), timedelta(minutes=5), NOW)
            assert job is not None
            await _take_over(factory, "worker-b")
            assert await scheduler.extend_scheduled_job(db_a, job, timedelta(minutes=5), NOW) is False
            await scheduler.complete_scheduled_job(db_a, job, timedelta(hours=1), NOW)

    async with factory() as db:
        stored = await db.get(ScheduledJob, "backfill")
        assert stored.lease_owner == "worker-b"
        assert stored.last_run_at is None


@pytest.mark.asyncio
async def test_owner_completes_and_clears_lease(factory):
    async with factory() as db:
        with patch.object(scheduler, "worker_instance_id", return_value="worker-a"):
            job = await scheduler.claim_scheduled_job(db, "backfill", timedelta(hours=1), timedelta(minutes=5), NOW)
            assert await scheduler.extend_scheduled_job(db, job, timedelta(minutes=5), NOW) is True
            await scheduler.complete_scheduled_job(db, job, timedelta(hours=1), NOW)

    async with factory() as db:
        stored = await db.get(ScheduledJob, "backfill")
        assert stored.lease_owner is None
        assert stored.next_run_at.replace(tzinfo=timezone.utc) == NOW + timedelta(hours=1)


@pytest.mark.asyncio
async def test_active_lease_excludes_a_second_worker_after_reload(factory):
    async with factory() as db:
        assert await scheduler.claim_scheduled_job(db, "backfill", timedelta(hours=1), timedelta(minutes=5), NOW)
    async with factory() as db:
        claimed = await scheduler.claim_scheduled_job(db, "backfill", timedelta(hours=1), timedelta(minutes=5), NOW)
        assert claimed is None
