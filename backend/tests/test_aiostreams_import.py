import asyncio
import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.append(str(PROJECT_ROOT / "src"))

from librarysync.connectors.metadata.base import MediaCandidate  # noqa: E402
from librarysync.jobs import aiostreams_import  # noqa: E402


class TestAIOStreamsLookup(unittest.TestCase):
    def _build_entry(
        self,
        title: str,
        year: int | None,
        media_type: str = "movie",
        season_number: int | None = None,
        episode_number: int | None = None,
    ) -> aiostreams_import.ParsedEntry:
        now = datetime.now(timezone.utc)
        return aiostreams_import.ParsedEntry(
            raw={},
            watched_at=now,
            last_seen=now,
            duration_seconds=3600,
            media_type=media_type,
            imdb_id=None,
            tmdb_id=None,
            tvdb_id=None,
            season_number=season_number,
            episode_number=episode_number,
            title=title,
            year=year,
            filename=None,
            url=None,
            request_id=None,
            entry_key="key",
        )

    def test_select_candidate_prefers_title_and_year(self) -> None:
        entry = self._build_entry("Example Movie", 2020)
        candidates = [
            MediaCandidate(
                provider="tmdb",
                provider_id="100",
                media_type="movie",
                title="Example Movie",
                year=2019,
                poster_url=None,
                imdb_id=None,
                raw={},
            ),
            MediaCandidate(
                provider="tmdb",
                provider_id="200",
                media_type="movie",
                title="Example Movie",
                year=2020,
                poster_url=None,
                imdb_id=None,
                raw={},
            ),
        ]
        # Create a mock db session
        db = AsyncMock()
        db.execute = AsyncMock(return_value=MagicMock(all=MagicMock(return_value=[])))
        
        selected = asyncio.run(
            aiostreams_import._select_candidate_for_entry(db, "test_user", entry, candidates)
        )
        self.assertIsNotNone(selected)
        self.assertEqual(selected.provider_id, "200")

    def test_select_candidate_uses_title_match(self) -> None:
        entry = self._build_entry("The.Show", None)
        candidates = [
            MediaCandidate(
                provider="imdb",
                provider_id="tt0000001",
                media_type="movie",
                title="Other Show",
                year=None,
                poster_url=None,
                imdb_id="tt0000001",
                raw={},
            ),
            MediaCandidate(
                provider="imdb",
                provider_id="tt0000002",
                media_type="movie",
                title="The Show",
                year=None,
                poster_url=None,
                imdb_id="tt0000002",
                raw={},
            ),
        ]
        # Create a mock db session
        db = AsyncMock()
        db.execute = AsyncMock(return_value=MagicMock(all=MagicMock(return_value=[])))
        
        selected = asyncio.run(
            aiostreams_import._select_candidate_for_entry(db, "test_user", entry, candidates)
        )
        self.assertIsNotNone(selected)
        self.assertEqual(selected.provider_id, "tt0000002")

    def test_series_continuity_prefers_previously_watched_show(self) -> None:
        """Test that if user watched Fallout S02E07, watching S02E08 prefers the same show."""
        entry = self._build_entry(
            title="Fallout",
            year=None,
            media_type="tv",
            season_number=2,
            episode_number=8,
        )
        
        # Two candidates: one is the correct Fallout TV show, another is an anime
        fallout_tv_candidate = MediaCandidate(
            provider="tmdb",
            provider_id="12345",
            media_type="tv",
            title="Fallout",
            year=2024,
            poster_url=None,
            imdb_id="tt12345678",
            raw={},
        )
        fallout_anime_candidate = MediaCandidate(
            provider="tmdb",
            provider_id="99999",
            media_type="tv",
            title="Fallout",
            year=2008,
            poster_url=None,
            imdb_id="tt99999999",
            raw={},
        )
        
        candidates = [
            fallout_anime_candidate,  # Wrong one first in list
            fallout_tv_candidate,     # Correct one second
        ]
        
        # Mock database to return a show with matching imdb_id that user has watched before
        mock_result = MagicMock()
        
        # Create mock MediaItem for the TV show the user has watched
        mock_media_item = MagicMock()
        mock_media_item.id = "show_123"
        mock_media_item.media_type = "tv"
        mock_media_item.title = "Fallout"
        mock_media_item.imdb_id = "tt12345678"
        mock_media_item.tmdb_id = None
        mock_media_item.tvdb_id = None
        
        # User has watched S02E07 (episode 7 before current episode 8)
        mock_result.all = MagicMock(return_value=[
            (mock_media_item, 2, 7)  # (MediaItem, max_season, max_episode)
        ])
        
        db = AsyncMock()
        db.execute = AsyncMock(return_value=mock_result)
        
        selected = asyncio.run(
            aiostreams_import._select_candidate_for_entry(db, "test_user", entry, candidates)
        )
        
        # Should select the TV show, not the anime, because user watched previous episode
        self.assertIsNotNone(selected)
        self.assertEqual(selected.imdb_id, "tt12345678")
        self.assertEqual(selected.year, 2024)


if __name__ == "__main__":
    unittest.main()
