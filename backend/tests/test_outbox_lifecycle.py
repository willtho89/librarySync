"""Outbox job lifecycle: dedupe keys, successors, stale recovery, attempt cap, shutdown."""

from contextlib import ExitStack
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
import pytest_asyncio
from librarysync.connectors.services.trakt import TraktError
from librarysync.core import shutdown
from librarysync.core.watch_pipeline import enqueue_outbox_job
from librarysync.db.models import Base, OutboxJob, User
from librarysync.jobs import process_outbox
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

USER = "user-1"


@pytest_asyncio.fixture
async def factory():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    session_factory = async_sessionmaker(engine, autoflush=False, expire_on_commit=False)
    async with session_factory() as db:
        db.add(User(id=USER, username="one", password_hash="unused"))
        await db.commit()
    yield session_factory
    await engine.dispose()


def _outbox_patches(factory, deliver=None, deliver_batch=None):
    stack = ExitStack()
    stack.enter_context(patch.object(process_outbox, "SessionLocal", factory))
    stack.enter_context(patch.object(process_outbox, "init_session_factory", lambda: None))
    stack.enter_context(
        patch.object(process_outbox, "load_blocked_outbox_users", AsyncMock(return_value=set()))
    )
    stack.enter_context(patch.object(process_outbox.RATE_LIMITER, "try_acquire", AsyncMock(return_value=None)))
    if deliver is not None:
        stack.enter_context(patch.object(process_outbox.OUTBOX_DISPATCHER, "deliver", deliver))
    if deliver_batch is not None:
        stack.enter_context(patch.object(process_outbox, "_deliver_batch", deliver_batch))
    return stack


async def _jobs(factory) -> list[OutboxJob]:
    async with factory() as db:
        result = await db.execute(select(OutboxJob).order_by(OutboxJob.created_at))
        return list(result.scalars().all())


async def _enqueue(factory, job_type: str, payload: dict) -> str:
    async with factory() as db:
        job = await enqueue_outbox_job(
            db, user_id=USER, target_provider="trakt", job_type=job_type, payload=payload
        )
        await db.commit()
        return job.id


@pytest.mark.asyncio
async def test_batched_jobs_release_dedupe_keys_so_items_can_resync(factory):
    for watched_id in ("w1", "w2"):
        await _enqueue(factory, "push_watched", {"watched_item_id": watched_id})

    with _outbox_patches(factory, deliver_batch=AsyncMock(return_value=201)):
        assert await process_outbox.process_outbox_once() == 2

    jobs = await _jobs(factory)
    assert {job.status for job in jobs} == {"succeeded"}
    assert all(job.dedupe_key is None for job in jobs)
    # Re-enqueueing the same item must not collide with the delivered job.
    await _enqueue(factory, "push_watched", {"watched_item_id": "w1"})
    assert len(await _jobs(factory)) == 3


@pytest.mark.asyncio
async def test_rating_change_during_delivery_queues_successor(factory):
    first = await _enqueue(factory, "push_rating", {"watched_item_id": "w1", "rating": 3.0})
    seen_payloads: list[dict] = []

    async def _deliver(db, job):
        seen_payloads.append(dict(job.payload))
        if len(seen_payloads) == 1:
            # The user changes the rating while the first delivery is in flight.
            await _enqueue(factory, "push_rating", {"watched_item_id": "w1", "rating": 4.5})
            # Re-enqueueing the identical payload again only refreshes the successor.
            await _enqueue(factory, "push_rating", {"watched_item_id": "w1", "rating": 4.5})
        return SimpleNamespace(response_code=201, external_id=None, resolved_rewatch=None)

    with _outbox_patches(factory, deliver=_deliver):
        assert await process_outbox.process_outbox_once() == 1
        jobs = await _jobs(factory)
        assert [job.status for job in jobs] == ["succeeded", "pending"]
        assert await process_outbox.process_outbox_once() == 1

    assert [payload["rating"] for payload in seen_payloads] == [3.0, 4.5]
    assert (await _jobs(factory))[0].id == first


@pytest.mark.asyncio
async def test_identical_push_watched_during_delivery_is_not_duplicated(factory):
    await _enqueue(factory, "push_watched", {"watched_item_id": "w1"})

    async def _deliver(db, job):
        await _enqueue(factory, "push_watched", {"watched_item_id": "w1"})
        return SimpleNamespace(response_code=201, external_id=None, resolved_rewatch=None)

    with _outbox_patches(factory, deliver=_deliver):
        await process_outbox.process_outbox_once()

    assert [job.status for job in await _jobs(factory)] == ["succeeded"]


