import asyncio
import unittest
from unittest.mock import AsyncMock, patch

from librarysync.connectors.metadata.base import ProviderContext
from librarysync.connectors.metadata.publicmetadb import (
    MEDIA_TYPE_MOVIE,
    MEDIA_TYPE_TV,
    PublicMetaDbMetadataProvider,
)


class TestPublicMetaDbMetadataProvider(unittest.TestCase):
    def setUp(self) -> None:
        self.provider = PublicMetaDbMetadataProvider.from_settings(
            {},
            {"api_key": "pm-test-key"},
            ProviderContext(user_id="test-user"),
        )

    def test_find_by_external_id_maps_ids(self) -> None:
        lookup_payload = {
            "results": [{"tmdb_id": 1399, "media_type": "tv"}],
            "total": 1,
        }
        mappings_payload = {
            "tmdb_id": 1399,
            "media_type": "tv",
            "mappings": {
                "imdb": [{"value": "tt0944947"}],
                "tvdb": [{"value": "121361"}],
                "mal": [{"value": "1535"}],
                "anilist": [{"value": "20958"}],
            },
        }
        with patch.object(
            self.provider,
            "_get",
            new=AsyncMock(side_effect=[lookup_payload, mappings_payload]),
        ) as mocked_get:
            candidates = asyncio.run(self.provider.find_by_external_id("tt0944947", MEDIA_TYPE_TV))

        self.assertEqual(len(candidates), 1)
        candidate = candidates[0]
        self.assertEqual(candidate.provider, "publicmetadb")
        self.assertEqual(candidate.provider_id, "1399")
        self.assertEqual(candidate.media_type, MEDIA_TYPE_TV)
        self.assertEqual(candidate.imdb_id, "tt0944947")
        self.assertEqual(candidate.raw["ids"]["tmdb_id"], "1399")
        self.assertEqual(candidate.raw["ids"]["tvdb_id"], "121361")
        self.assertEqual(candidate.raw["ids"]["myanimelist_id"], "1535")
        self.assertEqual(candidate.raw["ids"]["anilist_id"], "20958")
        self.assertEqual(mocked_get.await_count, 2)

    def test_find_by_external_id_rejects_non_id_query(self) -> None:
        with patch.object(self.provider, "_get", new=AsyncMock()) as mocked_get:
            candidates = asyncio.run(
                self.provider.find_by_external_id("the matrix", MEDIA_TYPE_MOVIE)
            )
        self.assertEqual(candidates, [])
        mocked_get.assert_not_awaited()

    def test_get_details_uses_mapping_payload(self) -> None:
        payload = {
            "tmdb_id": 550,
            "media_type": "movie",
            "title": "Fight Club",
            "release_date": "1999-10-15",
            "mappings": {
                "imdb": [{"value": "tt0137523"}],
                "tvdb": [{"value": "4022"}],
            },
        }
        with patch.object(self.provider, "_get", new=AsyncMock(return_value=payload)):
            candidate = asyncio.run(self.provider.get_details("550", MEDIA_TYPE_MOVIE))

        self.assertEqual(candidate.provider, "publicmetadb")
        self.assertEqual(candidate.provider_id, "550")
        self.assertEqual(candidate.media_type, MEDIA_TYPE_MOVIE)
        self.assertEqual(candidate.title, "Fight Club")
        self.assertEqual(candidate.year, 1999)
        self.assertEqual(candidate.imdb_id, "tt0137523")
        self.assertEqual(candidate.raw["ids"]["tvdb_id"], "4022")

    def test_validate_credentials_uses_lookup_endpoint(self) -> None:
        with patch.object(
            self.provider,
            "_get",
            new=AsyncMock(return_value={"results": []}),
        ) as mocked_get:
            asyncio.run(self.provider.validate_credentials())
        mocked_get.assert_awaited_once_with(
            "/api/external/mappings/lookup",
            {"id_type": "imdb", "id_value": "tt0944947", "media_type": MEDIA_TYPE_TV},
        )


if __name__ == "__main__":
    unittest.main()
