import asyncio
import hashlib
import json
import os
import secrets
import socket
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import replace
from datetime import datetime, timezone
from uuid import uuid4

import httpx
import httpx2
import pytest
import pytest_asyncio
import uvicorn
from librarysync.core import security
from librarysync.core.integration_tokens import save_integration_secret
from librarysync.db import session as db_session
from librarysync.db.models import (
    Base,
    EpisodeItem,
    Integration,
    MediaItem,
    OutboxJob,
    User,
    WatchedItem,
    WatchEvent,
    WatchlistItem,
    WatchlistSource,
    WatchlistSourceItem,
)
from mcp import ClientSession, types
from mcp.client.streamable_http import streamable_http_client
from sqlalchemy import select, text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine


@pytest_asyncio.fixture
async def mcp_db(tmp_path, monkeypatch, request):
    admin = None
    schema = "mcp_" + uuid4().hex
    if getattr(request, "param", None) == "postgres":
        database_url = os.getenv("WATCH_STATE_TEST_DATABASE_URL")
        if not database_url:
            pytest.skip("Isolated PostgreSQL test URL required")

        admin = create_async_engine(database_url)
        async with admin.begin() as connection:
            await connection.execute(text(f'CREATE SCHEMA "{schema}"'))
        isolated_url = make_url(database_url).update_query_dict({"options": f"-csearch_path={schema}"})
        engine = create_async_engine(isolated_url)
    else:
        engine = create_async_engine(
            f"sqlite+aiosqlite:///{tmp_path / 'mcp.sqlite'}", connect_args={"autocommit": False}
        )
    sessions = async_sessionmaker(engine, autoflush=False, expire_on_commit=False)
    monkeypatch.setattr(db_session, "SessionLocal", sessions)
    monkeypatch.setattr(db_session, "_ENGINE", engine)
    try:
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        yield sessions
    finally:
        await engine.dispose()
        if admin is not None:
            async with admin.begin() as connection:
                await connection.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
            await admin.dispose()


@pytest_asyncio.fixture
async def grants(mcp_db, monkeypatch):
    alice, bob, inactive, missing = (str(uuid4()) for _ in range(4))
    async with mcp_db.begin() as db:
        db.add_all(
            User(id=user_id, username=name, password_hash="synthetic", is_active=name != "inactive")
            for name, user_id in [("alice", alice), ("bob", bob), ("inactive", inactive)]
        )
    credentials = [(user_id, secrets.token_urlsafe(32)) for user_id in [alice, alice, bob, inactive, missing]]
    monkeypatch.setenv("LIBRARYSYNC_MCP_ENABLED", "true")
    monkeypatch.setenv("LIBRARYSYNC_MCP_ALLOWED_HOSTS", '["synthetic.test"]')
    monkeypatch.setenv(
        "LIBRARYSYNC_MCP_GRANTS",
        json.dumps(
            [
                {
                    "user_id": user_id,
                    "token_sha256": hashlib.sha256(token.encode()).hexdigest(),
                    "allow_watchlist_add": index in {0, 2},
                }
                for index, (user_id, token) in enumerate(credentials)
            ]
        ),
    )
    return credentials


_INIT = {
    "jsonrpc": "2.0",
    "id": 1,
    "method": "initialize",
    "params": {
        "protocolVersion": "2025-11-25",
        "capabilities": {},
        "clientInfo": {"name": "synthetic", "version": "1"},
    },
}
_HTTP_HEADERS = {"Accept": "application/json, text/event-stream"}


