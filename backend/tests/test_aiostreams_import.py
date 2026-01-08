import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.append(str(PROJECT_ROOT / "src"))

from librarysync.connectors.metadata.base import MediaCandidate  # noqa: E402
from librarysync.jobs import aiostreams_import  # noqa: E402


class TestAIOStreamsLookup(unittest.TestCase):
    def _build_entry(self, title: str, year: int | None) -> aiostreams_import.ParsedEntry:
        now = datetime.now(timezone.utc)
        return aiostreams_import.ParsedEntry(
            raw={},
            watched_at=now,
            last_seen=now,
            duration_seconds=3600,
            media_type="movie",
            imdb_id=None,
            tmdb_id=None,
            tvdb_id=None,
            season_number=None,
            episode_number=None,
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
        selected = aiostreams_import._select_candidate_for_entry(entry, candidates)
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
        selected = aiostreams_import._select_candidate_for_entry(entry, candidates)
        self.assertIsNotNone(selected)
        self.assertEqual(selected.provider_id, "tt0000002")


if __name__ == "__main__":
    unittest.main()
