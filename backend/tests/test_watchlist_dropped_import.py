"""Tests for ingesting provider-dropped shows into the local watchlist.

Covers:
- Trakt: GET /users/hidden/dropped fetch and the dropped import pass.
- SIMKL: dropped entries extracted from /sync/all-items in the dropped pass.
- process_dropped_candidates: marks items dropped, links the dropped source.
- reconcile_dropped_source: un-drops survivors, deletes orphans.
- Watchlist imports no longer resurrect dropped items (no flip-flop).
"""

import asyncio
from datetime import date, datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx
import pytest
from librarysync.connectors.services.simkl import SimklError
from librarysync.connectors.services.trakt import TraktClient, TraktError
from librarysync.db.models import (
    Base,
    MediaItem,
    User,
    WatchlistItem,
    WatchlistSource,
    WatchlistSourceItem,
)
from librarysync.jobs import simkl_import, trakt_import
from librarysync.jobs.watchlist_pipeline import (
    WatchlistCandidate,
    process_dropped_candidates,
    process_watchlist_candidates,
)
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

NOW = datetime(2026, 7, 18, 10, 0, tzinfo=timezone.utc)


def _trakt_hidden_entry(trakt_id: int = 203632, tmdb_id: int = 1399) -> dict:
    return {
        "hidden_at": "2026-07-01T10:00:00.000Z",
        "type": "show",
        "show": {
            "title": "Game of Thrones",
            "year": 2011,
            "ids": {"trakt": trakt_id, "tvdb": 121361, "imdb": "tt0944947", "tmdb": tmdb_id},
        },
    }


def test_trakt_get_hidden_items_fetches_dropped_section() -> None:
    client = TraktClient(client_id="trakt-client", client_secret="secret")
    response = httpx.Response(
        200,
        json=[_trakt_hidden_entry()],
        request=httpx.Request("GET", "https://api.trakt.tv/users/hidden/dropped"),
    )
    client._request = AsyncMock(return_value=response)  # type: ignore[method-assign]

    entries = asyncio.run(
        client.get_hidden_items("token", section="dropped", item_type="show", per_page=50)
    )

    assert len(entries) == 1
    assert entries[0]["show"]["ids"]["trakt"] == 203632
    client._request.assert_awaited_once_with(  # type: ignore[attr-defined]
        "GET",
        "/users/hidden/dropped",
        access_token="token",
        params={"page": "1", "limit": "50", "type": "show"},
    )


def test_trakt_dropped_pass_builds_candidates_from_hidden_items() -> None:
    integration = SimpleNamespace(user_id="user-1")
    source = SimpleNamespace(id="src-dropped", external_id="dropped")
    client = SimpleNamespace(
        get_hidden_items=AsyncMock(return_value=[_trakt_hidden_entry()]),
    )

    with patch(
        "librarysync.jobs.trakt_import.process_dropped_candidates",
        new=AsyncMock(return_value=1),
    ) as process:
        imported = asyncio.run(
            trakt_import._import_dropped_for_source(
                None, integration, client, access_token="token", source=source, now=NOW
            )
        )

    assert imported == 1
    process.assert_awaited_once()
    args = process.await_args.args
    assert args[1] == "user-1"
    assert args[2] == "trakt"
    assert args[3] is source
    candidates = args[4]
    assert len(candidates) == 1
    candidate = candidates[0]
    assert candidate.media_type == "tv"
    assert candidate.ids["imdb_id"] == "tt0944947"
    assert candidate.ids["tmdb_id"] == "1399"
    assert candidate.ids["tvdb_id"] == "121361"


