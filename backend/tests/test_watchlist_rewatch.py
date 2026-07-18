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
    def __init__(self, *, row=None, scalar=None, rows=None):
        self._row = row
        self._scalar = scalar
        self._rows = rows or []

    def first(self):
        return self._row

    def scalar(self):
        return self._scalar

    def all(self):
        return self._rows

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


def test_set_watchlist_rewatch_request_skips_dropped_item(monkeypatch) -> None:
    monkeypatch.setattr(watchlist, "log_watchlist_event", _noop)

    item = SimpleNamespace(
        status="dropped",
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
        )
    )

    assert changed is False
    assert item.rewatch_requested is False


def test_resolve_existing_dropped_item_restores_to_added_without_auto_evaluation(
    monkeypatch,
) -> None:
    existing = SimpleNamespace(
        status="dropped",
        source="manual",
        updated_at=None,
    )
    media_item = SimpleNamespace(id="media-1", first_air_date=None)
    db = SimpleNamespace(execute=AsyncMock())

    log_mock = AsyncMock()
    evaluate_mock = AsyncMock()
    enqueue_mock = AsyncMock()
    monkeypatch.setattr(watchlist, "log_watchlist_event", log_mock)
    monkeypatch.setattr(watchlist, "evaluate_show_watchlist_status", evaluate_mock)
    monkeypatch.setattr(watchlist, "_enqueue_watchlist_sync", enqueue_mock)

    restored_item, restored_status = asyncio.run(
        watchlist._resolve_existing_watchlist_item(
            db,
            existing,
            user_id="user-1",
            media_item=media_item,
            media_type="tv",
            source="manual",
            now=datetime(2026, 4, 17, tzinfo=timezone.utc),
            event_raw={},
            enqueue_sync=False,
        )
    )

    assert restored_item is existing
    assert restored_status == "restored"
    assert existing.status == "added"
    evaluate_mock.assert_not_awaited()


def test_drop_watchlist_item_updates_status_and_enqueues_removal(monkeypatch) -> None:
    item = SimpleNamespace(
        id="wl-1",
        user_id="user-1",
        media_item_id="media-1",
        status="added",
        rewatch_requested=False,
    )
    media_item = SimpleNamespace(id="media-1", media_type="tv")
    db = SimpleNamespace(
        execute=AsyncMock(return_value=_FakeResult(row=(item, media_item))),
        commit=AsyncMock(),
    )
    current_user = SimpleNamespace(id="user-1")

    clear_mock = AsyncMock(return_value=False)
    log_mock = AsyncMock()
    removal_mock = AsyncMock()
    monkeypatch.setattr(routes_watchlist, "clear_watchlist_rewatch_request", clear_mock)
    monkeypatch.setattr(routes_watchlist, "log_watchlist_event", log_mock)
    monkeypatch.setattr(routes_watchlist, "enqueue_personal_watchlist_removal", removal_mock)

    response = asyncio.run(routes_watchlist.drop_watchlist_item("wl-1", current_user, db))

    assert response == {"id": "wl-1", "status": "updated"}
    assert item.status == "dropped"
    clear_mock.assert_awaited_once()
    removal_mock.assert_awaited_once_with(db, item, media_item)
    db.commit.assert_awaited_once()


def test_drop_watchlist_item_rejects_movie(monkeypatch) -> None:
    item = SimpleNamespace(
        id="wl-1",
        user_id="user-1",
        media_item_id="media-1",
        status="added",
        rewatch_requested=False,
    )
    media_item = SimpleNamespace(id="media-1", media_type="movie")
    db = SimpleNamespace(
        execute=AsyncMock(return_value=_FakeResult(row=(item, media_item))),
        commit=AsyncMock(),
    )
    current_user = SimpleNamespace(id="user-1")

    clear_mock = AsyncMock(return_value=False)
    log_mock = AsyncMock()
    removal_mock = AsyncMock()
    monkeypatch.setattr(routes_watchlist, "clear_watchlist_rewatch_request", clear_mock)
    monkeypatch.setattr(routes_watchlist, "log_watchlist_event", log_mock)
    monkeypatch.setattr(routes_watchlist, "enqueue_personal_watchlist_removal", removal_mock)

    with pytest.raises(HTTPException, match="Drop is only supported for TV/anime watchlist items") as exc:
        asyncio.run(routes_watchlist.drop_watchlist_item("wl-1", current_user, db))

    assert exc.value.status_code == 400
    assert item.status == "added"
    clear_mock.assert_not_awaited()
    log_mock.assert_not_awaited()
    removal_mock.assert_not_awaited()
    db.commit.assert_not_awaited()


