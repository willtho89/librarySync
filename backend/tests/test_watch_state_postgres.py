"""Production-database checks. Use an isolated test database, never the application DB."""

import asyncio
import os
import uuid

import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI
from librarysync.api.deps import get_db
from librarysync.api.routes_addon_watch_state import router
from librarysync.db.models import Base, StremioAddonConfig, User, WatchedItem, WatchStateReceipt
from librarysync.jobs.watch_state import drain_watch_state
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from test_addon_watch_state import AT, movie_event

DATABASE = os.environ.get("WATCH_STATE_TEST_DATABASE_URL")
pytestmark = [pytest.mark.asyncio, pytest.mark.skipif(not DATABASE, reason="Isolated PostgreSQL test URL required")]


@pytest_asyncio.fixture
async def pg_context():
    schema = f"watch_state_test_{uuid.uuid4().hex}"
    admin = create_async_engine(DATABASE)
    async with admin.begin() as connection:
        await connection.execute(text(f'CREATE SCHEMA "{schema}"'))
    engine = create_async_engine(DATABASE, connect_args={"options": f"-csearch_path={schema}"})
    try:
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        factory = async_sessionmaker(engine, expire_on_commit=False)
        async with factory() as db:
            db.add(User(id="u", username="u", password_hash="unused"))
            await db.flush()
            db.add(StremioAddonConfig(id="addon", user_id="u", watch_state_enabled=True))
            await db.commit()

        async def session():
            async with factory() as db:
                yield db

        app = FastAPI()
        app.include_router(router)
        app.dependency_overrides[get_db] = session
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            yield client, factory
    finally:
        await engine.dispose()
        async with admin.begin() as connection:
            await connection.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
        await admin.dispose()


async def test_simultaneous_retry_creates_one_watch(pg_context):
    client, factory = pg_context
    url = "/stremio-addon/addon/watch_state/push/movie/tt0111161.json"
    results = await asyncio.gather(*(client.post(url, json=movie_event()) for _ in range(4)))
    assert [response.status_code for response in results] == [204] * 4
    async with factory() as db:
        assert await db.scalar(select(func.count()).select_from(WatchedItem)) == 1
        receipt = await db.scalar(select(WatchStateReceipt))
        assert receipt.duplicates == 3


async def test_simultaneous_initial_pulls_build_one_consistent_snapshot(pg_context):
    client, _ = pg_context
    results = await asyncio.gather(*(client.get("/stremio-addon/addon/watch_state/pull.json") for _ in range(4)))
    assert [response.status_code for response in results] == [200] * 4
    assert len({response.json()["version"] for response in results}) == 1


async def test_bulk_workers_claim_each_receipt_once(pg_context):
    client, factory = pg_context
    url = "/stremio-addon/addon/watch_state/push/series/tt0903747.json"
    for part in range(1, 3):
        body = {
            "id": f"bulk-{part}",
            "event": "played",
            "scope": "series",
            "at": AT,
            "metaId": "tt0903747",
            "part": part,
            "parts": 2,
            "videos": [{"videoId": f"tt0903747:1:{part}", "season": 1, "episode": part}],
        }
        assert (await client.post(url, json=body)).status_code == 204

    async def drain():
        async with factory() as db:
            return await drain_watch_state(db, limit=1)

    assert sum(await asyncio.gather(drain(), drain())) == 2
    async with factory() as db:
        assert await db.scalar(select(func.count()).select_from(WatchedItem)) == 2
        assert list((await db.scalars(select(WatchStateReceipt.status))).all()) == ["applied", "applied"]