def test_trakt_dropped_pass_tolerates_fetch_failure() -> None:
    integration = SimpleNamespace(user_id="user-1")
    source = SimpleNamespace(id="src-dropped", external_id="dropped")
    client = SimpleNamespace(
        get_hidden_items=AsyncMock(side_effect=TraktError("boom", status_code=500)),
    )

    with patch(
        "librarysync.jobs.trakt_import.process_dropped_candidates",
        new=AsyncMock(return_value=0),
    ) as process:
        imported = asyncio.run(
            trakt_import._import_dropped_for_source(
                None, integration, client, access_token="token", source=source, now=NOW
            )
        )

    assert imported == 0
    process.assert_not_awaited()


def _simkl_all_items_payload(show_status: str) -> dict:
    return {
        "shows": [
            {
                "status": show_status,
                "show": {
                    "title": "The Last Ship",
                    "year": 2014,
                    "ids": {"simkl": 42040, "imdb": "tt2402207", "tvdb": 269533},
                },
            }
        ],
        "anime": [],
    }


def test_simkl_dropped_pass_extracts_only_dropped_entries() -> None:
    integration = SimpleNamespace(user_id="user-1")
    source = SimpleNamespace(id="src-dropped", external_id="dropped")

    async def fake_fetch(_token, *, category=None, **_kwargs):
        # One dropped entry in each show category; plantowatch entries are ignored.
        payload = _simkl_all_items_payload("dropped")
        if category == "anime":
            return {"shows": [], "anime": payload["shows"]}
        return payload

    client = SimpleNamespace(fetch_all_items=AsyncMock(side_effect=fake_fetch))

    with patch(
        "librarysync.jobs.simkl_import.process_dropped_candidates",
        new=AsyncMock(return_value=2),
    ) as process:
        imported = asyncio.run(
            simkl_import._import_dropped_for_source(
                None, integration, client, access_token="token", source=source, now=NOW
            )
        )

    assert imported == 2
    # Movies are never fetched for the dropped pass (dropped is tv/anime only).
    fetched_categories = [call.kwargs.get("category") for call in client.fetch_all_items.await_args_list]
    assert fetched_categories == ["shows", "anime"]
    candidates = process.await_args.args[4]
    assert len(candidates) == 2
    assert all(candidate.media_type == "tv" for candidate in candidates)


def test_simkl_dropped_pass_ignores_other_lists() -> None:
    integration = SimpleNamespace(user_id="user-1")
    source = SimpleNamespace(id="src-dropped", external_id="dropped")

    async def fake_fetch(_token, *, category=None, **_kwargs):
        return _simkl_all_items_payload("plantowatch")

    client = SimpleNamespace(fetch_all_items=AsyncMock(side_effect=fake_fetch))

    with patch(
        "librarysync.jobs.simkl_import.process_dropped_candidates",
        new=AsyncMock(return_value=0),
    ) as process:
        imported = asyncio.run(
            simkl_import._import_dropped_for_source(
                None, integration, client, access_token="token", source=source, now=NOW
            )
        )

    assert imported == 0
    # No dropped entries anywhere -> still processes (with empty candidates) so
    # reconcile can un-drop items that left the provider dropped list.
    process.assert_awaited_once()
    assert process.await_args.args[4] == []


def test_simkl_dropped_pass_tolerates_fetch_failure() -> None:
    integration = SimpleNamespace(user_id="user-1")
    source = SimpleNamespace(id="src-dropped", external_id="dropped")
    client = SimpleNamespace(
        fetch_all_items=AsyncMock(side_effect=SimklError("boom", status_code=500)),
    )

    with patch(
        "librarysync.jobs.simkl_import.process_dropped_candidates",
        new=AsyncMock(return_value=0),
    ) as process:
        imported = asyncio.run(
            simkl_import._import_dropped_for_source(
                None, integration, client, access_token="token", source=source, now=NOW
            )
        )

    assert imported == 0
    # A failed fetch must never reconcile with an empty seen set (mass un-drop).
    process.assert_not_awaited()


# ---------------------------------------------------------------------------
# DB-backed tests for process_dropped_candidates / reconcile_dropped_source
# ---------------------------------------------------------------------------


