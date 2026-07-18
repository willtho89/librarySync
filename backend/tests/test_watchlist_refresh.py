import asyncio
from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock, patch

from librarysync.jobs import watchlist_refresh


def _as(value: Any) -> Any:
    return cast(Any, value)


class _FakeScalars:
    def __init__(self, values):
        self._values = values

    def all(self):
        return self._values


class _FakeResult:
    def __init__(self, values=()):
        self._values = list(values)

    def scalars(self):
        return _FakeScalars(self._values)

    def all(self):
        return list(self._values)


def _make_media(media_id: str, media_type: str) -> SimpleNamespace:
    return SimpleNamespace(
        id=media_id,
        media_type=media_type,
        imdb_id="tt1234567",
        tmdb_id="1399",
    )


def test_refresh_dispatches_anime_to_show_status_evaluation() -> None:
    movie_item = SimpleNamespace(status="added")
    tv_item = SimpleNamespace(status="added")
    anime_item = SimpleNamespace(status="added")
    movie = _make_media("media-movie", "movie")
    tv = _make_media("media-tv", "tv")
    anime = _make_media("media-anime", "anime")
    rows = [(movie_item, movie), (tv_item, tv), (anime_item, anime)]

    db = SimpleNamespace(
        execute=AsyncMock(return_value=_FakeResult(["user-1"])),
        commit=AsyncMock(),
    )
    job = SimpleNamespace(last_run_at=None)

    with (
        patch.object(
            watchlist_refresh, "_load_watchlist_rows_for_refresh", new_callable=AsyncMock
        ) as load_rows,
        patch.object(
            watchlist_refresh, "_backfill_missing_show_episodes", new_callable=AsyncMock
        ),
        patch.object(
            watchlist_refresh, "evaluate_show_watchlist_status", new_callable=AsyncMock
        ) as evaluate,
        patch.object(watchlist_refresh, "_refresh_movie_status", new_callable=AsyncMock) as refresh_movie,
        patch.object(watchlist_refresh, "extend_scheduled_job", new_callable=AsyncMock),
    ):
        load_rows.return_value = rows
        asyncio.run(watchlist_refresh.run_watchlist_refresh(db, _as(job)))

    refresh_movie.assert_awaited_once_with(db, "user-1", movie_item, movie)
    assert evaluate.await_count == 2
    evaluated_media = [call.args[3] for call in evaluate.await_args_list]
    assert evaluated_media == [tv, anime]


def test_backfill_missing_show_episodes_covers_tv_and_anime() -> None:
    tv_item = SimpleNamespace(status="added")
    anime_item = SimpleNamespace(status="added")
    movie_item = SimpleNamespace(status="added")
    tv = _make_media("media-tv", "tv")
    anime = _make_media("media-anime", "anime")
    movie = _make_media("media-movie", "movie")
    rows = [(tv_item, tv), (anime_item, anime), (movie_item, movie)]

    db = SimpleNamespace(execute=AsyncMock(return_value=_FakeResult([])))

    with patch.object(watchlist_refresh, "backfill_show_episodes", new_callable=AsyncMock) as backfill:
        asyncio.run(watchlist_refresh._backfill_missing_show_episodes(db, "user-1", _as(rows)))

    assert backfill.await_count == 2
    backfilled_media = [call.args[2] for call in backfill.await_args_list]
    assert backfilled_media == [tv, anime]


def test_load_rows_episode_queries_treat_anime_like_tv() -> None:
    execute = AsyncMock(return_value=_FakeResult([]))
    db = SimpleNamespace(execute=execute)
    last_run_at = datetime(2026, 7, 17, 10, 0, tzinfo=timezone.utc)

    result = asyncio.run(watchlist_refresh._load_watchlist_rows_for_refresh(db, "user-1", last_run_at))

    assert result == []
    compiled_queries = [
        str(call.args[0].compile(compile_kwargs={"literal_binds": True})).lower()
        for call in execute.await_args_list
    ]
    assert compiled_queries
    for compiled in compiled_queries:
        assert "media_items.media_type = 'tv'" not in compiled
    episode_queries = [compiled for compiled in compiled_queries if "episode_items" in compiled]
    assert episode_queries
    for compiled in episode_queries:
        assert "media_items.media_type in ('tv', 'anime')" in compiled
