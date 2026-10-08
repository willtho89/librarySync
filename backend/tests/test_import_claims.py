"""Claiming quick-import and import-all runs against a real (SQLite) database."""

from dataclasses import replace
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
import pytest_asyncio
from librarysync.core.import_all import (
    IMPORT_ALL_INDEX_KEY,
    IMPORT_ALL_LEASE_OWNER_KEY,
    IMPORT_ALL_LEASE_UNTIL_KEY,
    IMPORT_ALL_STATUS_KEY,
    build_import_all_config,
)
from librarysync.core.import_control import QUICK_IMPORT_REQUESTED_KEY
from librarysync.db.models import Base, Integration, User
from librarysync.jobs import imports
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

NOW = datetime.now(timezone.utc)


@pytest_asyncio.fixture
async def factory():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    yield async_sessionmaker(engine, autoflush=False, expire_on_commit=False)
    await engine.dispose()


async def _add_run(factory, user_id: str, config: dict, updated_at: datetime) -> None:
    async with factory() as db:
        db.add(User(id=user_id, username=user_id, password_hash="x"))
        db.add(
            Integration(id=f"run-{user_id}", user_id=user_id, provider="system", config=config, updated_at=updated_at)
        )
        await db.commit()


@pytest.mark.asyncio
async def test_quick_import_claim_is_not_starved_by_idle_runs(factory):
    # Many older runs with no schedule are never due and never get updated_at bumped.
    for index in range(8):
        await _add_run(factory, f"idle-{index}", {}, NOW - timedelta(days=30, minutes=index))
    await _add_run(factory, "due", {QUICK_IMPORT_REQUESTED_KEY: NOW.isoformat()}, NOW)

    with (
        patch.object(imports, "worker_instance_id", return_value="worker-a"),
        patch.object(imports, "build_import_all_queue", new=AsyncMock(return_value=["trakt"])),
    ):
        async with factory() as db:
            runs = await imports._claim_quick_import_runs(db, 1)

    assert [run.user_id for run in runs] == ["due"]


@pytest.mark.asyncio
async def test_import_all_run_leased_by_live_worker_is_not_claimed_twice(factory):
    config = {
        IMPORT_ALL_STATUS_KEY: "in_progress",
        IMPORT_ALL_LEASE_OWNER_KEY: "worker-b",
        IMPORT_ALL_LEASE_UNTIL_KEY: (NOW + timedelta(minutes=30)).isoformat(),
    }
    await _add_run(factory, "busy", config, NOW)

    with patch.object(imports, "worker_instance_id", return_value="worker-a"):
        async with factory() as db:
            assert await imports._claim_import_all_runs(db, 1) == []


@pytest.mark.asyncio
async def test_import_all_run_with_expired_lease_is_taken_over(factory):
    config = {
        IMPORT_ALL_STATUS_KEY: "in_progress",
        IMPORT_ALL_LEASE_OWNER_KEY: "worker-b",
        IMPORT_ALL_LEASE_UNTIL_KEY: (NOW - timedelta(minutes=1)).isoformat(),
    }
    await _add_run(factory, "stuck", config, NOW)

    with patch.object(imports, "worker_instance_id", return_value="worker-a"):
        async with factory() as db:
            runs = await imports._claim_import_all_runs(db, 1)

    assert [run.user_id for run in runs] == ["stuck"]
    assert runs[0].config[IMPORT_ALL_LEASE_OWNER_KEY].startswith("worker-a/")


@pytest.mark.asyncio
async def test_import_all_run_is_not_claimed_twice_by_one_process(factory):
    # Two import_all loops of one worker process share the worker id.
    await _add_run(factory, "running", {IMPORT_ALL_STATUS_KEY: "pending"}, NOW)

    with patch.object(imports, "worker_instance_id", return_value="one-process"):
        async with factory() as db:
            first = await imports._claim_import_all_runs(db, 1)
        async with factory() as db:
            second = await imports._claim_import_all_runs(db, 1)

    assert [run.user_id for run in first] == ["running"]
    assert second == []


@pytest.mark.asyncio
async def test_finished_step_releases_the_lease_for_the_next_step(factory):
    config = build_import_all_config({}, ["trakt", "simkl"], NOW)
    await _add_run(factory, "stepping", config, NOW)
    strategy = SimpleNamespace(import_for_integration=AsyncMock())
    registry = SimpleNamespace(get=lambda _provider: strategy)
    spec = replace(imports.IMPORT_ALL_SPEC, registry=registry)

    with patch.object(imports, "worker_instance_id", return_value="one-process"):
        async with factory() as db:
            db.add(Integration(id="trakt-int", user_id="stepping", provider="trakt"))
            await db.commit()
            [run] = await imports._claim_import_all_runs(db, 1)
            await imports._process_import_run(db, run, NOW, spec)
        async with factory() as db:
            [next_step] = await imports._claim_import_all_runs(db, 1)

    assert next_step.config[IMPORT_ALL_INDEX_KEY] == 1