@pytest.mark.asyncio
async def test_private_opt_in_bearer_and_credential_owned_sessions(grants, mcp_db, monkeypatch):
    from librarysync.main import app as public_app
    from librarysync.mcp_app import create_app

    app = create_app()
    transport = httpx.ASGITransport(app=app)
    alice, token = grants[0]
    auth = ("Authorization", f"Bearer {token}")
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(transport=transport, base_url="http://synthetic.test", headers=_HTTP_HEADERS) as client,
    ):
        for path in ("/", "/docs", "/api/history", "/mcp/", "/%6dcp"):
            response = await client.post(path, headers=[auth], json=_INIT)
            assert response.status_code == 404 and response.headers["Cache-Control"] == "no-store"
        response = await client.options("/mcp", headers=[auth])
        assert response.status_code == 405 and response.headers["Cache-Control"] == "no-store"
        for headers, status in [
            ([], 401),
            ([auth, auth], 401),
            ([auth, ("Origin", "null")], 403),
            ([auth, ("Host", "elsewhere.test")], 403),
            ([auth, ("Host", "synthetic.test"), ("Host", "synthetic.test")], 403),
            ([("Cookie", f"access_token={token}")], 401),
            ([("Authorization", "Bearer invalid")], 401),
            ([("Authorization", f"Bearer {grants[3][1]}")], 401),
            ([("Authorization", f"Bearer {grants[4][1]}")], 401),
        ]:
            response = await client.post("/mcp", headers=headers, json=_INIT)
            assert response.status_code == status
            assert response.headers["Cache-Control"] == "no-store"

        response = await client.post("/mcp", headers=[auth], json=_INIT)
        assert response.status_code == 200
        assert response.headers["Content-Type"].startswith("text/event-stream")
        assert response.headers["Cache-Control"] == "no-store"
        session = ("Mcp-Session-Id", response.headers["Mcp-Session-Id"])
        for _, other_token in grants[1:3]:
            response = await client.delete("/mcp", headers=[session, ("Authorization", f"Bearer {other_token}")])
            assert response.status_code == 404
        async with mcp_db.begin() as db:
            user = await db.get(User, alice)
            user.is_active = False
        assert (await client.delete("/mcp", headers=[session, auth])).status_code == 401

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=public_app), base_url="http://public.test"
    ) as client:
        assert (await client.post("/mcp", headers=[auth], json=_INIT)).status_code == 404

    monkeypatch.delenv("LIBRARYSYNC_MCP_ENABLED")
    disabled = create_app()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=disabled), base_url="http://synthetic.test"
    ) as client:
        response = await client.post("/mcp", headers=[auth], json=_INIT)
        assert response.status_code == 404 and response.headers["Cache-Control"] == "no-store"


@pytest.mark.parametrize(
    "grants_config",
    [
        ("LIBRARYSYNC_MCP_ALLOWED_HOSTS", '["*.synthetic.test"]'),
        ("LIBRARYSYNC_MCP_GRANTS", "not-json-secret-value"),
        ("LIBRARYSYNC_MCP_GRANTS", '[{"user_id":"bad","token_sha256":"bad","allow_watchlist_add":"false"}]'),
        (
            "LIBRARYSYNC_MCP_GRANTS",
            '[{"user_id":"12345678-1234-5678-9abc-123456789abc","token_sha256":"'
            + "a" * 64
            + '","allow_watchlist_add":false,"allow_watchlist_add":true}]',
        ),
    ],
)
def test_bad_configuration_is_rejected_without_echoing_values(grants_config, monkeypatch):
    from librarysync.mcp_app import create_app

    variable, bad_value = grants_config
    monkeypatch.setenv("LIBRARYSYNC_MCP_ENABLED", "true")
    monkeypatch.setenv("LIBRARYSYNC_MCP_ALLOWED_HOSTS", '["synthetic.test"]')
    monkeypatch.setenv("LIBRARYSYNC_MCP_GRANTS", "[]")
    monkeypatch.setenv(variable, bad_value)
    with pytest.raises(ValueError, match=variable) as error:
        create_app()
    assert bad_value not in str(error.value)


@pytest_asyncio.fixture
async def mcp_url(grants):
    from librarysync.mcp_app import create_app

    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        host, port = listener.getsockname()
        server = uvicorn.Server(uvicorn.Config(create_app(), access_log=False, timeout_graceful_shutdown=2))
        running = asyncio.create_task(server.serve(sockets=[listener]))
        try:
            async with asyncio.timeout(5):
                while not server.started:
                    if running.done():
                        running.result()
                        pytest.fail("Synthetic MCP server did not start")
                    await asyncio.sleep(0.01)
            yield f"http://{host}:{port}/mcp"
        finally:
            server.should_exit = True
            await asyncio.wait_for(running, timeout=5)


