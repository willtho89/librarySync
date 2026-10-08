from dataclasses import replace

import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI
from librarysync import config
from librarysync.api import deps, routes_auth
from librarysync.core import auth
from librarysync.core.login_throttle import MAX_FAILURES_PER_USERNAME, LoginThrottle
from librarysync.db.models import Base
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine


@pytest.fixture(autouse=True)
def auth_settings(monkeypatch):
    updated = replace(config.settings, secret_key="k" * 48, allow_registration=True, max_users=-1)
    monkeypatch.setattr(auth, "settings", updated)
    monkeypatch.setattr(routes_auth, "settings", updated)
    monkeypatch.setattr(routes_auth, "LOGIN_THROTTLE", LoginThrottle())


@pytest_asyncio.fixture
async def client():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)

    async def _db():
        async with factory() as session:
            yield session

    app = FastAPI()
    app.include_router(routes_auth.router)
    app.dependency_overrides[deps.get_db] = _db
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as http:
        yield http
    await engine.dispose()


async def _register(client, username="alice", password="correct horse"):
    return await client.post("/api/auth/register", json={"username": username, "password": password})


@pytest.mark.asyncio
async def test_register_then_login(client):
    assert (await _register(client)).status_code == 200
    response = await client.post("/api/auth/login", json={"username": "Alice", "password": "correct horse"})
    assert response.status_code == 200
    assert auth.decode_access_token(response.json()["access_token"])["sub"]


@pytest.mark.asyncio
async def test_duplicate_username_is_conflict(client):
    await _register(client)
    assert (await _register(client, username=" ALICE ")).status_code == 409


@pytest.mark.asyncio
@pytest.mark.parametrize("username", ["", "   ", "x" * 65])
async def test_register_rejects_invalid_usernames(client, username):
    assert (await _register(client, username=username)).status_code == 422


@pytest.mark.asyncio
async def test_register_rejects_passwords_bcrypt_would_truncate(client):
    response = await _register(client, password="p" * 73)
    assert response.status_code == 400
    assert "72 bytes" in response.json()["detail"]


@pytest.mark.asyncio
async def test_unknown_user_and_wrong_password_look_the_same(client):
    await _register(client)
    unknown = await client.post("/api/auth/login", json={"username": "nobody", "password": "whatever1"})
    wrong = await client.post("/api/auth/login", json={"username": "alice", "password": "whatever1"})
    assert unknown.status_code == wrong.status_code == 401
    assert unknown.json() == wrong.json()


@pytest.mark.asyncio
async def test_repeated_failures_are_throttled(client):
    await _register(client)
    for _ in range(MAX_FAILURES_PER_USERNAME):
        response = await client.post("/api/auth/login", json={"username": "alice", "password": "wrong-one"})
        assert response.status_code == 401
    throttled = await client.post("/api/auth/login", json={"username": "alice", "password": "correct horse"})
    assert throttled.status_code == 429
    assert int(throttled.headers["Retry-After"]) > 0


def test_throttle_window_expires():
    throttle = LoginThrottle()
    for _ in range(MAX_FAILURES_PER_USERNAME):
        throttle.record_failure("bob", "10.0.0.1", now=0.0)
    assert throttle.retry_after("bob", "10.0.0.1", now=1.0) is not None
    assert throttle.retry_after("bob", "10.0.0.1", now=15 * 60 + 1.0) is None
