"""Tests for show watchlist handling in process_new_item_job.

Covers:
- First watch of a show: the watchlist item is created, evaluated exactly once,
  and pushed to connected providers exactly once (no redundant evaluation via
  check_and_update_watchlist).
- Subsequent watches: the pre-existing item is evaluated exactly once via
  check_and_update_watchlist and not pushed again.
"""

import asyncio
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from librarysync.core.watch_pipeline import process_new_item_job

NOW = datetime(2026, 7, 18, 10, 0, tzinfo=timezone.utc)


class _FakeScalarResult:
    def __init__(self, value):
        self._value = value

    def first(self):
        return self._value


class _FakeResult:
    def __init__(self, scalar=None):
        self._scalar = scalar

    def scalars(self):
        return _FakeScalarResult(self._scalar)


class _FakeNestedTransaction:
    """Mimics session.begin_nested(): rolls back to the savepoint and re-raises on error."""

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False


def _make_watched() -> SimpleNamespace:
    return SimpleNamespace(
        id="watched-1",
        user_id="user-1",
        media_item_id="media-1",
        episode_item_id=None,
        watched_at=NOW,
        rating=None,
    )


def _make_show_without_ids(media_type: str = "tv") -> SimpleNamespace:
    return SimpleNamespace(
        id="media-1",
        media_type=media_type,
        imdb_id=None,
        tmdb_id=None,
        tvdb_id=None,
        tvmaze_id=None,
        kitsu_id=None,
        myanimelist_id=None,
        anilist_id=None,
        title="Sample Show",
        year=2011,
        poster_url=None,
        first_air_date=None,
        raw={},
    )


def _make_db(watched, media_item, execute_side_effects):
    async def fake_get(model, pk):
        if model.__name__ == "WatchedItem":
            return watched
        if model.__name__ == "MediaItem":
            return media_item
        return None

    return SimpleNamespace(
        get=AsyncMock(side_effect=fake_get),
        execute=AsyncMock(side_effect=execute_side_effects),
        add=MagicMock(),
        flush=AsyncMock(),
        begin_nested=MagicMock(return_value=_FakeNestedTransaction()),
    )


def _make_job() -> SimpleNamespace:
    return SimpleNamespace(payload={"watched_item_id": "watched-1"})


def test_first_watch_creates_and_evaluates_show_watchlist_item_exactly_once() -> None:
    watched = _make_watched()
    media_item = _make_show_without_ids()
    db = _make_db(
        watched,
        media_item,
        execute_side_effects=[
            _FakeResult(scalar=None),  # ensure: no existing watchlist item
        ],
    )

    with (
        patch("librarysync.core.watch_pipeline.enrich_watched_metadata", new_callable=AsyncMock),
        patch("librarysync.core.watch_pipeline.backfill_show_episodes", new_callable=AsyncMock),
        patch("librarysync.core.watch_pipeline._sync_to_integrations", new_callable=AsyncMock),
        patch(
            "librarysync.core.watchlist.evaluate_show_watchlist_status", new_callable=AsyncMock
        ) as evaluate,
        patch("librarysync.core.watchlist._enqueue_watchlist_sync", new_callable=AsyncMock) as sync,
    ):
        asyncio.run(process_new_item_job(db, _make_job()))

    # Newly created item is evaluated by ensure_show_watchlist_item; the
    # follow-up check_and_update_watchlist must be skipped.
    assert evaluate.await_count == 1
    # Auto-created item is pushed to connected providers exactly once.
    assert sync.await_count == 1
    sync_args = sync.await_args.args
    assert sync_args[0] is db
    assert sync_args[1].media_item_id == "media-1"
    assert sync_args[2] is media_item


def test_second_watch_evaluates_existing_item_exactly_once_without_push() -> None:
    watched = _make_watched()
    media_item = _make_show_without_ids()
    existing_item = SimpleNamespace(
        id="wl-existing",
        user_id="user-1",
        media_item_id="media-1",
        status="added",
        rewatch_requested=False,
    )
    db = _make_db(
        watched,
        media_item,
        execute_side_effects=[
            _FakeResult(scalar=existing_item),  # ensure: item already exists
            _FakeResult(scalar=existing_item),  # check_and_update: fetch item
            _FakeResult(scalar=media_item),  # check_and_update: fetch media item
        ],
    )

    with (
        patch("librarysync.core.watch_pipeline.enrich_watched_metadata", new_callable=AsyncMock),
        patch("librarysync.core.watch_pipeline.backfill_show_episodes", new_callable=AsyncMock),
        patch("librarysync.core.watch_pipeline._sync_to_integrations", new_callable=AsyncMock),
        patch(
            "librarysync.core.watchlist.evaluate_show_watchlist_status", new_callable=AsyncMock
        ) as evaluate,
        patch("librarysync.core.watchlist._enqueue_watchlist_sync", new_callable=AsyncMock) as sync,
    ):
        asyncio.run(process_new_item_job(db, _make_job()))

    # Pre-existing item is evaluated once by check_and_update_watchlist.
    assert evaluate.await_count == 1
    evaluate.assert_awaited_with(db, "user-1", existing_item, media_item)
    sync.assert_not_awaited()


