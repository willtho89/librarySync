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
