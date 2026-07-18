import asyncio
from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import ANY, AsyncMock, MagicMock, patch

from librarysync.core import watchlist
from librarysync.db.models import WatchlistItem


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
    )


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


def test_ensure_show_watchlist_item_adds_tv_show_from_history() -> None:
    now = datetime(2026, 7, 18, 10, 0, tzinfo=timezone.utc)
    db = SimpleNamespace(execute=AsyncMock(return_value=_FakeResult(scalar=None)))

    with patch("librarysync.core.watchlist.upsert_watchlist_item", new_callable=AsyncMock) as upsert:
        upsert.return_value = (SimpleNamespace(id="wl-1"), "created")

        asyncio.run(
            watchlist.ensure_show_watchlist_item(
                db=db,
                user_id="user-1",
                media_item=_as_media_item(_make_show("tv")),
                watched_at=now,
            )
        )

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
    )


def test_ensure_show_watchlist_item_skips_non_show_types() -> None:
    with patch("librarysync.core.watchlist.upsert_watchlist_item", new_callable=AsyncMock) as upsert:
        asyncio.run(
            watchlist.ensure_show_watchlist_item(
                db=SimpleNamespace(),
                user_id="user-1",
                media_item=_as_media_item(_make_show("movie")),
                watched_at=datetime(2026, 7, 18, tzinfo=timezone.utc),
            )
        )

    upsert.assert_not_awaited()


def test_ensure_show_watchlist_item_returns_existing_without_ids() -> None:
    media_item = _make_show("tv")
    media_item.imdb_id = None
    media_item.tmdb_id = None
    media_item.tvdb_id = None
    media_item.tvmaze_id = None
    media_item.kitsu_id = None
    media_item.myanimelist_id = None
    media_item.anilist_id = None

    existing_item = SimpleNamespace(id="wl-existing", user_id="user-1", media_item_id="media-1")
    db = SimpleNamespace(
        execute=AsyncMock(return_value=_FakeResult(scalar=existing_item)),
        add=MagicMock(),
        flush=AsyncMock(),
    )

    with (
        patch("librarysync.core.watchlist.upsert_watchlist_item", new_callable=AsyncMock) as upsert,
        patch("librarysync.core.watchlist.evaluate_show_watchlist_status", new_callable=AsyncMock) as evaluate,
    ):
        result = asyncio.run(
            watchlist.ensure_show_watchlist_item(
                db=db,
                user_id="user-1",
                media_item=_as_media_item(media_item),
                watched_at=datetime(2026, 7, 18, 10, 0, tzinfo=timezone.utc),
            )
        )

    assert result is existing_item
    db.add.assert_not_called()
    db.flush.assert_not_awaited()
    upsert.assert_not_awaited()
    evaluate.assert_not_awaited()


def test_ensure_show_watchlist_item_creates_missing_without_ids() -> None:
    media_item = _make_show("tv")
    media_item.imdb_id = None
    media_item.tmdb_id = None
    media_item.tvdb_id = None
    media_item.tvmaze_id = None
    media_item.kitsu_id = None
    media_item.myanimelist_id = None
    media_item.anilist_id = None
    media_item.first_air_date = None

    db = SimpleNamespace(
        execute=AsyncMock(return_value=_FakeResult(scalar=None)),
        add=MagicMock(),
        flush=AsyncMock(),
    )

    with (
        patch("librarysync.core.watchlist.log_watchlist_event", new_callable=AsyncMock) as log_event,
        patch("librarysync.core.watchlist.evaluate_show_watchlist_status", new_callable=AsyncMock) as evaluate,
    ):
        result = asyncio.run(
            watchlist.ensure_show_watchlist_item(
                db=db,
                user_id="user-1",
                media_item=_as_media_item(media_item),
                watched_at=datetime(2026, 7, 18, 10, 0, tzinfo=timezone.utc),
            )
        )

    assert isinstance(result, WatchlistItem)
    assert result.user_id == "user-1"
    assert result.media_item_id == media_item.id
    assert result.type == "tv"
    assert result.status == "added"
    assert result.source == "auto_from_history"
    db.add.assert_called_once()
    db.flush.assert_awaited_once()
    log_event.assert_awaited_once_with(
        db,
        "user-1",
        media_item.id,
        "watchlist_added",
        {"source": "auto_from_history"},
    )
    evaluate.assert_awaited_once_with(db, "user-1", result, media_item)