@asynccontextmanager
async def mcp_client(mcp_url, token, **callbacks) -> AsyncIterator[ClientSession]:
    async with (
        httpx2.AsyncClient(headers={"Host": "synthetic.test", "Authorization": f"Bearer {token}"}, timeout=5) as http,
        streamable_http_client(mcp_url, http_client=http) as (reader, writer),
        ClientSession(reader, writer, **callbacks) as client,
    ):
        await client.initialize()
        yield client


@pytest.mark.asyncio
@pytest.mark.parametrize("mcp_db", ["sqlite", "postgres"], indirect=True)
async def test_history_paginates_ties_and_duplicates_with_credential_scoped_cursor(mcp_url, grants, mcp_db):
    alice, alice_token = grants[0]
    bob, bob_token = grants[2]
    movie = MediaItem(id=str(uuid4()), title="Synthetic Movie", media_type="movie", raw={"path": "/private/movie"})
    show = MediaItem(id=str(uuid4()), title="Synthetic Show", media_type="tv", imdb_id="tt0123456", year=2020)
    episode = EpisodeItem(id=str(uuid4()), show_media_item_id=show.id, season_number=2, episode_number=3)
    older, tie_a, tie_b = sorted(str(uuid4()) for _ in range(3))
    date = datetime(2026, 1, 2, 3, 4, 5, tzinfo=timezone.utc)
    async with mcp_db.begin() as db:
        db.add_all([movie, show])
        await db.flush()
        db.add(episode)
        await db.flush()
        db.add_all(
            [
                WatchedItem(id=tie_b, user_id=alice, media_item_id=movie.id, watched_at=date),
                WatchedItem(id=tie_a, user_id=alice, episode_item_id=episode.id, watched_at=date, rating=4.5),
                WatchedItem(id=older, user_id=alice, media_item_id=movie.id, watched_at=date.replace(day=1)),
                WatchedItem(user_id=bob, media_item_id=movie.id, watched_at=date.replace(day=3)),
            ]
        )

    async with mcp_client(mcp_url, alice_token) as client:
        tools = {tool.name: tool for tool in (await client.list_tools()).tools}
        assert tools["list_history"].annotations.read_only_hint is True
        assert tools["list_history"].input_schema["additionalProperties"] is False
        result = await client.call_tool("list_history", {"limit": 2})
        assert not result.is_error
        first = result.structured_content
        assert set(first) == {"items", "next_cursor"}
        assert [item["id"] for item in first["items"]] == [tie_b, tie_a]
        assert first["items"][1] == {
            "id": tie_a,
            "title": "Synthetic Show",
            "media_type": "tv",
            "year": 2020,
            "imdb_id": "tt0123456",
            "date": date.isoformat(),
            "rating": 4.5,
            "season": 2,
            "episode": 3,
        }
        assert "private/movie" not in str(first)
        cursor = first["next_cursor"]
        page = (await client.call_tool("list_history", {"limit": 2, "cursor": cursor})).structured_content
        assert [item["id"] for item in page["items"]] == [older]
        assert page["items"][0]["title"] == first["items"][0]["title"]
        assert page["next_cursor"] is None
        for arguments in ({"limit": 51}, {"limit": "2"}, {"limit": True}, {"other": "secret-value"}, {"cursor": "x"}):
            error = await client.call_tool("list_history", arguments)
            assert error.is_error and "secret-value" not in str(error)

    for token in [bob_token, grants[1][1]]:
        async with mcp_client(mcp_url, token) as client:
            assert (await client.call_tool("list_history", {"cursor": cursor})).is_error

    async with mcp_client(mcp_url, bob_token) as client:
        page = (await client.call_tool("list_history", {})).structured_content
        assert len(page["items"]) == 1
        assert page["items"][0]["id"] not in {tie_a, tie_b, older}


