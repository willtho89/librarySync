import sys
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.append(str(PROJECT_ROOT / "src"))

from librarysync.api import routes_admin  # noqa: E402


class TestAdminMediaUpdate(unittest.TestCase):
    def test_media_id_update_with_all_fields(self) -> None:
        model = routes_admin.MediaIdUpdate(
            id="test-id",
            imdb="tt1234567",
            tmdb="12345",
            tvdb="67890",
            tvmaze="123",
            kitsu="456",
            myanimelist="789",
            anilist="101112",
        )
        self.assertEqual(model.id, "test-id")
        self.assertEqual(model.imdb, "tt1234567")
        self.assertEqual(model.tmdb, "12345")
        self.assertEqual(model.tvdb, "67890")
        self.assertEqual(model.tvmaze, "123")
        self.assertEqual(model.kitsu, "456")
        self.assertEqual(model.myanimelist, "789")
        self.assertEqual(model.anilist, "101112")

    def test_media_id_update_with_partial_fields(self) -> None:
        model = routes_admin.MediaIdUpdate(
            id="test-id",
            imdb="tt1234567",
            tmdb="12345",
        )
        self.assertEqual(model.id, "test-id")
        self.assertEqual(model.imdb, "tt1234567")
        self.assertEqual(model.tmdb, "12345")
        self.assertIsNone(model.tvdb)
        self.assertIsNone(model.tvmaze)

    def test_media_id_update_with_null_fields(self) -> None:
        model = routes_admin.MediaIdUpdate(
            id="test-id",
            imdb=None,
            tmdb=None,
        )
        self.assertEqual(model.id, "test-id")
        self.assertIsNone(model.imdb)
        self.assertIsNone(model.tmdb)

    def test_media_update_request_with_single_item(self) -> None:
        request = routes_admin.MediaUpdateRequest(
            updates=[
                routes_admin.MediaIdUpdate(
                    id="test-id",
                    tmdb="12345",
                )
            ],
            dry_run=False,
        )
        self.assertEqual(len(request.updates), 1)
        self.assertEqual(request.updates[0].id, "test-id")
        self.assertEqual(request.updates[0].tmdb, "12345")
        self.assertFalse(request.dry_run)

    def test_media_update_request_with_multiple_items(self) -> None:
        request = routes_admin.MediaUpdateRequest(
            updates=[
                routes_admin.MediaIdUpdate(
                    id="test-id-1",
                    tmdb="12345",
                ),
                routes_admin.MediaIdUpdate(
                    id="test-id-2",
                    imdb="tt67890",
                ),
            ],
            dry_run=True,
        )
        self.assertEqual(len(request.updates), 2)
        self.assertEqual(request.updates[0].id, "test-id-1")
        self.assertEqual(request.updates[1].id, "test-id-2")
        self.assertTrue(request.dry_run)

    def test_media_update_request_default_dry_run(self) -> None:
        request = routes_admin.MediaUpdateRequest(
            updates=[
                routes_admin.MediaIdUpdate(
                    id="test-id",
                    tmdb="12345",
                )
            ]
        )
        self.assertFalse(request.dry_run)

    def test_media_id_update_requires_id(self) -> None:
        with self.assertRaises(ValueError):
            routes_admin.MediaIdUpdate(
                tmdb="12345",
            )


if __name__ == "__main__":
    unittest.main()