def test_restore_watchlist_item_updates_status_and_enqueues_sync(monkeypatch) -> None:
    item = SimpleNamespace(
        id="wl-1",
        user_id="user-1",
        media_item_id="media-1",
        status="dropped",
    )
    media_item = SimpleNamespace(id="media-1", media_type="movie")
    db = SimpleNamespace(
        execute=AsyncMock(return_value=_FakeResult(row=(item, media_item))),
        commit=AsyncMock(),
    )
    current_user = SimpleNamespace(id="user-1")

    log_mock = AsyncMock()
    sync_mock = AsyncMock()
    monkeypatch.setattr(routes_watchlist, "log_watchlist_event", log_mock)
    monkeypatch.setattr(routes_watchlist, "enqueue_personal_watchlist_sync", sync_mock)

    response = asyncio.run(routes_watchlist.restore_watchlist_item("wl-1", current_user, db))

    assert response == {"id": "wl-1", "status": "updated"}
    assert item.status == "added"
    sync_mock.assert_awaited_once_with(db, item, media_item)
    db.commit.assert_awaited_once()


def test_restore_watchlist_item_evaluates_show_before_enqueuing_sync(monkeypatch) -> None:
    item = SimpleNamespace(
        id="wl-1",
        user_id="user-1",
        media_item_id="media-1",
        status="dropped",
    )
    media_item = SimpleNamespace(id="media-1", media_type="anime", first_air_date=None)
    db = SimpleNamespace(
        execute=AsyncMock(return_value=_FakeResult(row=(item, media_item))),
        commit=AsyncMock(),
    )
    current_user = SimpleNamespace(id="user-1")

    log_mock = AsyncMock()

    async def _set_evaluated_status(_db, _user_id, target_item, _media_item):
        target_item.status = "in_progress"

    evaluate_mock = AsyncMock(side_effect=_set_evaluated_status)

    async def _assert_sync_after_eval(_db, target_item, _media_item):
        assert target_item.status == "in_progress"

    sync_mock = AsyncMock(side_effect=_assert_sync_after_eval)
    monkeypatch.setattr(routes_watchlist, "log_watchlist_event", log_mock)
    monkeypatch.setattr(routes_watchlist, "evaluate_show_watchlist_status", evaluate_mock)
    monkeypatch.setattr(routes_watchlist, "enqueue_personal_watchlist_sync", sync_mock)

    response = asyncio.run(routes_watchlist.restore_watchlist_item("wl-1", current_user, db))

    assert response == {"id": "wl-1", "status": "updated"}
    evaluate_mock.assert_awaited_once_with(db, "user-1", item, media_item)
    sync_mock.assert_awaited_once_with(db, item, media_item)
    db.commit.assert_awaited_once()


def test_list_watchlist_items_excludes_dropped_for_status_all() -> None:
    class _QueryCaptureDB:
        def __init__(self):
            self.queries = []

        async def execute(self, query):
            self.queries.append(query)
            if len(self.queries) == 1:
                return _FakeResult(scalar=0)
            return _FakeResult(rows=[])

        async def commit(self):
            return None

    db = _QueryCaptureDB()
    current_user = SimpleNamespace(id="user-1")

    response = asyncio.run(
        routes_watchlist.list_watchlist_items(
            limit=100,
            offset=0,
            status="all",
            media_type=None,
            search=None,
            source=None,
            rewatch="all",
            order_by="date_added",
            order_dir="desc",
            current_user=current_user,
            db=db,
        )
    )

    assert response["items"] == []
    assert len(db.queries) >= 2
    compiled = db.queries[1].compile()
    query_sql = str(compiled)
    assert "watchlist_items.status !=" in query_sql
    assert "dropped" in compiled.params.values()


def test_list_watchlist_items_includes_dropped_when_explicitly_filtered() -> None:
    class _QueryCaptureDB:
        def __init__(self):
            self.queries = []

        async def execute(self, query):
            self.queries.append(query)
            if len(self.queries) == 1:
                return _FakeResult(scalar=0)
            return _FakeResult(rows=[])

        async def commit(self):
            return None

    db = _QueryCaptureDB()
    current_user = SimpleNamespace(id="user-1")

    response = asyncio.run(
        routes_watchlist.list_watchlist_items(
            limit=100,
            offset=0,
            status="dropped",
            media_type=None,
            search=None,
            source=None,
            rewatch="all",
            order_by="date_added",
            order_dir="desc",
            current_user=current_user,
            db=db,
        )
    )

    assert response["items"] == []
    assert len(db.queries) >= 2
    query_sql = str(db.queries[1])
    assert "watchlist_items.status !=" not in query_sql