@pytest.mark.asyncio
@pytest.mark.parametrize("mcp_db", ["sqlite", "postgres"], indirect=True)
async def test_find_title_is_exact_bounded_and_reports_only_the_callers_status(mcp_url, grants, mcp_db):
    alice, alice_token = grants[0]
    bob, bob_token = grants[2]
    show = MediaItem(id=str(uuid4()), title="Synthetic Match", media_type="tv", imdb_id="tt0100000", year=2000)
    other = MediaItem(id=str(uuid4()), title="Synthetic Match Extra", media_type="movie")
    movies = [MediaItem(title=show.title, media_type="movie", year=2010 + index) for index in range(6)]
    episode = EpisodeItem(id=str(uuid4()), show_media_item_id=show.id, season_number=1, episode_number=1)
    async with mcp_db.begin() as db:
        db.add_all([show, other, *movies])
        await db.flush()
        db.add(episode)
        await db.flush()
        db.add_all(
            [
                WatchedItem(user_id=alice, episode_item_id=episode.id, watched_at=datetime.now(timezone.utc)),
                WatchlistItem(user_id=bob, media_item_id=show.id, type="tv", status="dropped", source="manual"),
            ]
        )

    async with mcp_client(mcp_url, alice_token) as client:
        annotations = {tool.name: tool.annotations for tool in (await client.list_tools()).tools}
        assert annotations.get("find_title") and annotations["find_title"].read_only_hint is True
        matches = (await client.call_tool("find_title", {"title": show.title})).structured_content
        assert matches["ambiguous"] is True and matches["truncated"] is True
        assert len(matches["matches"]) == 5
        assert {item["title"] for item in matches["matches"]} == {show.title}
        exact = (
            await client.call_tool("find_title", {"title": show.title, "year": 2000, "media_type": "tv"})
        ).structured_content
        assert exact == {
            "status": "found",
            "ambiguous": False,
            "truncated": False,
            "matches": [
                {
                    "id": show.id,
                    "title": show.title,
                    "media_type": "tv",
                    "year": 2000,
                    "imdb_id": "tt0100000",
                    "watched": True,
                    "watchlist_status": None,
                }
            ],
        }
        for title in ("Synthetic", "synthetic match", "Synthetic Alias"):
            miss = (await client.call_tool("find_title", {"title": title})).structured_content
            assert miss == {"status": "not_found", "matches": [], "ambiguous": False, "truncated": False}
        for args in (
            {},
            {"title": show.title, "imdb_id": show.imdb_id},
            {"imdb_id": "TT0100000"},
            {"imdb_id": show.imdb_id, "year": 2000},
            {"title": show.title, "year": "2000"},
        ):
            assert (await client.call_tool("find_title", args)).is_error

    async with mcp_client(mcp_url, bob_token) as client:
        result = (await client.call_tool("find_title", {"imdb_id": show.imdb_id})).structured_content
        assert result["matches"] == [{**exact["matches"][0], "watched": False, "watchlist_status": "dropped"}]


