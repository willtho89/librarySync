import asyncio
from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import ANY, AsyncMock, MagicMock, patch

import pytest
from librarysync.core import watchlist
from librarysync.db.models import WatchlistItem
from sqlalchemy.exc import IntegrityError


def _as_media_item(value: Any) -> Any:
    return cast(Any, value)


def _make_show(media_type: str = "tv") -> SimpleNamespace:
    return SimpleNamespace(
        id="media-1",
        media_type=media_type,
        imdb_id="tt1234567",
        tmdb_id="1399",
        tvdb_id=None,
        tvmaze_id=None,
        kitsu_id=None,
        myanimelist_id=None,
        anilist_id=None,
        title="Sample Show",
        year=2011,
        poster_url="https://example.com/poster.jpg",
        first_air_date=None,
        raw={},
    )


def _make_show_without_ids(media_type: str = "tv") -> SimpleNamespace:
    show = _make_show(media_type)
    show.imdb_id = None
    show.tmdb_id = None
    return show


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


def _make_db(**overrides: Any) -> SimpleNamespace:
    db = SimpleNamespace(
        execute=AsyncMock(return_value=_FakeResult(scalar=None)),
        add=MagicMock(),
        flush=AsyncMock(),
        begin_nested=MagicMock(return_value=_FakeNestedTransaction()),
    )
    for key, value in overrides.items():
        setattr(db, key, value)
    return db


def _unique_violation(constraint: str = "uq_watchlist_items_user_media") -> IntegrityError:
    orig = SimpleNamespace(diag=SimpleNamespace(constraint_name=constraint))
    return IntegrityError("INSERT INTO watchlist_items ...", {}, orig)


def test_ensure_show_watchlist_item_adds_tv_show_from_history() -> None:
    now = datetime(2026, 7, 18, 10, 0, tzinfo=timezone.utc)
    db = _make_db()
    created_item = SimpleNamespace(id="wl-1")

    with patch("librarysync.core.watchlist.upsert_watchlist_item", new_callable=AsyncMock) as upsert:
        upsert.return_value = (created_item, "created")

        result = asyncio.run(
            watchlist.ensure_show_watchlist_item(
                db=db,
                user_id="user-1",
                media_item=_as_media_item(_make_show("tv")),
                watched_at=now,
            )
        )

    assert result == (created_item, True)
    upsert.assert_awaited_once_with(
        ANY,
        "user-1",
        "tv",
        {
            "imdb_id": "tt1234567",
            "tmdb_id": "1399",
        },
        "Sample Show",
        2011,
        "https://example.com/poster.jpg",
        "auto_from_history",
        now=now,
        event_raw={},
        enqueue_sync=True,
    )


def test_ensure_show_watchlist_item_skips_non_show_types() -> None:
    with patch("librarysync.core.watchlist.upsert_watchlist_item", new_callable=AsyncMock) as upsert:
        result = asyncio.run(
            watchlist.ensure_show_watchlist_item(
                db=SimpleNamespace(),
                user_id="user-1",
                media_item=_as_media_item(_make_show("movie")),
                watched_at=datetime(2026, 7, 18, tzinfo=timezone.utc),
            )
        )

    assert result == (None, False)
    upsert.assert_not_awaited()


def test_ensure_show_watchlist_item_returns_existing_without_ids() -> None:
    media_item = _make_show_without_ids("tv")

    existing_item = SimpleNamespace(id="wl-existing", user_id="user-1", media_item_id="media-1")
    db = _make_db(execute=AsyncMock(return_value=_FakeResult(scalar=existing_item)))

    with (
        patch("librarysync.core.watchlist.upsert_watchlist_item", new_callable=AsyncMock) as upsert,
        patch(
            "librarysync.core.watchlist.evaluate_show_watchlist_status", new_callable=AsyncMock
        ) as evaluate,
    ):
        result = asyncio.run(
            watchlist.ensure_show_watchlist_item(
                db=db,
                user_id="user-1",
                media_item=_as_media_item(media_item),
                watched_at=datetime(2026, 7, 18, 10, 0, tzinfo=timezone.utc),
            )
        )

    assert result == (existing_item, False)
    db.add.assert_not_called()
    db.flush.assert_not_awaited()
    upsert.assert_not_awaited()
    evaluate.assert_not_awaited()


