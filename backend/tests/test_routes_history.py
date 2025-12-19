import sys
import unittest
from pathlib import Path

from fastapi import HTTPException

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.append(str(PROJECT_ROOT / "src"))

from librarysync.api import routes_history  # noqa: E402


class TestRoutesHistoryHelpers(unittest.TestCase):
    def test_normalize_episode_number_requires_value(self) -> None:
        with self.assertRaises(HTTPException):
            routes_history._normalize_episode_number(None, "season")

    def test_normalize_episode_number_rejects_negative(self) -> None:
        with self.assertRaises(HTTPException):
            routes_history._normalize_episode_number(-1, "episode")

    def test_normalize_episode_number_allows_zero(self) -> None:
        value = routes_history._normalize_episode_number(0, "season")
        self.assertEqual(value, 0)

    def test_has_episode_fields_detects_values(self) -> None:
        payload = routes_history.WatchedItemCreateIn(episode_title="Pilot")
        self.assertTrue(routes_history._has_episode_fields(payload))

    def test_extract_episode_ids_normalizes_imdb(self) -> None:
        payload = routes_history.WatchedItemCreateIn(episode_imdb_id="TT1234567")
        ids = routes_history._extract_episode_ids(payload)
        self.assertEqual(ids.get("imdb_id"), "tt1234567")

    def test_extract_media_ids_normalizes_imdb(self) -> None:
        payload = routes_history.WatchedItemCreateIn(imdb_id="TT7654321")
        ids = routes_history._extract_media_ids(payload)
        self.assertEqual(ids.get("imdb_id"), "tt7654321")


if __name__ == "__main__":
    unittest.main()
