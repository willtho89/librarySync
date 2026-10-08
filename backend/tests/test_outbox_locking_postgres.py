"""Row-lock behaviour of outbox delivery that SQLite cannot show."""

import os
import uuid
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio
from librarysync.connectors.services.trakt import TraktToken
from librarysync.core import integration_tokens, security
from librarysync.core.watch_pipeline import enqueue_outbox_job
from librarysync.db.models import Base, Integration, OutboxJob, User, WatchStateEntry
from librarysync.jobs import process_outbox
from sqlalchemy import select, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

DATABASE = os.environ.get("WATCH_STATE_TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(not DATABASE, reason="Isolated PostgreSQL test URL required")
PAST = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()


@pytest_asyncio.fixture
async def factory(monkeypatch):
    monkeypatch.setattr(security, "settings", replace(security.settings, secret_key="k" * 48, secret_key_previous=()))
    schema = "outbox_locking_" + uuid.uuid4().hex
    admin = create_async_engine(DATABASE)
    async with admin.begin() as connection:
        await connection.execute(text(f'CREATE SCHEMA "{schema}"'))
    engine = create_async_engine(DATABASE + "?options=-csearch_path%3D" + schema)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    session_factory = async_sessionmaker(engine, autoflush=False, expire_on_commit=False)
    async with session_factory() as db:
        db.add(User(id="u", username="u", password_hash="unused"))
        await db.commit()
        db.add(Integration(id="int", user_id="u", provider="trakt"))
        await db.commit()
        await integration_tokens.save_integration_secret(
            db, "int", {"access_token": "old-access", "refresh_token": "old-refresh", "expires_at": PAST}
        )
        await db.commit()
    yield session_factory
    await engine.dispose()
    async with admin.begin() as connection:
        await connection.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
    await admin.dispose()


@pytest.mark.asyncio
async def test_rating_delivery_keeps_the_state_lock_through_a_token_refresh(factory, monkeypatch):
    now = datetime.now(timezone.utc)
    async with factory() as db:
        db.add(
            WatchStateEntry(
                id="rating-entry",
                user_id="u",
                category="rating",
                target_key="movie-key",
                media_type="movie",
                scope="movie",
                meta_id="tt1",
                occurred_at=now,
                payload={"event": "rated", "rating": 6},
            )
        )
        await db.commit()
        job = await enqueue_outbox_job(
            db,
            user_id="u",
            target_provider="trakt",
            job_type="push_rating",
            payload={
                "state_entry_id": "rating-entry",
                "protocol_at": int(now.timestamp()),
                "protocol_event": "rated",
                "protocol_rating": 6,
                "rating": 3,
                "media_type": "movie",
                "movie_ids": {"imdb": "tt1"},
            },
        )
        await db.commit()
        job_id = job.id

    lock_free_during_provider_write: list[bool] = []

    async def _add_ratings(*_args):
        async with factory() as other:
            try:
                await other.execute(
                    select(WatchStateEntry).where(WatchStateEntry.id == "rating-entry").with_for_update(nowait=True)
                )
                lock_free_during_provider_write.append(True)
            except DBAPIError:
                lock_free_during_provider_write.append(False)
        return {}, 200

    token = TraktToken(
        access_token="new-access",
        refresh_token="new-refresh",
        expires_at=datetime.now(timezone.utc) + timedelta(days=30),
        scope=None,
        token_type="bearer",
    )
    client = SimpleNamespace(refresh_access_token=AsyncMock(return_value=token), add_ratings=_add_ratings)
    monkeypatch.setattr(process_outbox, "TraktClient", lambda **_kwargs: client)
    monkeypatch.setattr(
        process_outbox,
        "settings",
        replace(process_outbox.settings, trakt_client_id="test-id", trakt_client_secret="test-secret"),
    )

    async with factory() as db:
        await process_outbox.OUTBOX_DISPATCHER.deliver(db, await db.get(OutboxJob, job_id))
        await db.rollback()

    client.refresh_access_token.assert_awaited_once()
    assert lock_free_during_provider_write == [False]


@pytest.mark.asyncio
async def test_token_refresh_leaves_the_callers_transaction_open(factory):
    async with factory() as db:
        user = await db.scalar(select(User).where(User.id == "u").with_for_update())
        user.username = "uncommitted-change"
        await integration_tokens.ensure_fresh_secret(
            db,
            "int",
            {"expires_at": PAST},
            is_fresh=lambda data: data.get("access_token") == "new-access",
            refresh=AsyncMock(return_value={"access_token": "new-access", "refresh_token": "new-refresh"}),
        )
        await db.rollback()
    async with factory() as db:
        assert (await db.get(User, "u")).username == "u"


async def _seed_watch_pushes(factory, watched_ids: list[str]) -> None:
    from librarysync.db.models import MediaItem, WatchedItem

    async with factory() as db:
        db.add(MediaItem(id="movie", media_type="movie", title="Movie"))
        await db.commit()
        for watched_id in watched_ids:
            db.add(
                WatchedItem(id=watched_id, user_id="u", media_item_id="movie", watched_at=datetime.now(timezone.utc))
            )
        await db.commit()
        for watched_id in watched_ids:
            await enqueue_outbox_job(
                db,
                user_id="u",
                target_provider="trakt",
                job_type="push_watched",
                payload={"watched_item_id": watched_id},
            )
        await db.commit()


async def _try_delete_watch(factory, watched_id: str) -> bool:
    """Delete the watch from another session; False when it has to wait for a lock."""
    from librarysync.db.models import WatchedItem
    from sqlalchemy import delete

    async with factory() as other:
        try:
            await other.execute(text("SET LOCAL lock_timeout = '300ms'"))
            await other.execute(delete(WatchedItem).where(WatchedItem.id == watched_id))
            await other.commit()
            return True
        except DBAPIError:
            await other.rollback()
            return False


@pytest.mark.asyncio
@pytest.mark.parametrize("batched", [False, True])
async def test_deletion_waits_for_an_in_flight_push(factory, monkeypatch, batched):
    watched_ids = ["w1", "w2"] if batched else ["w1"]
    await _seed_watch_pushes(factory, watched_ids)
    deletion_during_delivery: list[bool] = []

    async def _rate_limit(*_args, **_kwargs):
        # The user deletes the watch while the worker is about to deliver it.
        deletion_during_delivery.append(await _try_delete_watch(factory, "w1"))
        return None

    monkeypatch.setattr(process_outbox, "load_blocked_outbox_users", AsyncMock(return_value=set()))
    monkeypatch.setattr(process_outbox.RATE_LIMITER, "try_acquire", _rate_limit)
    deliver = AsyncMock(return_value=SimpleNamespace(response_code=201, external_id=None, resolved_rewatch=None))
    deliver_batch = AsyncMock(return_value=201)
    monkeypatch.setattr(process_outbox.OUTBOX_DISPATCHER, "deliver", deliver)
    monkeypatch.setattr(process_outbox, "_deliver_batch", deliver_batch)

    async with factory() as worker_db:
        claimed = await process_outbox._claim_jobs(worker_db, 50)
        if batched:
            await process_outbox._process_job_batch(worker_db, claimed)
        else:
            await process_outbox._process_job(worker_db, claimed[0])

    # The deletion could not slip in between the existence check and the provider write.
    assert deletion_during_delivery == [False]
    assert (deliver_batch if batched else deliver).await_count == 1
    # Once the push is committed the deletion goes through (and would queue the removal after it).
    assert await _try_delete_watch(factory, "w1") is True