def test_ensure_show_watchlist_item_creates_missing_without_ids() -> None:
    media_item = _make_show_without_ids("tv")
    db = _make_db()

    with (
        patch("librarysync.core.watchlist.log_watchlist_event", new_callable=AsyncMock) as log_event,
        patch(
            "librarysync.core.watchlist.evaluate_show_watchlist_status", new_callable=AsyncMock
        ) as evaluate,
        patch("librarysync.core.watchlist._enqueue_watchlist_sync", new_callable=AsyncMock) as sync,
    ):
        item, evaluated = asyncio.run(
            watchlist.ensure_show_watchlist_item(
                db=db,
                user_id="user-1",
                media_item=_as_media_item(media_item),
                watched_at=datetime(2026, 7, 18, 10, 0, tzinfo=timezone.utc),
            )
        )

    assert evaluated is True
    assert isinstance(item, WatchlistItem)
    assert item.user_id == "user-1"
    assert item.media_item_id == media_item.id
    assert item.type == "tv"
    assert item.status == "added"
    assert item.source == "auto_from_history"
    db.begin_nested.assert_called_once()
    db.add.assert_called_once()
    db.flush.assert_awaited_once()
    log_event.assert_awaited_once_with(
        db,
        "user-1",
        media_item.id,
        "watchlist_added",
        {"source": "auto_from_history"},
    )
    evaluate.assert_awaited_once_with(db, "user-1", item, media_item)
    sync.assert_awaited_once_with(db, item, media_item)


def test_ensure_show_watchlist_item_recovers_from_insert_race() -> None:
    media_item = _make_show_without_ids("tv")
    existing_item = SimpleNamespace(
        id="wl-existing",
        user_id="user-1",
        media_item_id="media-1",
        status="added",
    )
    db = _make_db(
        execute=AsyncMock(
            side_effect=[
                _FakeResult(scalar=None),  # initial existence check
                _FakeResult(scalar=existing_item),  # re-fetch after unique violation
            ]
        ),
        flush=AsyncMock(side_effect=_unique_violation()),
    )

    with (
        patch("librarysync.core.watchlist.log_watchlist_event", new_callable=AsyncMock),
        patch(
            "librarysync.core.watchlist.evaluate_show_watchlist_status", new_callable=AsyncMock
        ) as evaluate,
        patch("librarysync.core.watchlist._enqueue_watchlist_sync", new_callable=AsyncMock) as sync,
    ):
        item, evaluated = asyncio.run(
            watchlist.ensure_show_watchlist_item(
                db=db,
                user_id="user-1",
                media_item=_as_media_item(media_item),
                watched_at=datetime(2026, 7, 18, 10, 0, tzinfo=timezone.utc),
            )
        )

    assert item is existing_item
    assert evaluated is False
    evaluate.assert_not_awaited()
    sync.assert_not_awaited()


def test_ensure_show_watchlist_item_reraises_unrelated_integrity_error() -> None:
    media_item = _make_show_without_ids("tv")
    db = _make_db(
        flush=AsyncMock(side_effect=_unique_violation("uq_some_other_constraint")),
    )

    with patch("librarysync.core.watchlist.log_watchlist_event", new_callable=AsyncMock):
        with pytest.raises(IntegrityError):
            asyncio.run(
                watchlist.ensure_show_watchlist_item(
                    db=db,
                    user_id="user-1",
                    media_item=_as_media_item(media_item),
                    watched_at=datetime(2026, 7, 18, 10, 0, tzinfo=timezone.utc),
                )
            )


def test_upsert_watchlist_item_recovers_from_insert_race() -> None:
    media_item = _make_show("tv")
    existing_item = SimpleNamespace(
        id="wl-existing",
        user_id="user-1",
        media_item_id="media-1",
        status="added",
        source="auto_from_history",
    )
    db = _make_db(
        execute=AsyncMock(
            side_effect=[
                _FakeResult(scalar=media_item),  # find_media_item_by_ids
                _FakeResult(scalar=None),  # initial watchlist existence check
                _FakeResult(scalar=existing_item),  # re-fetch after unique violation
            ]
        ),
        flush=AsyncMock(side_effect=_unique_violation()),
    )

    with (
        patch("librarysync.core.watchlist.log_watchlist_event", new_callable=AsyncMock),
        patch(
            "librarysync.core.watchlist.evaluate_show_watchlist_status", new_callable=AsyncMock
        ) as evaluate,
        patch("librarysync.core.watchlist._enqueue_watchlist_sync", new_callable=AsyncMock) as sync,
    ):
        result = asyncio.run(
            watchlist.upsert_watchlist_item(
                db,
                "user-1",
                "tv",
                {"imdb_id": "tt1234567", "tmdb_id": "1399"},
                "Sample Show",
                2011,
                "https://example.com/poster.jpg",
                "auto_from_history",
                enqueue_sync=True,
            )
        )

    assert result == (existing_item, "already_exists")
    evaluate.assert_not_awaited()
    # The concurrent writer that inserted the row owns the sync push.
    sync.assert_not_awaited()