@pytest.mark.asyncio
async def test_successor_is_not_claimed_while_predecessor_in_flight(factory):
    await _enqueue(factory, "push_rating", {"watched_item_id": "w1", "rating": 3.0})
    async with factory() as db:
        job = (await db.execute(select(OutboxJob))).scalars().one()
        job.status = "in_progress"
        job.updated_at = datetime.now(timezone.utc)
        await db.commit()
    await _enqueue(factory, "push_rating", {"watched_item_id": "w1", "rating": 4.0})

    with _outbox_patches(factory, deliver=AsyncMock()):
        assert await process_outbox.process_outbox_once() == 0


@pytest.mark.asyncio
async def test_failed_job_with_waiting_successor_is_superseded(factory):
    await _enqueue(factory, "push_rating", {"watched_item_id": "w1", "rating": 3.0})

    async def _deliver(db, job):
        await _enqueue(factory, "push_rating", {"watched_item_id": "w1", "rating": 4.0})
        raise TraktError("unavailable", status_code=503)

    with _outbox_patches(factory, deliver=_deliver):
        await process_outbox.process_outbox_once()

    jobs = await _jobs(factory)
    assert [job.status for job in jobs] == [process_outbox.SUPERSEDED_STATUS, "pending"]
    assert jobs[0].dedupe_key is None


@pytest.mark.asyncio
async def test_stale_in_progress_jobs_are_recovered(factory):
    await _enqueue(factory, "push_watched", {"watched_item_id": "w1"})
    async with factory() as db:
        job = (await db.execute(select(OutboxJob))).scalars().one()
        job.status = "in_progress"
        job.updated_at = datetime.now(timezone.utc) - timedelta(hours=2)
        await db.commit()
    deliver = AsyncMock(return_value=SimpleNamespace(response_code=201, external_id=None, resolved_rewatch=None))

    with _outbox_patches(factory, deliver=deliver):
        assert await process_outbox.process_outbox_once() == 1

    deliver.assert_awaited_once()
    assert [job.status for job in await _jobs(factory)] == ["succeeded"]


@pytest.mark.asyncio
async def test_retryable_failures_give_up_after_max_attempts(factory, monkeypatch):
    monkeypatch.setattr(process_outbox, "settings", SimpleNamespace(**{
        **vars(process_outbox.settings), "outbox_max_attempts": 2,
    }))
    await _enqueue(factory, "push_watched", {"watched_item_id": "w1"})
    deliver = AsyncMock(side_effect=TraktError("unavailable", status_code=503))

    with _outbox_patches(factory, deliver=deliver):
        await process_outbox.process_outbox_once()
        async with factory() as db:
            job = (await db.execute(select(OutboxJob))).scalars().one()
            assert job.status == "failed_retryable"
            job.run_after = None
            await db.commit()
        await process_outbox.process_outbox_once()

    job = (await _jobs(factory))[0]
    assert job.status == "failed_permanent"
    assert job.last_error.startswith("Gave up after 2 attempts")
    assert job.dedupe_key is None


@pytest.mark.asyncio
async def test_shutdown_releases_unprocessed_claimed_jobs(factory):
    for watched_id in ("w1", "w2", "w3"):
        await _enqueue(factory, "push_watched", {"watched_item_id": watched_id})
    delivered: list[str] = []

    async def _deliver(db, job):
        delivered.append(job.payload["watched_item_id"])
        shutdown.request_shutdown()
        return SimpleNamespace(response_code=201, external_id=None, resolved_rewatch=None)

    # Keep the jobs on the single-job path so they run one at a time.
    with _outbox_patches(factory, deliver=_deliver), patch.object(process_outbox, "BATCHABLE_PROVIDERS", set()):
        try:
            await process_outbox.process_outbox_once()
        finally:
            shutdown.reset_shutdown()

    statuses = sorted(job.status for job in await _jobs(factory))
    assert len(delivered) == 1
    assert statuses == ["pending", "pending", "succeeded"]


@pytest.mark.asyncio
async def test_rejected_token_is_expired_and_retried_once(factory):
    await _enqueue(factory, "push_watched", {"watched_item_id": "w1"})
    deliver = AsyncMock(side_effect=TraktError("unauthorized", status_code=401))
    expire = AsyncMock(return_value=True)

    with _outbox_patches(factory, deliver=deliver), patch.object(process_outbox, "expire_access_token", expire):
        await process_outbox.process_outbox_once()

    expire.assert_awaited_once()
    assert (await _jobs(factory))[0].status == "failed_retryable"