@pytest.mark.asyncio
@pytest.mark.parametrize("mcp_db", ["sqlite", "postgres"], indirect=True)
async def test_add_requires_its_own_true_form_and_only_creates_once(mcp_url, grants, mcp_db, monkeypatch):
    alice, alice_token = grants[0]
    title = "Synthetic\n\u202e Title " + "x" * 180
    movie = MediaItem(id=str(uuid4()), title=title, media_type="movie", year=2020, imdb_id="tt0123456")
    dropped = MediaItem(id=str(uuid4()), title="Dropped", media_type="tv")
    removed = MediaItem(id=str(uuid4()), title="Removed", media_type="tv")
    date = datetime(2026, 1, 2, 3, 4, 5, tzinfo=timezone.utc)
    watched = WatchedItem(id=str(uuid4()), user_id=alice, media_item_id=movie.id, watched_at=date, rating=4.5)
    monkeypatch.setattr(security, "settings", replace(security.settings, secret_key="k" * 48, secret_key_previous=()))
    async with mcp_db.begin() as db:
        db.add_all([movie, dropped, removed])
        await db.flush()
        db.add_all(
            [
                watched,
                WatchlistItem(user_id=alice, media_item_id=dropped.id, type="tv", status="dropped", source="trakt"),
                WatchlistItem(user_id=alice, media_item_id=removed.id, type="tv", status="removed", source="manual"),
                WatchlistSource(
                    user_id=alice, provider="simkl", source_type="personal", external_id="watchlist", is_enabled=False
                ),
            ]
        )
        for provider in ("trakt", "simkl"):
            integration = Integration(user_id=alice, provider=provider)
            db.add(integration)
            await db.flush()
            await save_integration_secret(
                db, integration.id, {"access_token": "synthetic", "refresh_token": "synthetic"}
            )

    calls = []
    responses = iter(
        [
            types.ElicitResult(action="accept", content={"approve": False}),
            types.ElicitResult(action="accept", content={"approve": "true"}),
            types.ElicitResult(action="accept", content={"approve": 1}),
            types.ElicitResult(action="accept", content={"approve": True, "other": False}),
            types.ElicitResult(action="accept"),
            types.ElicitResult(action="cancel"),
            types.ElicitResult(action="decline"),
            *(types.ElicitResult(action="accept", content={"approve": True}) for _ in range(4)),
        ]
    )

    async def elicit(_context, params):
        calls.append(params)
        return next(responses)

    arguments = {"media_id": movie.id}
    async with mcp_client(mcp_url, grants[1][1], elicitation_callback=elicit) as client:
        assert (await client.call_tool("add_to_watchlist", arguments)).is_error
    async with mcp_client(mcp_url, alice_token) as client:
        assert (await client.call_tool("add_to_watchlist", arguments)).is_error
    assert not calls

    async with mcp_client(mcp_url, alice_token, elicitation_callback=elicit) as client:
        tools = {tool.name: tool.annotations for tool in (await client.list_tools()).tools}
        assert set(tools) == {"list_history", "find_title", "add_to_watchlist"}
        annotations = tools["add_to_watchlist"]
        assert annotations is not None
        assert annotations.read_only_hint is False and annotations.idempotent_hint is True
        assert annotations.open_world_hint is True and annotations.destructive_hint is False
        for invalid in (
            {"media_id": movie.id.upper()},
            {"media_id": [movie.id]},
            {"title": title},
            {**arguments, "approve": True},
            {"media_id": str(uuid4())},
        ):
            assert (await client.call_tool("add_to_watchlist", invalid)).is_error
        assert not calls
        for _ in range(7):
            assert (await client.call_tool("add_to_watchlist", arguments)).is_error
        async with mcp_db() as db:
            assert len((await db.scalars(select(WatchlistItem))).all()) == 2
            assert not (await db.scalars(select(OutboxJob))).all()

        created = (await client.call_tool("add_to_watchlist", arguments)).structured_content
        assert set(created) == {"status", "id"} and created["status"] == "created"
        for target in (movie, dropped, removed):
            duplicate = await client.call_tool("add_to_watchlist", {"media_id": target.id})
            assert not duplicate.is_error and duplicate.structured_content["status"] == "already_exists"
            if target is movie:
                assert duplicate.structured_content["id"] == created["id"]

    assert len(calls) == 11
    form = calls[0]
    assert form.mode == "form"
    assert all(provider in form.message for provider in ("Trakt", "SIMKL", "PublicMetaDB", "Letterboxd"))
    assert "Synthetic" not in form.message and movie.id not in form.message
    assert form.requested_schema["required"] == ["approve"]
    assert set(form.requested_schema["properties"]) == {"approve"}
    prop = form.requested_schema["properties"]["approve"]
    assert prop["type"] == "boolean"
    description = prop["description"]
    assert all(value in description for value in ("Synthetic", "2020", "movie", movie.id))
    assert "\n" not in description and "\u202e" not in description and len(description) <= 220

    async with mcp_db() as db:
        rows = {item.media_item_id: item for item in (await db.scalars(select(WatchlistItem))).all()}
        assert {media_id: item.status for media_id, item in rows.items()} == {
            movie.id: "watched",
            dropped.id: "dropped",
            removed.id: "removed",
        }
        assert rows[movie.id].source == "manual" and rows[dropped.id].source == "trakt"
        assert [(item.id, item.rating) for item in (await db.scalars(select(WatchedItem))).all()] == [(watched.id, 4.5)]
        assert [(item.media_item_id, item.event_type) for item in (await db.scalars(select(WatchEvent))).all()] == [
            (movie.id, "watchlist_added")
        ]
        sources = {row.provider: row for row in (await db.scalars(select(WatchlistSource))).all()}
        links = (await db.scalars(select(WatchlistSourceItem))).all()
        assert [(link.source_id, link.watchlist_item_id) for link in links] == [(sources["manual"].id, created["id"])]
        jobs = (await db.scalars(select(OutboxJob))).all()
        assert [(job.user_id, job.target_provider, job.job_type, job.status) for job in jobs] == [
            (alice, "trakt", "push_watchlist", "pending")
        ]
        assert jobs[0].payload["watchlist_item_id"] == created["id"] and "unhide_dropped" not in jobs[0].payload


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("field", "updated"),
    [
        ("title", "After Approval"),
        ("imdb_id", "tt0999999"),
        ("tmdb_id", "7654321"),
        ("tvdb_id", "3456789"),
        ("raw", {"simkl_id": "8888888"}),
        ("raw", {"letterboxd_film_id": "synthetic-different"}),
    ],
)
async def test_pending_approval_does_not_hold_a_db_session_and_rechecks_target(mcp_url, grants, mcp_db, field, updated):
    movie = MediaItem(id=str(uuid4()), title="Before Approval", media_type="movie")
    async with mcp_db.begin() as db:
        db.add(movie)

    checkouts = []

    async def approve_stale(_ctx, _params):
        checkouts.append(db_session.get_engine().pool.checkedout())
        async with mcp_db.begin() as db:
            item = await db.get(MediaItem, movie.id)
            setattr(item, field, updated)
        return types.ElicitResult(action="accept", content={"approve": True})

    async with mcp_client(mcp_url, grants[0][1], elicitation_callback=approve_stale) as client:
        result = await client.call_tool("add_to_watchlist", {"media_id": movie.id})
        assert result.is_error and "title changed" in result.content[0].text
    assert checkouts == [0]
    async with mcp_db() as db:
        assert not (await db.scalars(select(WatchlistItem))).all()


