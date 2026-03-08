"""Tests for episode metadata refresh behavior.

Covers:
- _needs_media_enrichment detects missing episode title
- _apply_episode_metadata with force=True overwrites existing title/air_date
- refresh_episode_metadata public function
"""
import asyncio
import sys
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.append(str(PROJECT_ROOT / "src"))

from librarysync.connectors.metadata.base import EpisodeSummary  # noqa: E402
from librarysync.core import metadata_enrichment  # noqa: E402
from librarysync.core.metadata_enrichment import (  # noqa: E402
    _apply_episode_metadata,
    _needs_media_enrichment,
    refresh_episode_metadata,
)


def _make_media_item(**kwargs):
    m = MagicMock()
    m.imdb_id = kwargs.get("imdb_id", "tt1234567")
    m.tmdb_id = kwargs.get("tmdb_id", "12345")
    m.tvdb_id = kwargs.get("tvdb_id", "67890")
    m.media_type = kwargs.get("media_type", "tv")
    m.poster_url = kwargs.get("poster_url", "https://image.tmdb.org/poster.jpg")
    m.year = kwargs.get("year", 2024)
    m.overview = kwargs.get("overview", "A great show")
    m.genres = kwargs.get("genres", ["drama"])
    m.runtime_in_seconds = kwargs.get("runtime_in_seconds", 3600)
    m.first_air_date = kwargs.get("first_air_date", "2024-01-01")
    m.last_air_date = kwargs.get("last_air_date", "2024-12-01")
    m.release_date = kwargs.get("release_date", None)
    m.raw = kwargs.get("raw", {"source": "tmdb"})
    return m


def _make_episode_item(**kwargs):
    e = MagicMock()
    e.tmdb_id = kwargs.get("tmdb_id", "99999")
    e.title = kwargs.get("title", None)
    e.season_number = kwargs.get("season_number", 1)
    e.episode_number = kwargs.get("episode_number", 1)
    e.air_date = kwargs.get("air_date", None)
    return e


class TestNeedsMediaEnrichmentEpisodeTitle(unittest.TestCase):
    def _base_media_item(self):
        return _make_media_item()

    def test_episode_missing_title_triggers_enrichment(self) -> None:
        media_item = self._base_media_item()
        episode_item = _make_episode_item(tmdb_id="99999", title=None)
        self.assertTrue(_needs_media_enrichment(media_item, episode_item))

    def test_episode_with_title_does_not_trigger_via_episode_check(self) -> None:
        media_item = self._base_media_item()
        episode_item = _make_episode_item(tmdb_id="99999", title="Pilot")
        # All show-level metadata is complete, episode has title → no enrichment needed
        self.assertFalse(_needs_media_enrichment(media_item, episode_item))

    def test_episode_missing_title_and_tmdb_id_triggers_enrichment(self) -> None:
        media_item = self._base_media_item()
        episode_item = _make_episode_item(tmdb_id=None, title=None)
        self.assertTrue(_needs_media_enrichment(media_item, episode_item))

    def test_no_episode_item_does_not_require_episode_check(self) -> None:
        media_item = self._base_media_item()
        self.assertFalse(_needs_media_enrichment(media_item, None))

    def test_episode_needs_title_requires_show_tmdb_id(self) -> None:
        media_item = _make_media_item(tmdb_id=None)
        episode_item = _make_episode_item(tmdb_id="99999", title=None)
        # show is missing tmdb_id → enrichment triggered (via missing_ids), not episode_needs_title
        self.assertTrue(_needs_media_enrichment(media_item, episode_item))

    def test_episode_missing_title_but_no_show_tmdb_id_still_triggers(self) -> None:
        """If show lacks tmdb_id and episode lacks title, enrichment runs (for show IDs)."""
        media_item = _make_media_item(tmdb_id=None, imdb_id=None, tvdb_id=None)
        episode_item = _make_episode_item(title=None)
        self.assertTrue(_needs_media_enrichment(media_item, episode_item))


