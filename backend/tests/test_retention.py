from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest
import pytest_asyncio
from librarysync import config
from librarysync.db.models import Base, MetadataLookupRequest, OutboxJob, User
from librarysync.jobs import retention
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

NOW = datetime(2026, 10, 7, 12, 0, tzinfo=timezone.utc)
OLD = NOW - timedelta(days=200)


@pytest_asyncio.fixture
async def factory(monkeypatch):
    monkeypatch.setattr(
        retention, "settings", replace(config.settings, outbox_retention_days=90, lookup_retention_days=30)
    )
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    session_factory = async_sessionmaker(engine, autoflush=False, expire_on_commit=False)
    async with session_factory() as db:
        db.add(User(id="u", username="u", password_hash="x"))
        await db.commit()
    yield session_factory
    await engine.dispose()


def _job(job_id: str, status: str, updated_at: datetime) -> OutboxJob:
    return OutboxJob(
        id=job_id,
        user_id="u",
        target_provider="trakt",
        job_type="push_watched",
        payload={},
        status=status,
        updated_at=updated_at,
    )


@pytest.mark.asyncio
async def test_prunes_only_old_finished_rows(factory):
    async with factory() as db:
        db.add_all(
            [
                _job("old-done", "succeeded", OLD),
                _job("old-superseded", "superseded", OLD),
                _job("old-retrying", "failed_retryable", OLD),
                _job("old-pending", "pending", OLD),
                _job("recent-done", "succeeded", NOW),
                MetadataLookupRequest(
                    id="old-lookup", user_id="u", query="q", query_type="title", status="completed", updated_at=OLD
                ),
                MetadataLookupRequest(
                    id="old-pending-lookup",
                    user_id="u",
                    query="q",
                    query_type="title",
                    status="pending",
                    updated_at=OLD,
                ),
            ]
        )
        await db.commit()

    async with factory() as db:
        removed = await retention.prune_finished_rows(db, NOW)

    assert removed == {"outbox jobs": 2, "metadata lookups": 1}
    async with factory() as db:
        jobs = set((await db.execute(select(OutboxJob.id))).scalars().all())
        lookups = set((await db.execute(select(MetadataLookupRequest.id))).scalars().all())
    assert jobs == {"old-retrying", "old-pending", "recent-done"}
    assert lookups == {"old-pending-lookup"}


@pytest.mark.asyncio
async def test_zero_days_disables_pruning(factory, monkeypatch):
    monkeypatch.setattr(
        retention, "settings", replace(config.settings, outbox_retention_days=0, lookup_retention_days=0)
    )
    async with factory() as db:
        db.add(_job("old-done", "succeeded", OLD))
        await db.commit()
    async with factory() as db:
        assert await retention.prune_finished_rows(db, NOW) == {"outbox jobs": 0, "metadata lookups": 0}