@pytest.mark.asyncio
@pytest.mark.parametrize("mcp_db", ["postgres"], indirect=True)
async def test_rest_and_mcp_share_first_manual_watchlist_source(mcp_url, grants, mcp_db, monkeypatch):
    from librarysync.api.routes_watchlist import WatchlistItemCreateIn, add_watchlist_item

    alice, token = grants[0]
    mcp_movie = MediaItem(id=str(uuid4()), title="MCP first", media_type="movie", imdb_id="tt1111111")
    rest_movie = MediaItem(id=str(uuid4()), title="REST first", media_type="movie", imdb_id="tt2222222")
    async with mcp_db.begin() as db:
        db.add_all([mcp_movie, rest_movie])

    # Force both transactions to see no manual source before either can insert it.
    execute = AsyncSession.execute
    both_read = asyncio.Event()
    reads = 0

    async def execute_in_lockstep(self, statement, *args, **kwargs):
        nonlocal reads
        result = await execute(self, statement, *args, **kwargs)
        columns = getattr(statement, "column_descriptions", [])
        if columns and columns[0].get("expr") is WatchlistSource and reads < 2:
            reads += 1
            if reads == 2:
                both_read.set()
            await asyncio.wait_for(both_read.wait(), timeout=5)
        return result

    monkeypatch.setattr(AsyncSession, "execute", execute_in_lockstep)

    async def approve(_ctx, _params):
        return types.ElicitResult(action="accept", content={"approve": True})

    async def rest_add():
        async with mcp_db() as db:
            return await add_watchlist_item(
                WatchlistItemCreateIn(media_type="movie", title=rest_movie.title, imdb_id=rest_movie.imdb_id),
                current_user=await db.get(User, alice),
                db=db,
            )

    async with mcp_client(mcp_url, token, elicitation_callback=approve) as client:
        mcp, rest = await asyncio.gather(client.call_tool("add_to_watchlist", {"media_id": mcp_movie.id}), rest_add())

    assert reads == 2
    assert mcp.structured_content["status"] == rest["status"] == "created"
    async with mcp_db() as db:
        source = (await db.scalars(select(WatchlistSource))).one()
        links = (await db.scalars(select(WatchlistSourceItem))).all()
        assert {(link.source_id, link.watchlist_item_id) for link in links} == {
            (source.id, mcp.structured_content["id"]),
            (source.id, rest["id"]),
        }


