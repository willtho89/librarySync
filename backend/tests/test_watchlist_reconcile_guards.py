"""Watchlist imports must not reconcile removals against failed or page-capped listings."""

import asyncio
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from librarysync.connectors.services.letterboxd import LetterboxdClient
from librarysync.connectors.services.pagination import PagedEntries
from librarysync.connectors.services.simkl import SimklError
from librarysync.connectors.services.trakt import TraktClient, TraktError
from librarysync.jobs import letterboxd_import, simkl_import, trakt_import

NOW = datetime(2026, 10, 6, 12, 0, tzinfo=timezone.utc)
PERSONAL = SimpleNamespace(id="src-personal", source_type="personal", external_id=None, url=None)
INTEGRATION = SimpleNamespace(user_id="user-1")


def _trakt_movie(trakt_id: int) -> dict:
    return {
        "type": "movie",
        "listed_at": "2026-09-01T10:00:00.000Z",
        "movie": {"title": f"Movie {trakt_id}", "year": 2020, "ids": {"trakt": trakt_id, "imdb": f"tt{trakt_id:07d}"}},
    }


def _patched(module):
    return (
        patch.object(module, "ensure_personal_watchlist_source", AsyncMock()),
        patch.object(module, "ensure_dropped_watchlist_source", AsyncMock()),
        patch.object(module, "process_watchlist_candidates", AsyncMock(return_value=0)),
        patch.object(module, "reconcile_watchlist_source", AsyncMock(return_value=0)),
    )


def _run_trakt(client) -> tuple[AsyncMock, AsyncMock]:
    ensure_personal, ensure_dropped, process, reconcile = _patched(trakt_import)
    with ensure_personal, ensure_dropped, process as process_mock, reconcile as reconcile_mock:
        asyncio.run(
            trakt_import._import_watchlist_for_integration(
                None, INTEGRATION, client, "token", NOW, lookback_days=7, sources=[PERSONAL]
            )
        )
    return process_mock, reconcile_mock


def test_trakt_watchlist_fetch_failure_does_not_wipe_watchlist() -> None:
    client = SimpleNamespace(get_watchlist=AsyncMock(side_effect=TraktError("down", status_code=503)))

    process, reconcile = _run_trakt(client)

    process.assert_not_awaited()
    reconcile.assert_not_awaited()


def test_trakt_partial_fetch_failure_skips_reconcile() -> None:
    async def _get_watchlist(_token, *, watchlist_type, **_kwargs):
        if watchlist_type == "shows":
            raise TraktError("down", status_code=503)
        return [_trakt_movie(1)]

    process, reconcile = _run_trakt(SimpleNamespace(get_watchlist=_get_watchlist))

    process.assert_awaited_once()
    assert process.await_args.kwargs["reconcile"] is False
    reconcile.assert_not_awaited()


def test_trakt_complete_empty_watchlist_still_reconciles() -> None:
    process, reconcile = _run_trakt(SimpleNamespace(get_watchlist=AsyncMock(return_value=[])))

    process.assert_not_awaited()
    reconcile.assert_awaited_once()


def test_trakt_paginator_flags_page_cap_truncation() -> None:
    client = TraktClient(client_id="id", client_secret="secret")
    full_page = [_trakt_movie(i) for i in range(2)]
    client.fetch_watchlist = AsyncMock(return_value=(full_page, {}))  # type: ignore[method-assign]

    entries = asyncio.run(client.get_watchlist("token", watchlist_type="movies", per_page=2, max_pages=3))

    assert len(entries) == 6
    assert entries.truncated is True


def test_trakt_paginator_complete_when_provider_ends_list() -> None:
    client = TraktClient(client_id="id", client_secret="secret")
    client.fetch_watchlist = AsyncMock(  # type: ignore[method-assign]
        side_effect=[([_trakt_movie(1), _trakt_movie(2)], {}), ([_trakt_movie(3)], {})]
    )

    entries = asyncio.run(client.get_watchlist("token", watchlist_type="movies", per_page=2, max_pages=3))

    assert len(entries) == 3
    assert entries.truncated is False


def test_trakt_truncated_watchlist_imports_without_reconcile() -> None:
    client = TraktClient(client_id="id", client_secret="secret")
    full_page = [_trakt_movie(i) for i in range(trakt_import.WATCHLIST_PER_PAGE)]
    client.fetch_watchlist = AsyncMock(return_value=(full_page, {}))  # type: ignore[method-assign]

    process, reconcile = _run_trakt(client)

    process.assert_awaited_once()
    assert process.await_args.kwargs["reconcile"] is False
    reconcile.assert_not_awaited()


def test_simkl_failed_category_fetch_skips_reconcile() -> None:
    async def _fetch_all_items(_token, *, category, **_kwargs):
        if category == "shows":
            raise SimklError("down", status_code=503)
        return {category: []}

    client = SimpleNamespace(fetch_all_items=_fetch_all_items)
    ensure_personal, ensure_dropped, process, reconcile = _patched(simkl_import)
    with ensure_personal, ensure_dropped, process as process_mock, reconcile as reconcile_mock:
        asyncio.run(
            simkl_import._import_watchlist_for_integration(None, INTEGRATION, client, "token", NOW, sources=[PERSONAL])
        )

    process_mock.assert_not_awaited()
    reconcile_mock.assert_not_awaited()


def test_letterboxd_paginator_flags_page_cap_truncation() -> None:
    client = LetterboxdClient.__new__(LetterboxdClient)
    page = {"items": [{"film": {"id": "a"}}], "next": "cursor"}
    client.fetch_watchlist = AsyncMock(return_value=page)  # type: ignore[method-assign]

    with patch(
        "librarysync.connectors.services.letterboxd._extract_watchlist_items",
        side_effect=lambda payload: payload["items"],
    ):
        entries = asyncio.run(client.get_watchlist("token", member_id="m", per_page=1, max_pages=2))

    assert len(entries) == 2
    assert entries.truncated is True


def test_letterboxd_truncated_watchlist_imports_without_reconcile() -> None:
    entries = PagedEntries([{"film": {"id": "a"}}])
    entries.truncated = True
    client = SimpleNamespace(get_watchlist=AsyncMock(return_value=entries))
    candidate = SimpleNamespace(entry_key="k")
    ensure_personal = patch.object(letterboxd_import, "ensure_personal_watchlist_source", AsyncMock())
    process = patch.object(letterboxd_import, "process_watchlist_candidates", AsyncMock(return_value=1))
    reconcile = patch.object(letterboxd_import, "reconcile_watchlist_source", AsyncMock(return_value=0))
    build = patch.object(letterboxd_import, "_build_watchlist_candidate", return_value=candidate)
    with ensure_personal, process as process_mock, reconcile as reconcile_mock, build:
        asyncio.run(
            letterboxd_import._import_watchlist_for_integration(
                None, INTEGRATION, client, "token", "member", NOW, sources=[PERSONAL]
            )
        )

    process_mock.assert_awaited_once()
    assert process_mock.await_args.kwargs["reconcile"] is False
    reconcile_mock.assert_not_awaited()