async def _make_session():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    return engine, async_sessionmaker(engine, expire_on_commit=False)


async def _seed_user_and_show(db, *, status="added", source="trakt") -> None:
    db.add(User(id="user-1", username="user1", password_hash="x"))
    db.add(
        MediaItem(
            id="show-1",
            media_type="tv",
            title="Some Show",
            imdb_id="tt0944947",
            tmdb_id="1399",
            first_air_date=date(2020, 1, 1),
        )
    )
    db.add(
        WatchlistItem(
            id="wl-1",
            user_id="user-1",
            media_item_id="show-1",
            type="tv",
            status=status,
            source=source,
        )
    )
    db.add(
        WatchlistSource(
            id="src-dropped",
            user_id="user-1",
            provider="trakt",
            source_type="personal",
            external_id="dropped",
            name="Trakt dropped",
        )
    )
    await db.commit()


def _dropped_candidate() -> WatchlistCandidate:
    return WatchlistCandidate(
        entry_key=None,
        media_type="tv",
        ids={"imdb_id": "tt0944947", "tmdb_id": "1399"},
        title="Some Show",
        year=2020,
        poster_url=None,
        raw=None,
        source="trakt",
    )


@pytest.mark.asyncio
async def test_process_dropped_candidates_marks_existing_item_dropped() -> None:
    engine, session_factory = await _make_session()
    async with session_factory() as db:
        await _seed_user_and_show(db, status="added")
        source = (
            await db.execute(select(WatchlistSource).where(WatchlistSource.id == "src-dropped"))
        ).scalars().one()

        marked = await process_dropped_candidates(
            db, "user-1", "trakt", source, [_dropped_candidate()], now=NOW
        )

        assert marked == 1
        item = (
            await db.execute(select(WatchlistItem).where(WatchlistItem.id == "wl-1"))
        ).scalars().one()
        assert item.status == "dropped"
        link = (
            await db.execute(
                select(WatchlistSourceItem).where(WatchlistSourceItem.source_id == "src-dropped")
            )
        ).scalars().one()
        assert link.watchlist_item_id == "wl-1"
    await engine.dispose()


@pytest.mark.asyncio
async def test_process_dropped_candidates_un_drops_when_show_leaves_provider_list() -> None:
    engine, session_factory = await _make_session()
    async with session_factory() as db:
        await _seed_user_and_show(db, status="dropped")
        # A second (watchlist) source link keeps the item alive on reconcile.
        db.add(
            WatchlistSource(
                id="src-watchlist",
                user_id="user-1",
                provider="trakt",
                source_type="personal",
                external_id="watchlist",
                name="Trakt watchlist",
            )
        )
        db.add(
            WatchlistSourceItem(
                id="link-dropped",
                source_id="src-dropped",
                watchlist_item_id="wl-1",
                user_id="user-1",
                media_item_id="show-1",
            )
        )
        db.add(
            WatchlistSourceItem(
                id="link-watchlist",
                source_id="src-watchlist",
                watchlist_item_id="wl-1",
                user_id="user-1",
                media_item_id="show-1",
            )
        )
        await db.commit()
        source = (
            await db.execute(select(WatchlistSource).where(WatchlistSource.id == "src-dropped"))
        ).scalars().one()

        # Empty candidate list: the show is no longer dropped on the provider.
        marked = await process_dropped_candidates(db, "user-1", "trakt", source, [], now=NOW)

        assert marked == 0
        item = (
            await db.execute(select(WatchlistItem).where(WatchlistItem.id == "wl-1"))
        ).scalars().one()
        assert item.status == "added"
        assert (
            await db.execute(
                select(WatchlistSourceItem).where(WatchlistSourceItem.id == "link-dropped")
            )
        ).scalars().first() is None
    await engine.dispose()