def test_pipeline_skips_check_and_update_when_item_was_created_and_evaluated() -> None:
    watched = _make_watched()
    media_item = _make_show_without_ids()
    db = _make_db(watched, media_item, execute_side_effects=[])

    with (
        patch("librarysync.core.watch_pipeline.enrich_watched_metadata", new_callable=AsyncMock),
        patch("librarysync.core.watch_pipeline.backfill_show_episodes", new_callable=AsyncMock),
        patch("librarysync.core.watch_pipeline._sync_to_integrations", new_callable=AsyncMock),
        patch(
            "librarysync.core.watch_pipeline.ensure_show_watchlist_item",
            new_callable=AsyncMock,
            return_value=(SimpleNamespace(id="wl-1"), True),
        ),
        patch(
            "librarysync.core.watch_pipeline.check_and_update_watchlist", new_callable=AsyncMock
        ) as check,
    ):
        asyncio.run(process_new_item_job(db, _make_job()))

    check.assert_not_awaited()


def test_pipeline_runs_check_and_update_when_item_already_existed() -> None:
    watched = _make_watched()
    media_item = _make_show_without_ids()
    db = _make_db(watched, media_item, execute_side_effects=[])

    with (
        patch("librarysync.core.watch_pipeline.enrich_watched_metadata", new_callable=AsyncMock),
        patch("librarysync.core.watch_pipeline.backfill_show_episodes", new_callable=AsyncMock),
        patch("librarysync.core.watch_pipeline._sync_to_integrations", new_callable=AsyncMock),
        patch(
            "librarysync.core.watch_pipeline.ensure_show_watchlist_item",
            new_callable=AsyncMock,
            return_value=(SimpleNamespace(id="wl-1"), False),
        ),
        patch(
            "librarysync.core.watch_pipeline.check_and_update_watchlist", new_callable=AsyncMock
        ) as check,
    ):
        asyncio.run(process_new_item_job(db, _make_job()))

    check.assert_awaited_once_with(db, "user-1", "media-1", watched_at=NOW)


def test_dropped_show_watch_restores_and_pushes_watchlist_sync() -> None:
    watched = _make_watched()
    media_item = _make_show_without_ids("tv")
    existing_item = SimpleNamespace(
        id="wl-existing",
        user_id="user-1",
        media_item_id="media-1",
        status="dropped",
        rewatch_requested=False,
    )
    db = _make_db(
        watched,
        media_item,
        execute_side_effects=[
            _FakeResult(scalar=existing_item),  # ensure: item already exists
            _FakeResult(scalar=existing_item),  # check_and_update: fetch item
            _FakeResult(scalar=media_item),  # check_and_update: fetch media item
        ],
    )

    with (
        patch("librarysync.core.watch_pipeline.enrich_watched_metadata", new_callable=AsyncMock),
        patch("librarysync.core.watch_pipeline.backfill_show_episodes", new_callable=AsyncMock),
        patch("librarysync.core.watch_pipeline._sync_to_integrations", new_callable=AsyncMock),
        patch(
            "librarysync.core.watchlist.evaluate_show_watchlist_status", new_callable=AsyncMock
        ) as evaluate,
        patch("librarysync.core.watchlist._enqueue_watchlist_sync", new_callable=AsyncMock) as sync,
    ):
        asyncio.run(process_new_item_job(db, _make_job()))

    assert existing_item.status == "added"
    evaluate.assert_awaited_once_with(db, "user-1", existing_item, media_item)
    sync.assert_awaited_once_with(db, existing_item, media_item, unhide_dropped=True)


def test_dropped_anime_watch_restores_and_pushes_watchlist_sync() -> None:
    watched = _make_watched()
    media_item = _make_show_without_ids("anime")
    existing_item = SimpleNamespace(
        id="wl-existing",
        user_id="user-1",
        media_item_id="media-1",
        status="dropped",
        type="anime",
        rewatch_requested=False,
    )
    db = _make_db(
        watched,
        media_item,
        execute_side_effects=[
            _FakeResult(scalar=existing_item),  # ensure: item already exists
            _FakeResult(scalar=existing_item),  # check_and_update: fetch item
            _FakeResult(scalar=media_item),  # check_and_update: fetch media item
        ],
    )

    with (
        patch("librarysync.core.watch_pipeline.enrich_watched_metadata", new_callable=AsyncMock),
        patch("librarysync.core.watch_pipeline.backfill_show_episodes", new_callable=AsyncMock),
        patch("librarysync.core.watch_pipeline._sync_to_integrations", new_callable=AsyncMock),
        patch(
            "librarysync.core.watchlist.evaluate_show_watchlist_status", new_callable=AsyncMock
        ) as evaluate,
        patch("librarysync.core.watchlist._enqueue_watchlist_sync", new_callable=AsyncMock) as sync,
    ):
        asyncio.run(process_new_item_job(db, _make_job()))

    assert existing_item.status == "added"
    evaluate.assert_awaited_once_with(db, "user-1", existing_item, media_item)
    sync.assert_awaited_once_with(db, existing_item, media_item, unhide_dropped=True)
