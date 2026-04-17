import asyncio
import copy
import unittest
from types import SimpleNamespace

from fastapi import HTTPException
from librarysync.api import (
    routes_stremio_addon,
    routes_stremio_addon_public,
)
from librarysync.core.external_catalog import _discover_tmdb_chart_catalogs
from librarysync.core.stremio_addon import build_default_catalogs
from librarysync.core.watchlist_links import (
    parse_imdb_chart_urls,
    parse_mdblist_urls,
    parse_tmdb_chart_urls,
    parse_tmdb_list_urls,
    parse_tvdb_list_urls,
)
from librarysync.db.models import MediaItem, StremioExternalCatalogItem


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
        external_catalog = SimpleNamespace(
            name="Best Movies",
            slug="best-movies",
            enabled=True,
            source_catalog_type="movie",
            page_size=30,
            show_in_home=True,
        )
        custom_catalog = SimpleNamespace(
            name="Curated Picks", slug="curated_picks", media_type="movie"
        )

        manifest = routes_stremio_addon_public._build_manifest(
            catalogs,
            [external_catalog],
            [custom_catalog],
        )

        catalog_ids = [catalog["id"] for catalog in manifest["catalogs"]]
        self.assertIn("watchlist_movies", catalog_ids)
        self.assertIn("watchlist_shows", catalog_ids)
        self.assertIn("in_progress_shows", catalog_ids)
        self.assertIn("best-movies", catalog_ids)
        self.assertIn("curated_picks", catalog_ids)
        self.assertNotIn("watchlist_anime", catalog_ids)
        self.assertLess(catalog_ids.index("best-movies"), catalog_ids.index("curated_picks"))
        for catalog in manifest["catalogs"]:
            self.assertIn("extraSupported", catalog)

    def test_external_catalog_query_hides_watched_items_by_default(self) -> None:
        catalog = SimpleNamespace(id="external-1", filters={"show_watched": False})
        query = asyncio.run(
            routes_stremio_addon_public._build_external_catalog_query(
                "user-id",
                catalog,
                None,
            )
        )

        compiled = str(query.compile(compile_kwargs={"literal_binds": True})).lower()
        self.assertIn("stremio_external_catalog_items", compiled)
        self.assertIn("exists (select watched_items.id", compiled)

    def test_external_catalog_query_can_include_watched_items(self) -> None:
        catalog = SimpleNamespace(id="external-1", filters={"show_watched": True})
        query = asyncio.run(
            routes_stremio_addon_public._build_external_catalog_query(
                "user-id",
                catalog,
                None,
            )
        )

        compiled = str(query.compile(compile_kwargs={"literal_binds": True})).lower()
        self.assertIn("stremio_external_catalog_items", compiled)
        self.assertNotIn("not (exists", compiled)

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

    def test_watchlist_query_includes_rewatch_requested_items(self) -> None:
        catalog = {"media_type": "movie", "filters": {"statuses": []}}
        query = asyncio.run(
            routes_stremio_addon_public._build_watchlist_query("user-id", catalog, None)
        )

        compiled = str(query.compile(compile_kwargs={"literal_binds": True})).lower()
        self.assertIn("watchlist_items.rewatch_requested is true", compiled)

    def test_watchlist_show_query_keeps_status_filter_and_rewatch_override(self) -> None:
        catalog = {"media_type": "tv", "filters": {"statuses": []}}
        query = asyncio.run(
            routes_stremio_addon_public._build_watchlist_query("user-id", catalog, None)
        )

        compiled = str(query.compile(compile_kwargs={"literal_binds": True})).lower()
        self.assertIn("watchlist_items.rewatch_requested is true", compiled)
        self.assertIn("case", compiled)

    def test_slugify_normalizes(self) -> None:
        self.assertEqual(routes_stremio_addon._slugify("Curated Picks!"), "curated-picks")

    def test_slugify_falls_back_for_empty(self) -> None:
        self.assertEqual(routes_stremio_addon._slugify("  "), "catalog")

    def test_reorder_map_requires_all_items(self) -> None:
        with self.assertRaises(HTTPException):
            routes_stremio_addon._build_reorder_map(["a", "b"], ["a"])

    def test_build_external_item_meta_uses_cached_fields_when_media_missing(self) -> None:
        item = StremioExternalCatalogItem(
            stremio_id="tt1234567",
            stremio_type="movie",
            title="Cached Movie",
            year=1999,
            poster_url="https://image.example/poster.jpg",
        )
        meta = routes_stremio_addon_public._build_external_item_meta(item, None, "movie")

        self.assertEqual(
            meta,
            {
                "id": "tt1234567",
                "type": "movie",
                "name": "Cached Movie",
                "year": 1999,
                "poster": "https://image.example/poster.jpg",
            },
        )

    def test_parse_tmdb_list_url(self) -> None:
        refs = parse_tmdb_list_urls(["https://www.themoviedb.org/list/12345-best-movies"])

        self.assertEqual(len(refs), 1)
        self.assertEqual(refs[0].list_id, "12345")
        self.assertEqual(refs[0].external_id, "tmdb:12345")

    def test_parse_tvdb_list_url(self) -> None:
        refs = parse_tvdb_list_urls(["https://thetvdb.com/lists/top-shows"])

        self.assertEqual(len(refs), 1)
        self.assertEqual(refs[0].list_id, "top-shows")
        self.assertEqual(refs[0].external_id, "tvdb:top-shows")

    def test_parse_tmdb_chart_url(self) -> None:
        refs = parse_tmdb_chart_urls(["https://www.themoviedb.org/movie/top-rated"])

        self.assertEqual(len(refs), 1)
        self.assertEqual(refs[0].media_type, "movie")
        self.assertEqual(refs[0].chart_slug, "top-rated")
        self.assertEqual(refs[0].external_id, "tmdb-chart:movie:top-rated")

    def test_discover_tmdb_chart_catalog(self) -> None:
        ref = parse_tmdb_chart_urls(["https://www.themoviedb.org/movie/top-rated"])[0]

        payload = asyncio.run(_discover_tmdb_chart_catalogs(ref))

        self.assertEqual(payload["source_provider"], "tmdb")
        self.assertEqual(payload["catalogs"][0]["id"], "tmdb-chart:movie:top-rated")
        self.assertEqual(payload["catalogs"][0]["type"], "movie")

    def test_parse_imdb_chart_url_with_locale(self) -> None:
        refs = parse_imdb_chart_urls(["https://www.imdb.com/de/chart/top/"])

        self.assertEqual(len(refs), 1)
        self.assertEqual(refs[0].chart_slug, "top")

    def test_discover_payload_accepts_legacy_manifest_url_field(self) -> None:
        payload = routes_stremio_addon.StremioExternalCatalogDiscoverPayload(
            manifest_url="https://www.themoviedb.org/movie/top-rated"
        )

        self.assertEqual(payload.manifest_url, "https://www.themoviedb.org/movie/top-rated")
        self.assertIsNone(payload.source_url)

    def test_parse_mdblist_url(self) -> None:
        refs = parse_mdblist_urls([
            "https://mdblist.com/lists/cb2131/emby-imdb-top-rated-movies"
        ])

        self.assertEqual(len(refs), 1)
        self.assertEqual(refs[0].username, "cb2131")
        self.assertEqual(refs[0].slug, "emby-imdb-top-rated-movies")
        self.assertEqual(refs[0].external_id, "mdblist:cb2131:emby-imdb-top-rated-movies")


if __name__ == "__main__":
    unittest.main()
