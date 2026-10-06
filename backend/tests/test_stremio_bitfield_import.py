import asyncio
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from librarysync.jobs import stremio_import
from librarysync.jobs.stremio_import import ShowSummary

NOW = datetime(2026, 10, 6, 12, 0, tzinfo=timezone.utc)


def test_series_bitfield_candidates_keep_their_own_episode() -> None:
    show = ShowSummary(
        title="Example Show",
        year=2024,
        imdb_id="tt1234567",
        tmdb_id=None,
        tvdb_id=None,
        stremio_id=None,
        poster_url=None,
        raw={},
    )
    video_ids = ["tt1234567:1:1", "tt1234567:1:2", "tt1234567:1:3"]
    show_item = SimpleNamespace(id="show-1")

    async def _episode_item(_db, _show_item, episode):
        return SimpleNamespace(id=f"ep-{episode.season_number}-{episode.episode_number}")

    captured: list = []

    async def _capture(_db, _user_id, _provider, candidates, **_kwargs):
        captured.extend(candidates)
        return len(candidates)

    with (
        patch.object(stremio_import, "fetch_cinemeta_video_ids", AsyncMock(return_value=video_ids)),
        patch.object(
            stremio_import,
            "watched_bitfield_from_string",
            return_value=SimpleNamespace(get_video=lambda _vid: True),
        ),
        patch.object(stremio_import, "_get_or_create_show_item", AsyncMock(return_value=show_item)),
        patch.object(stremio_import, "_load_existing_stremio_sync_ids", AsyncMock(return_value=set())),
        patch.object(stremio_import, "_load_existing_episode_watches", AsyncMock(return_value={})),
        patch.object(stremio_import, "_load_stremio_syncs", AsyncMock(return_value={})),
        patch.object(stremio_import, "_get_or_create_episode_item", side_effect=_episode_item),
        patch.object(stremio_import, "process_import_candidates", side_effect=_capture),
    ):
        imported = asyncio.run(
            stremio_import._import_series_bitfield(
                None,
                "user-1",
                {},
                {"watched": "tt1234567:1:3:3:eJyLBgAAawB1"},
                show,
                "tt1234567",
                NOW,
            )
        )

    assert imported is True
    assert len(captured) == 3

    async def _resolve():
        return [await candidate.build_items(None) for candidate in captured]

    items = asyncio.run(_resolve())
    assert [item.episode_item.id for item in items] == ["ep-1-1", "ep-1-2", "ep-1-3"]
    assert all(item.show_item is show_item for item in items)