class TestApplyEpisodeMetadataForce(unittest.TestCase):
    def _make_provider(self, episodes):
        provider = MagicMock()
        provider.provider = "tmdb"
        provider.list_episodes = AsyncMock(return_value=episodes)
        return provider

    def test_force_overwrites_existing_title(self) -> None:
        provider = self._make_provider([
            EpisodeSummary(
                episode_number=1,
                title="Real Episode Title",
                provider_id="99999",
                air_date="2024-03-01",
                still_url=None,
            )
        ])
        media_item = _make_media_item(media_type="tv", tmdb_id="12345")
        episode_item = _make_episode_item(
            tmdb_id="99999",
            title="Old Title",
            episode_number=1,
            season_number=1,
        )

        asyncio.run(_apply_episode_metadata(provider, media_item, episode_item, force=True))

        self.assertEqual(episode_item.title, "Real Episode Title")

    def test_no_force_skips_when_title_exists(self) -> None:
        provider = self._make_provider([
            EpisodeSummary(
                episode_number=1,
                title="Real Episode Title",
                provider_id="99999",
                air_date="2024-03-01",
                still_url=None,
            )
        ])
        media_item = _make_media_item(media_type="tv", tmdb_id="12345")
        episode_item = _make_episode_item(
            tmdb_id="99999",
            title="Old Title",
            episode_number=1,
            season_number=1,
        )

        asyncio.run(_apply_episode_metadata(provider, media_item, episode_item, force=False))

        # Should NOT overwrite existing title when force=False
        provider.list_episodes.assert_not_awaited()
        self.assertEqual(episode_item.title, "Old Title")

    def test_no_force_populates_missing_title(self) -> None:
        provider = self._make_provider([
            EpisodeSummary(
                episode_number=1,
                title="New Title",
                provider_id="99999",
                air_date="2024-03-01",
                still_url=None,
            )
        ])
        media_item = _make_media_item(media_type="tv", tmdb_id="12345")
        episode_item = _make_episode_item(
            tmdb_id=None,
            title=None,
            episode_number=1,
            season_number=1,
        )

        asyncio.run(_apply_episode_metadata(provider, media_item, episode_item, force=False))

        self.assertEqual(episode_item.title, "New Title")
        self.assertEqual(episode_item.tmdb_id, "99999")

    def test_force_overwrites_air_date(self) -> None:
        from datetime import date

        provider = self._make_provider([
            EpisodeSummary(
                episode_number=1,
                title="Title",
                provider_id="99999",
                air_date="2024-06-15",
                still_url=None,
            )
        ])
        media_item = _make_media_item(media_type="tv", tmdb_id="12345")
        episode_item = _make_episode_item(
            tmdb_id="99999",
            title="Title",
            air_date=date(2024, 1, 1),
            episode_number=1,
            season_number=1,
        )

        asyncio.run(_apply_episode_metadata(provider, media_item, episode_item, force=True))

        self.assertEqual(episode_item.air_date, date(2024, 6, 15))

    def test_skips_for_movie_media_type(self) -> None:
        provider = self._make_provider([])
        media_item = _make_media_item(media_type="movie", tmdb_id="12345")
        episode_item = _make_episode_item(title=None)

        asyncio.run(_apply_episode_metadata(provider, media_item, episode_item, force=True))

        provider.list_episodes.assert_not_awaited()

    def test_skips_when_no_show_tmdb_id(self) -> None:
        provider = self._make_provider([])
        media_item = _make_media_item(media_type="tv", tmdb_id=None)
        episode_item = _make_episode_item(title=None)

        asyncio.run(_apply_episode_metadata(provider, media_item, episode_item, force=True))

        provider.list_episodes.assert_not_awaited()


class TestRefreshEpisodeMetadata(unittest.TestCase):
    def test_returns_false_for_movie(self) -> None:
        db = AsyncMock()
        media_item = _make_media_item(media_type="movie")
        episode_item = _make_episode_item(title=None)

        result = asyncio.run(refresh_episode_metadata(db, "user-1", media_item, episode_item))

        self.assertFalse(result)

    def test_returns_false_when_no_show_tmdb_id(self) -> None:
        db = AsyncMock()
        media_item = _make_media_item(media_type="tv", tmdb_id=None)
        episode_item = _make_episode_item(title=None)

        result = asyncio.run(refresh_episode_metadata(db, "user-1", media_item, episode_item))

        self.assertFalse(result)

    def test_returns_false_when_no_tmdb_provider(self) -> None:
        db = AsyncMock()
        media_item = _make_media_item(media_type="tv", tmdb_id="12345")
        episode_item = _make_episode_item(title=None)

        with patch(
            "librarysync.core.metadata_enrichment.MetadataProviderService"
        ) as mock_service_cls:
            mock_service = MagicMock()
            mock_service.load_provider = AsyncMock(return_value=None)
            mock_service_cls.return_value = mock_service

            result = asyncio.run(refresh_episode_metadata(db, "user-1", media_item, episode_item))

        self.assertFalse(result)

    def test_updates_title_via_provider(self) -> None:
        from librarysync.connectors.metadata.base import EpisodeMetadataProvider

        db = AsyncMock()
        media_item = _make_media_item(media_type="tv", tmdb_id="12345")
        episode_item = _make_episode_item(
            tmdb_id="99999", title=None, episode_number=1, season_number=1
        )

        mock_provider = MagicMock(spec=EpisodeMetadataProvider)
        mock_provider.provider = "tmdb"
        mock_provider.list_episodes = AsyncMock(
            return_value=[
                EpisodeSummary(
                    episode_number=1,
                    title="Updated Title",
                    provider_id="99999",
                    air_date="2024-05-01",
                    still_url=None,
                )
            ]
        )

        with patch(
            "librarysync.core.metadata_enrichment.MetadataProviderService"
        ) as mock_service_cls:
            mock_service = MagicMock()
            mock_service.load_provider = AsyncMock(return_value=mock_provider)
            mock_service_cls.return_value = mock_service

            result = asyncio.run(refresh_episode_metadata(db, "user-1", media_item, episode_item))

        self.assertTrue(result)
        self.assertEqual(episode_item.title, "Updated Title")

    def test_uses_provider_override(self) -> None:
        from librarysync.connectors.metadata.base import EpisodeMetadataProvider

        db = AsyncMock()
        media_item = _make_media_item(media_type="tv", tmdb_id="12345")
        episode_item = _make_episode_item(
            tmdb_id="99999", title="Old Title", episode_number=1, season_number=1
        )

        mock_provider = MagicMock(spec=EpisodeMetadataProvider)
        mock_provider.provider = "tmdb"
        mock_provider.list_episodes = AsyncMock(
            return_value=[
                EpisodeSummary(
                    episode_number=1,
                    title="Override Title",
                    provider_id="99999",
                    air_date=None,
                    still_url=None,
                )
            ]
        )

        result = asyncio.run(
            refresh_episode_metadata(
                db, "user-1", media_item, episode_item, provider_overrides={"tmdb": mock_provider}
            )
        )

        self.assertTrue(result)
        self.assertEqual(episode_item.title, "Override Title")


if __name__ == "__main__":
    unittest.main()
