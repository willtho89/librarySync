import asyncio
import copy
import unittest
from types import SimpleNamespace

from fastapi import HTTPException
from librarysync.api import (
    routes_stremio_addon,
    routes_stremio_addon_public,
)
from librarysync.core.stremio_addon import build_default_catalogs
from librarysync.db.models import MediaItem


class TestStremioAddonCatalogs(unittest.TestCase):
    def test_merge_catalog_updates_does_not_mutate_existing(self) -> None:
        existing = build_default_catalogs()
        original = copy.deepcopy(existing)
        update = routes_stremio_addon.StremioCatalogUpdate(
            id="watchlist_movies",
            enabled=False,
            ordering=routes_stremio_addon.StremioCatalogOrdering(order_by="title"),
        )
        merged = routes_stremio_addon._merge_catalog_updates(existing, [update])

        self.assertEqual(existing, original)
        movie_catalog = next(catalog for catalog in merged if catalog["id"] == "watchlist_movies")
        self.assertFalse(movie_catalog["enabled"])
        self.assertEqual(movie_catalog["ordering"]["order_by"], "title")

    def test_build_manifest_includes_custom_and_enabled_catalogs(self) -> None:
        catalogs = build_default_catalogs()
        custom_catalog = SimpleNamespace(
            name="Curated Picks", slug="curated_picks", media_type="movie"
        )

        manifest = routes_stremio_addon_public._build_manifest(catalogs, [custom_catalog])

        catalog_ids = {catalog["id"] for catalog in manifest["catalogs"]}
        self.assertIn("watchlist_movies", catalog_ids)
        self.assertIn("watchlist_shows", catalog_ids)
        self.assertIn("in_progress_shows", catalog_ids)
        self.assertIn("curated_picks", catalog_ids)
        self.assertNotIn("watchlist_anime", catalog_ids)
        for catalog in manifest["catalogs"]:
            self.assertIn("extraSupported", catalog)

    def test_build_meta_prefers_stremio_id(self) -> None:
        media_item = MediaItem(
            title="Example Movie",
            media_type="movie",
            imdb_id="tt1234567",
            raw={"stremio_id": "stremio:movie:123"},
        )
        meta = routes_stremio_addon_public._build_meta(media_item, "movie")

        self.assertIsNotNone(meta)
        self.assertEqual(meta["id"], "stremio:movie:123")
        self.assertEqual(meta["type"], "movie")

    def test_in_progress_query_applies_status_filter(self) -> None:
        catalog = {"filters": {"statuses": ["added"]}}
        query = asyncio.run(
            routes_stremio_addon_public._build_in_progress_query("user-id", catalog, None)
        )

        compiled = str(query.compile(compile_kwargs={"literal_binds": True})).lower()
        self.assertIn("watchlist_items.status in", compiled)

    def test_slugify_normalizes(self) -> None:
        self.assertEqual(routes_stremio_addon._slugify("Curated Picks!"), "curated-picks")

    def test_slugify_falls_back_for_empty(self) -> None:
        self.assertEqual(routes_stremio_addon._slugify("  "), "catalog")

    def test_reorder_map_requires_all_items(self) -> None:
        with self.assertRaises(HTTPException):
            routes_stremio_addon._build_reorder_map(["a", "b"], ["a"])


if __name__ == "__main__":
    unittest.main()