@pytest.mark.asyncio
async def test_timed_out_answer_does_not_authorize_the_next_call(mcp_url, grants, mcp_db, monkeypatch):
    from librarysync import mcp_app

    monkeypatch.setattr(mcp_app, "_APPROVAL_TIMEOUT_SECONDS", 0.2)
    first = MediaItem(id=str(uuid4()), title="First", media_type="movie")
    second = MediaItem(id=str(uuid4()), title="Second", media_type="movie")
    async with mcp_db.begin() as db:
        db.add_all([first, second])
    late = asyncio.Event()
    seen = []

    async def answer(_ctx, params):
        description = params.requested_schema["properties"]["approve"]["description"]
        seen.append(description)
        if first.id in description:
            await late.wait()
            return types.ElicitResult(action="accept", content={"approve": True})
        return types.ElicitResult(action="decline")

    async with mcp_client(mcp_url, grants[0][1], elicitation_callback=answer) as client:
        result = await client.call_tool("add_to_watchlist", {"media_id": first.id})
        assert result.is_error and "not approved" in result.content[0].text
        late.set()
        result = await client.call_tool("add_to_watchlist", {"media_id": second.id})
        assert result.is_error and "not approved" in result.content[0].text
    assert len(seen) == 2 and first.id in seen[0] and second.id in seen[1]
    async with mcp_db() as db:
        assert not (await db.scalars(select(WatchlistItem))).all()


@pytest.mark.asyncio
async def test_disconnected_call_cannot_use_a_late_form_reply(mcp_url, grants, mcp_db):
    movie = MediaItem(id=str(uuid4()), title="Disconnected Title", media_type="movie")
    async with mcp_db.begin() as db:
        db.add(movie)

    headers = {**_HTTP_HEADERS, "Host": "synthetic.test", "Authorization": f"Bearer {grants[0][1]}"}
    async with httpx.AsyncClient(headers=headers) as http:
        params = {**_INIT["params"], "capabilities": {"elicitation": {"form": {}}}}
        initialized = await http.post(mcp_url, json={**_INIT, "params": params})
        assert initialized.status_code == 200
        http.headers["mcp-session-id"] = initialized.headers["mcp-session-id"]
        http.headers["mcp-protocol-version"] = "2025-11-25"
        ready = await http.post(mcp_url, json={"jsonrpc": "2.0", "method": "notifications/initialized"})
        assert ready.status_code == 202
        call = {
            "jsonrpc": "2.0",
            "id": 2,
            "method": "tools/call",
            "params": {"name": "add_to_watchlist", "arguments": {"media_id": movie.id}},
        }
        prompt = None
        async with http.stream("POST", mcp_url, json=call) as stream:
            assert stream.status_code == 200
            async for line in stream.aiter_lines():
                if line.startswith("data: "):
                    prompt = json.loads(line[6:])
                    break
            assert prompt is not None and prompt["method"] == "elicitation/create"
        late = await http.post(
            mcp_url,
            json={"jsonrpc": "2.0", "id": prompt["id"], "result": {"action": "accept", "content": {"approve": True}}},
        )
        assert late.status_code == 202
        # This test's disposable database has no concurrent writers; let the accepted POST dispatch.
        await asyncio.sleep(0.2)
        async with mcp_db() as db:
            assert not (await db.scalars(select(WatchlistItem))).all()
        assert (await http.delete(mcp_url)).status_code == 200


