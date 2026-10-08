import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import librarysync.core.security
import pytest
import pytest_asyncio
from cryptography.fernet import Fernet
from librarysync.connectors.services.trakt import TraktError, TraktToken
from librarysync.core import integration_tokens
from librarysync.db.models import Base, Integration, IntegrationSecret, User
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

PAST = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
FUTURE = (datetime.now(timezone.utc) + timedelta(days=30)).isoformat()


@pytest.fixture(autouse=True)
def stub_fernet(monkeypatch):
    fernet = Fernet(Fernet.generate_key())
    monkeypatch.setattr(librarysync.core.security, "_get_fernet", lambda: fernet)


@pytest_asyncio.fixture
async def factory():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    session_factory = async_sessionmaker(engine, autoflush=False, expire_on_commit=False)
    async with session_factory() as db:
        db.add(User(id="u", username="u", password_hash="x"))
        db.add(Integration(id="int", user_id="u", provider="trakt"))
        await db.commit()
    yield session_factory
    await engine.dispose()


async def _store(factory, data: dict) -> None:
    async with factory() as db:
        await integration_tokens.save_integration_secret(db, "int", data)
        await db.commit()


async def _stored(factory) -> dict:
    async with factory() as db:
        secret = (await db.execute(select(IntegrationSecret))).scalars().one()
        return json.loads(librarysync.core.security.decrypt_value(secret.secret_data))


def _client(access_token: str = "new-access", refresh_token: str = "new-refresh"):
    token = TraktToken(
        access_token=access_token,
        refresh_token=refresh_token,
        expires_at=datetime.now(timezone.utc) + timedelta(days=90),
        scope=None,
        token_type="bearer",
    )
    return SimpleNamespace(refresh_access_token=AsyncMock(return_value=token))


@pytest.mark.asyncio
async def test_unexpired_token_is_returned_without_refresh(factory):
    stale_view = {"access_token": "live", "refresh_token": "r1", "expires_at": FUTURE}
    client = _client()
    async with factory() as db:
        token = await integration_tokens.ensure_trakt_access_token(db, "int", stale_view, client)
    assert token == "live"
    client.refresh_access_token.assert_not_awaited()


@pytest.mark.asyncio
async def test_expired_token_is_refreshed_and_persisted(factory):
    data = {"access_token": "old", "refresh_token": "r1", "expires_at": PAST}
    await _store(factory, data)
    client = _client()

    async with factory() as db:
        token = await integration_tokens.ensure_trakt_access_token(db, "int", data, client)

    assert token == "new-access"
    client.refresh_access_token.assert_awaited_once_with("r1")
    stored = await _stored(factory)
    assert stored["access_token"] == "new-access"
    assert stored["refresh_token"] == "new-refresh"


@pytest.mark.asyncio
async def test_worker_with_stale_view_reuses_token_refreshed_by_another(factory):
    # Another worker already refreshed: the stored secret is fresh, our in-memory copy is not.
    await _store(factory, {"access_token": "theirs", "refresh_token": "r2", "expires_at": FUTURE})
    stale_view = {"access_token": "old", "refresh_token": "r1", "expires_at": PAST}
    client = _client()

    async with factory() as db:
        token = await integration_tokens.ensure_trakt_access_token(db, "int", stale_view, client)

    assert token == "theirs"
    client.refresh_access_token.assert_not_awaited()


@pytest.mark.asyncio
async def test_refresh_failure_propagates_and_keeps_stored_secret(factory):
    data = {"access_token": "old", "refresh_token": "r1", "expires_at": PAST}
    await _store(factory, data)
    client = SimpleNamespace(refresh_access_token=AsyncMock(side_effect=TraktError("revoked", status_code=401)))

    async with factory() as db:
        with pytest.raises(TraktError):
            await integration_tokens.ensure_trakt_access_token(db, "int", data, client)

    assert (await _stored(factory))["refresh_token"] == "r1"


@pytest.mark.asyncio
async def test_expire_access_token_forces_next_refresh(factory):
    await _store(factory, {"access_token": "live", "refresh_token": "r1", "expires_at": FUTURE})

    async with factory() as db:
        assert await integration_tokens.expire_access_token(db, "u", "trakt") is True
        await db.commit()

    stored = await _stored(factory)
    assert stored["expires_at"].startswith("1970-01-01")
    client = _client()
    async with factory() as db:
        token = await integration_tokens.ensure_trakt_access_token(db, "int", stored, client)
    assert token == "new-access"


@pytest_asyncio.fixture
async def file_factory(tmp_path):
    # A file database gives the refresh session its own connection, like PostgreSQL.
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'tokens.db'}")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    session_factory = async_sessionmaker(engine, autoflush=False, expire_on_commit=False)
    async with session_factory() as db:
        db.add(User(id="u", username="original", password_hash="x"))
        db.add(Integration(id="int", user_id="u", provider="trakt"))
        await db.commit()
    yield session_factory
    await engine.dispose()


@pytest.mark.asyncio
async def test_refresh_does_not_commit_or_end_the_callers_transaction(file_factory):
    data = {"access_token": "old", "refresh_token": "r1", "expires_at": PAST}
    await _store(file_factory, data)

    async with file_factory() as db:
        user = await db.get(User, "u")
        user.username = "uncommitted-change"
        token = await integration_tokens.ensure_trakt_access_token(db, "int", data, _client())
        assert db.in_transaction()
        await db.rollback()

    assert token == "new-access"
    async with file_factory() as db:
        assert (await db.get(User, "u")).username == "original"
    assert (await _stored(file_factory))["refresh_token"] == "new-refresh"
