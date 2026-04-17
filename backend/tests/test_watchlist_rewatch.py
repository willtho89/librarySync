import asyncio
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException
from librarysync.api import routes_watchlist
from librarysync.core import watchlist


async def _noop(*args, **kwargs):
    return None


def test_set_watchlist_rewatch_request_updates_item_and_logs(monkeypatch) -> None:
    events = []

    async def fake_log(db, user_id, media_item_id, event_type, raw):
        events.append((user_id, media_item_id, event_type, raw))

    monkeypatch.setattr(watchlist, "log_watchlist_event", fake_log)

    item = SimpleNamespace(
        status="watched",
        rewatch_requested=False,
        rewatch_requested_at=None,
        updated_at=None,
    )

    changed = asyncio.run(
        watchlist.set_watchlist_rewatch_request(
            None,
            item,
            "user-1",
            "media-1",
            enabled=True,
            reason="manual",
            now=datetime(2026, 4, 17, tzinfo=timezone.utc),
        )
    )

    assert changed is True
    assert item.rewatch_requested is True
    assert item.rewatch_requested_at == datetime(2026, 4, 17, tzinfo=timezone.utc)
    assert events == [
        (
            "user-1",
            "media-1",
            "watchlist_rewatch_updated",
            {"enabled": True, "reason": "manual"},
        )
    ]


def test_clear_watchlist_rewatch_request_is_noop_when_not_requested(monkeypatch) -> None:
    monkeypatch.setattr(watchlist, "log_watchlist_event", _noop)

    item = SimpleNamespace(
        status="watched",
        rewatch_requested=False,
        rewatch_requested_at=None,
        updated_at=None,
    )

    changed = asyncio.run(
        watchlist.clear_watchlist_rewatch_request(
            None,
            item,
            "user-1",
            "media-1",
            reason="watched",
        )
    )

    assert changed is False
    assert item.rewatch_requested is False


def test_clear_watchlist_rewatch_request_keeps_flag_for_older_watch(monkeypatch) -> None:
    monkeypatch.setattr(watchlist, "log_watchlist_event", _noop)

    item = SimpleNamespace(
        status="watched",
        rewatch_requested=True,
        rewatch_requested_at=datetime(2026, 4, 17, 12, 0, tzinfo=timezone.utc),
        updated_at=None,
    )

    changed = asyncio.run(
        watchlist.clear_watchlist_rewatch_request(
            None,
            item,
            "user-1",
            "media-1",
            reason="watched",
            watched_at=datetime(2026, 4, 17, 11, 0, tzinfo=timezone.utc),
        )
    )

    assert changed is False
    assert item.rewatch_requested is True


class _FakeScalarResult:
    def __init__(self, value):
        self._value = value

    def first(self):
        return self._value


class _FakeResult:
    def __init__(self, *, row=None, scalar=None):
        self._row = row
        self._scalar = scalar

    def first(self):
        return self._row

    def scalars(self):
        return _FakeScalarResult(self._scalar)


def test_enable_watchlist_rewatch_rejects_unwatched_movie() -> None:
    db = SimpleNamespace(
        execute=AsyncMock(
            side_effect=[
                _FakeResult(
                    row=(
                        SimpleNamespace(
                            id="wl-1",
                            user_id="user-1",
                            media_item_id="media-1",
                            status="added",
                        ),
                        SimpleNamespace(id="media-1", media_type="movie"),
                    )
                ),
                _FakeResult(scalar=None),
            ]
        ),
        commit=AsyncMock(),
    )
    current_user = SimpleNamespace(id="user-1")

    with pytest.raises(HTTPException, match="Only watched items can be queued for rewatch"):
        asyncio.run(routes_watchlist.enable_watchlist_rewatch("wl-1", current_user, db))