@pytest.mark.asyncio
async def test_process_dropped_candidates_deletes_orphaned_item() -> None:
    engine, session_factory = await _make_session()
    async with session_factory() as db:
        await _seed_user_and_show(db, status="dropped")
        db.add(
            WatchlistSourceItem(
                id="link-dropped",
                source_id="src-dropped",
                watchlist_item_id="wl-1",
                user_id="user-1",
                media_item_id="show-1",
            )
        )
        await db.commit()
        source = (
            await db.execute(select(WatchlistSource).where(WatchlistSource.id == "src-dropped"))
        ).scalars().one()

        await process_dropped_candidates(db, "user-1", "trakt", source, [], now=NOW)

        assert (
            await db.execute(select(WatchlistItem).where(WatchlistItem.id == "wl-1"))
        ).scalars().first() is None
    await engine.dispose()


@pytest.mark.asyncio
async def test_watchlist_import_does_not_resurrect_dropped_item() -> None:
    """A show present in both the provider watchlist and its dropped list must
    stay dropped across import runs (no flip-flop)."""
    engine, session_factory = await _make_session()
    async with session_factory() as db:
        await _seed_user_and_show(db, status="dropped")
        db.add(
            WatchlistSource(
                id="src-watchlist",
                user_id="user-1",
                provider="trakt",
                source_type="personal",
                external_id="watchlist",
                name="Trakt watchlist",
            )
        )
        await db.commit()
        source = (
            await db.execute(select(WatchlistSource).where(WatchlistSource.id == "src-watchlist"))
        ).scalars().one()

        await process_watchlist_candidates(
            db, "user-1", "trakt", source, [_dropped_candidate()], now=NOW
        )

        item = (
            await db.execute(select(WatchlistItem).where(WatchlistItem.id == "wl-1"))
        ).scalars().one()
        assert item.status == "dropped"
    await engine.dispose()


@pytest.mark.asyncio
async def test_process_dropped_candidates_clears_rewatch_request() -> None:
    engine, session_factory = await _make_session()
    async with session_factory() as db:
        await _seed_user_and_show(db, status="added")
        item = (
            await db.execute(select(WatchlistItem).where(WatchlistItem.id == "wl-1"))
        ).scalars().one()
        item.rewatch_requested = True
        item.rewatch_requested_at = NOW
        await db.commit()
        source = (
            await db.execute(select(WatchlistSource).where(WatchlistSource.id == "src-dropped"))
        ).scalars().one()

        marked = await process_dropped_candidates(
            db, "user-1", "trakt", source, [_dropped_candidate()], now=NOW
        )

        assert marked == 1
        assert item.status == "dropped"
        assert item.rewatch_requested is False
        assert item.rewatch_requested_at is None
    await engine.dispose()


@pytest.mark.asyncio
async def test_dropped_import_never_enqueues_provider_sync() -> None:
    """Dropped import upserts must never echo items back out to providers."""
    engine, session_factory = await _make_session()
    async with session_factory() as db:
        db.add(User(id="user-1", username="user1", password_hash="x"))
        db.add(
            WatchlistSource(
                id="src-dropped",
                user_id="user-1",
                provider="trakt",
                source_type="personal",
                external_id="dropped",
                name="Trakt dropped",
            )
        )
        await db.commit()
        source = (
            await db.execute(select(WatchlistSource).where(WatchlistSource.id == "src-dropped"))
        ).scalars().one()

        with patch(
            "librarysync.core.watchlist._enqueue_watchlist_sync",
            new=AsyncMock(),
        ) as enqueue:
            # A brand-new show exercises the watchlist-item creation path.
            marked = await process_dropped_candidates(
                db, "user-1", "trakt", source, [_dropped_candidate()], now=NOW
            )

        assert marked == 1
        item = (
            await db.execute(select(WatchlistItem).where(WatchlistItem.user_id == "user-1"))
        ).scalars().one()
        assert item.status == "dropped"
        enqueue.assert_not_awaited()
    await engine.dispose()