@pytest.mark.parametrize("mcp_db", ["postgres"], indirect=True)
@pytest.mark.asyncio
async def test_postgres_outbox_failure_rolls_back_the_entire_watchlist_add(
    mcp_url, grants, mcp_db, monkeypatch, caplog
):
    alice, token = grants[0]
    movie = MediaItem(id=str(uuid4()), title="Synthetic rollback title", media_type="movie", imdb_id="tt0100000")
    monkeypatch.setattr(security, "settings", replace(security.settings, secret_key="k" * 48, secret_key_previous=()))
    async with mcp_db.begin() as db:
        db.add(movie)
        integration = Integration(user_id=alice, provider="trakt")
        db.add(integration)
        await db.flush()
        await save_integration_secret(db, integration.id, {"access_token": "synthetic", "refresh_token": "synthetic"})
        await db.execute(text("ALTER TABLE outbox ADD CHECK (job_type != 'push_watchlist')"))

    async def approve(_ctx, _params):
        return types.ElicitResult(action="accept", content={"approve": True})

    async with mcp_client(mcp_url, token, elicitation_callback=approve) as client:
        result = await client.call_tool("add_to_watchlist", {"media_id": movie.id})
        assert result.is_error and result.content == [types.TextContent(type="text", text="Tool operation failed")]
    assert "LibrarySync MCP tool failed" in caplog.text
    assert movie.id not in caplog.text and "push_watchlist" not in caplog.text and token not in caplog.text
    async with mcp_db() as db:
        for model in (WatchlistItem, WatchlistSource, WatchlistSourceItem, WatchEvent, OutboxJob):
            assert not (await db.scalars(select(model))).all()


@pytest.mark.parametrize("mcp_db", ["postgres"], indirect=True)
@pytest.mark.asyncio
async def test_postgres_parallel_adds_serialize_only_within_each_account(mcp_url, grants, mcp_db):
    alice, alice_token = grants[0]
    bob, bob_token = grants[2]
    first = MediaItem(id=str(uuid4()), title="First", media_type="movie")
    second = MediaItem(id=str(uuid4()), title="Second", media_type="anime")
    async with mcp_db.begin() as db:
        db.add_all([first, second])

    alice_prompted = asyncio.Event()
    alice_count = 0

    async def confirm_alice(_ctx, _params):
        nonlocal alice_count
        alice_count += 1
        if alice_count == 3:
            alice_prompted.set()
        return types.ElicitResult(action="accept", content={"approve": True})

    async def confirm_bob(_ctx, _params):
        return types.ElicitResult(action="accept", content={"approve": True})

    async def add(client, media_id):
        result = await client.call_tool("add_to_watchlist", {"media_id": media_id})
        assert not result.is_error
        return result.structured_content

    async with (
        mcp_client(mcp_url, alice_token, elicitation_callback=confirm_alice) as alice_client,
        mcp_client(mcp_url, bob_token, elicitation_callback=confirm_bob) as bob_client,
        asyncio.TaskGroup() as tasks,
    ):
        async with mcp_db.begin() as db:
            await db.execute(select(User.id).where(User.id == alice).with_for_update(key_share=True))
            alice_adds = [tasks.create_task(add(alice_client, target)) for target in (first.id, first.id, second.id)]
            await asyncio.wait_for(alice_prompted.wait(), timeout=4)
            assert (await add(bob_client, first.id))["status"] == "created"
            assert not any(task.done() for task in alice_adds)

    results = [task.result() for task in alice_adds]
    assert sorted(result["status"] for result in results) == ["already_exists", "created", "created"]
    assert results[0]["id"] == results[1]["id"] != results[2]["id"]
    async with mcp_db() as db:
        sources = (await db.scalars(select(WatchlistSource))).all()
        assert {(source.user_id, source.provider) for source in sources} == {(alice, "manual"), (bob, "manual")}
        assert len(sources) == 2
        assert len((await db.scalars(select(WatchlistSourceItem))).all()) == 3
        assert not (await db.scalars(select(OutboxJob))).all()