def test_upsert_watchlist_item_reraises_unrelated_integrity_error() -> None:
    media_item = _make_show("tv")
    db = _make_db(
        execute=AsyncMock(
            side_effect=[
                _FakeResult(scalar=media_item),  # find_media_item_by_ids
                _FakeResult(scalar=None),  # initial watchlist existence check
            ]
        ),
        flush=AsyncMock(side_effect=_unique_violation("uq_some_other_constraint")),
    )

    with patch("librarysync.core.watchlist.log_watchlist_event", new_callable=AsyncMock):
        with pytest.raises(IntegrityError):
            asyncio.run(
                watchlist.upsert_watchlist_item(
                    db,
                    "user-1",
                    "tv",
                    {"imdb_id": "tt1234567", "tmdb_id": "1399"},
                    "Sample Show",
                    2011,
                    None,
                    "auto_from_history",
                )
            )


def test_upsert_watchlist_item_enqueues_sync_on_create_when_requested() -> None:
    media_item = _make_show("tv")
    db = _make_db(
        execute=AsyncMock(
            side_effect=[
                _FakeResult(scalar=media_item),  # find_media_item_by_ids
                _FakeResult(scalar=None),  # initial watchlist existence check
            ]
        ),
    )

    with (
        patch("librarysync.core.watchlist.log_watchlist_event", new_callable=AsyncMock),
        patch(
            "librarysync.core.watchlist.evaluate_show_watchlist_status", new_callable=AsyncMock
        ) as evaluate,
        patch("librarysync.core.watchlist._enqueue_watchlist_sync", new_callable=AsyncMock) as sync,
    ):
        item, status = asyncio.run(
            watchlist.upsert_watchlist_item(
                db,
                "user-1",
                "tv",
                {"imdb_id": "tt1234567", "tmdb_id": "1399"},
                "Sample Show",
                2011,
                None,
                "auto_from_history",
                enqueue_sync=True,
            )
        )

    assert status == "created"
    assert isinstance(item, WatchlistItem)
    evaluate.assert_awaited_once_with(db, "user-1", item, media_item)
    sync.assert_awaited_once_with(db, item, media_item)


def test_upsert_watchlist_item_does_not_enqueue_sync_by_default() -> None:
    # Default path used by the manual API route (enqueues itself) and import pipeline.
    media_item = _make_show("tv")
    db = _make_db(
        execute=AsyncMock(
            side_effect=[
                _FakeResult(scalar=media_item),  # find_media_item_by_ids
                _FakeResult(scalar=None),  # initial watchlist existence check
            ]
        ),
    )

    with (
        patch("librarysync.core.watchlist.log_watchlist_event", new_callable=AsyncMock),
        patch("librarysync.core.watchlist.evaluate_show_watchlist_status", new_callable=AsyncMock),
        patch("librarysync.core.watchlist._enqueue_watchlist_sync", new_callable=AsyncMock) as sync,
    ):
        item, status = asyncio.run(
            watchlist.upsert_watchlist_item(
                db,
                "user-1",
                "tv",
                {"imdb_id": "tt1234567", "tmdb_id": "1399"},
                "Sample Show",
                2011,
                None,
                "manual",
            )
        )

    assert status == "created"
    assert isinstance(item, WatchlistItem)
    sync.assert_not_awaited()


def test_upsert_watchlist_item_enqueues_sync_on_restore_when_requested() -> None:
    media_item = _make_show("tv")
    removed_item = SimpleNamespace(
        id="wl-removed",
        user_id="user-1",
        media_item_id="media-1",
        status="removed",
        source="auto_from_history",
        updated_at=None,
    )
    db = _make_db(
        execute=AsyncMock(
            side_effect=[
                _FakeResult(scalar=media_item),  # find_media_item_by_ids
                _FakeResult(scalar=removed_item),  # initial watchlist existence check
            ]
        ),
    )

    with (
        patch("librarysync.core.watchlist.log_watchlist_event", new_callable=AsyncMock),
        patch(
            "librarysync.core.watchlist.evaluate_show_watchlist_status", new_callable=AsyncMock
        ) as evaluate,
        patch("librarysync.core.watchlist._enqueue_watchlist_sync", new_callable=AsyncMock) as sync,
    ):
        item, status = asyncio.run(
            watchlist.upsert_watchlist_item(
                db,
                "user-1",
                "tv",
                {"imdb_id": "tt1234567", "tmdb_id": "1399"},
                "Sample Show",
                2011,
                None,
                "auto_from_history",
                enqueue_sync=True,
            )
        )

    assert status == "restored"
    assert item is removed_item
    assert removed_item.status == "added"
    evaluate.assert_awaited_once_with(db, "user-1", removed_item, media_item)
    sync.assert_awaited_once_with(db, removed_item, media_item)
