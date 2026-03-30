"""Tests for direct episode metadata enrichment in process_new_item_job.

Covers:
- process_new_item_job calls refresh_episode_metadata directly for episode watches
- process_new_item_job skips refresh_episode_metadata when no episode_item
"""

import asyncio
import sys
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.append(str(PROJECT_ROOT / "src"))

from librarysync.core.watch_pipeline import process_new_item_job  # noqa: E402


def _make_watched(episode_item_id: str | None = None, media_item_id: str | None = None):
    watched = MagicMock()
    watched.episode_item_id = episode_item_id
    watched.media_item_id = media_item_id
    watched.user_id = "user-1"
    return watched


def _make_media_item(media_type: str = "tv", tmdb_id: str = "12345"):
    m = MagicMock()
    m.id = "media-1"
    m.media_type = media_type
    m.tmdb_id = tmdb_id
    return m


def _make_episode_item(title: str | None = None):
    e = MagicMock()
    e.id = "episode-1"
    e.show_media_item_id = "media-1"
    e.title = title
    e.season_number = 1
    e.episode_number = 1
    return e


def _make_job(watched_id: str = "watched-1", is_rewatch: bool = False):
    job = MagicMock()
    job.payload = {"watched_item_id": watched_id, "is_rewatch": is_rewatch}
    return job


class TestProcessNewItemJobEpisodeMetadata(unittest.TestCase):
    def test_refresh_episode_metadata_called_for_episode_watch(self) -> None:
        """refresh_episode_metadata must be called directly when the watched item is an episode."""
        db = AsyncMock()
        media_item = _make_media_item(media_type="tv", tmdb_id="12345")
        episode_item = _make_episode_item(title=None)
        watched = _make_watched(episode_item_id="episode-1")

        async def fake_db_get(model, pk):
            if model.__name__ == "WatchedItem":
                return watched
            if model.__name__ == "EpisodeItem":
                return episode_item
            if model.__name__ == "MediaItem":
                return media_item
            return None

        db.get.side_effect = fake_db_get
        job = _make_job()

        with (
            patch(
                "librarysync.core.watch_pipeline.enrich_watched_metadata",
                new_callable=AsyncMock,
            ) as mock_enrich,
            patch(
                "librarysync.core.watch_pipeline.refresh_episode_metadata",
                new_callable=AsyncMock,
                return_value=True,
            ) as mock_refresh,
            patch(
                "librarysync.core.watch_pipeline.backfill_show_episodes",
                new_callable=AsyncMock,
                return_value=False,
            ),
            patch(
                "librarysync.core.watch_pipeline.check_and_update_watchlist",
                new_callable=AsyncMock,
            ),
            patch(
                "librarysync.core.watch_pipeline._sync_to_integrations",
                new_callable=AsyncMock,
            ),
        ):
            asyncio.run(process_new_item_job(db, job))

        mock_enrich.assert_awaited_once_with(db, "user-1", media_item, episode_item)
        mock_refresh.assert_awaited_once_with(db, "user-1", media_item, episode_item)

    def test_refresh_episode_metadata_not_called_for_movie_watch(self) -> None:
        """refresh_episode_metadata must NOT be called when the watched item is a movie."""
        db = AsyncMock()
        media_item = _make_media_item(media_type="movie", tmdb_id="99999")
        watched = _make_watched(media_item_id="media-1")

        async def fake_db_get(model, pk):
            if model.__name__ == "WatchedItem":
                return watched
            if model.__name__ == "MediaItem":
                return media_item
            return None

        db.get.side_effect = fake_db_get
        job = _make_job()

        with (
            patch(
                "librarysync.core.watch_pipeline.enrich_watched_metadata",
                new_callable=AsyncMock,
            ),
            patch(
                "librarysync.core.watch_pipeline.refresh_episode_metadata",
                new_callable=AsyncMock,
                return_value=False,
            ) as mock_refresh,
            patch(
                "librarysync.core.watch_pipeline.backfill_show_episodes",
                new_callable=AsyncMock,
                return_value=False,
            ),
            patch(
                "librarysync.core.watch_pipeline.check_and_update_watchlist",
                new_callable=AsyncMock,
            ),
            patch(
                "librarysync.core.watch_pipeline._sync_to_integrations",
                new_callable=AsyncMock,
            ),
        ):
            asyncio.run(process_new_item_job(db, job))

        mock_refresh.assert_not_awaited()

    def test_refresh_episode_metadata_called_even_when_episode_has_title(self) -> None:
        """refresh_episode_metadata is called regardless of whether title is already set,
        so that tmdb_id and air_date can also be updated if needed."""
        db = AsyncMock()
        media_item = _make_media_item(media_type="tv", tmdb_id="12345")
        episode_item = _make_episode_item(title="Existing Title")
        watched = _make_watched(episode_item_id="episode-1")

        async def fake_db_get(model, pk):
            if model.__name__ == "WatchedItem":
                return watched
            if model.__name__ == "EpisodeItem":
                return episode_item
            if model.__name__ == "MediaItem":
                return media_item
            return None

        db.get.side_effect = fake_db_get
        job = _make_job()

        with (
            patch(
                "librarysync.core.watch_pipeline.enrich_watched_metadata",
                new_callable=AsyncMock,
            ),
            patch(
                "librarysync.core.watch_pipeline.refresh_episode_metadata",
                new_callable=AsyncMock,
                return_value=False,
            ) as mock_refresh,
            patch(
                "librarysync.core.watch_pipeline.backfill_show_episodes",
                new_callable=AsyncMock,
                return_value=False,
            ),
            patch(
                "librarysync.core.watch_pipeline.check_and_update_watchlist",
                new_callable=AsyncMock,
            ),
            patch(
                "librarysync.core.watch_pipeline._sync_to_integrations",
                new_callable=AsyncMock,
            ),
        ):
            asyncio.run(process_new_item_job(db, job))

        mock_refresh.assert_awaited_once_with(db, "user-1", media_item, episode_item)


if __name__ == "__main__":
    unittest.main()
