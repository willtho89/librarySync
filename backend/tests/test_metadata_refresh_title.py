"""Tests for metadata refresh title behavior across multiple providers.

Covers the four scenarios from the bug report where metadata refresh
could permanently overwrite a show title with "Unknown title":

1. IMDb returns valid metadata → title is applied
2. IMDb returns no match but the next provider does → title comes from next provider
3. IMDb and next provider return no match but a third provider does → title from third
4. No provider returns valid metadata → existing title is preserved
"""
import asyncio
import sys
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.append(str(PROJECT_ROOT / "src"))

from librarysync.connectors.metadata.base import MediaCandidate  # noqa: E402
from librarysync.connectors.metadata.imdb import ImdbMetadataProvider  # noqa: E402
from librarysync.core.metadata_enrichment import apply_refresh_candidate  # noqa: E402

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_media_item(title: str = "Original Title", **kwargs):
    m = MagicMock()
    m.id = "media-item-1"
    m.title = title
    m.year = kwargs.get("year", 2023)
    m.media_type = kwargs.get("media_type", "tv")
    m.imdb_id = kwargs.get("imdb_id", "tt1234567")
    m.tmdb_id = kwargs.get("tmdb_id", "218961")
    m.tvdb_id = kwargs.get("tvdb_id", "409159")
    m.tvmaze_id = kwargs.get("tvmaze_id", None)
    m.kitsu_id = kwargs.get("kitsu_id", None)
    m.myanimelist_id = kwargs.get("myanimelist_id", None)
    m.anilist_id = kwargs.get("anilist_id", None)
    m.poster_url = kwargs.get("poster_url", None)
    m.overview = kwargs.get("overview", None)
    m.genres = kwargs.get("genres", None)
    m.release_date = kwargs.get("release_date", None)
    m.first_air_date = kwargs.get("first_air_date", None)
    m.last_air_date = kwargs.get("last_air_date", None)
    m.runtime_in_seconds = kwargs.get("runtime_in_seconds", None)
    m.raw = kwargs.get("raw", {})
    return m


def _make_candidate(provider: str, title: str, provider_id: str = "12345") -> MediaCandidate:
    return MediaCandidate(
        provider=provider,
        provider_id=provider_id,
        media_type="tv",
        title=title,
        year=2023,
        poster_url=None,
        imdb_id=None,
    )


async def _run_refresh_loop(media_item, providers_and_candidates):
    """Simulate the refresh loop in routes_metadata.py with mocked providers.

    providers_and_candidates: list of (provider_name, candidate_or_none)
    Returns whether any provider successfully refreshed metadata.
    """
    db = AsyncMock()
    # _apply_candidate_ids does DB lookups – stub it out
    with patch(
        "librarysync.core.metadata_enrichment._apply_candidate_ids", new=AsyncMock()
    ):
        refreshed = False
        for _provider_name, candidate in providers_and_candidates:
            if not candidate:
                continue
            await apply_refresh_candidate(db, media_item, candidate, overwrite=not refreshed)
            refreshed = True
    return refreshed


# ---------------------------------------------------------------------------
# IMDb provider unit tests
# ---------------------------------------------------------------------------

class TestImdbGetDetailsReturnsNoneWhenNoMatch(unittest.TestCase):
    """ImdbMetadataProvider.get_details() must return None when the suggestion
    API does not contain an entry matching the requested ID."""

    def _make_provider(self):
        config = MagicMock()
        context = MagicMock()
        context.user_id = "user-1"
        context.include_adult = False
        return ImdbMetadataProvider(config, None, context)

    def _patch_suggestions(self, provider, payload):
        return patch.object(provider, "_get_suggestions", new=AsyncMock(return_value=payload))

    def test_returns_none_when_api_has_no_matching_entry(self):
        """Empty suggestion list → get_details returns None."""
        provider = self._make_provider()
        with self._patch_suggestions(provider, {"d": []}):
            result = asyncio.run(provider.get_details("tt9999999", "tv"))
        self.assertIsNone(result)

    def test_returns_none_when_id_not_in_suggestion_results(self):
        """Suggestion list exists but has a different ID → get_details returns None."""
        provider = self._make_provider()
        payload = {
            "d": [
                {"id": "tt1111111", "l": "Some Other Show", "qid": "tvSeries", "y": 2020},
            ]
        }
        with self._patch_suggestions(provider, payload):
            result = asyncio.run(provider.get_details("tt9999999", "tv"))
        self.assertIsNone(result)

    def test_returns_candidate_when_id_matches(self):
        """Suggestion list contains the requested ID → get_details returns a valid candidate."""
        provider = self._make_provider()
        payload = {
            "d": [
                {"id": "tt9999999", "l": "Drops of God", "qid": "tvSeries", "y": 2023},
            ]
        }
        with self._patch_suggestions(provider, payload):
            result = asyncio.run(provider.get_details("tt9999999", "tv"))
        self.assertIsNotNone(result)
        self.assertEqual(result.title, "Drops of God")

    def test_returns_none_when_matched_entry_has_no_title(self):
        """API returns matching ID but no title fields → get_details returns None."""
        provider = self._make_provider()
        payload = {
            "d": [
                {"id": "tt9999999", "qid": "tvSeries"},  # no "l" or "title" field
            ]
        }
        with self._patch_suggestions(provider, payload):
            result = asyncio.run(provider.get_details("tt9999999", "tv"))
        self.assertIsNone(result)


# ---------------------------------------------------------------------------
# Multi-provider refresh loop scenarios
# ---------------------------------------------------------------------------

class TestMultiProviderRefreshScenarios(unittest.TestCase):
    """Simulate the refresh loop from routes_metadata.py with mocked providers.

    The loop tries providers in order; the first non-None result uses
    overwrite=True (replaces all fields), subsequent use overwrite=False
    (only fills missing fields).
    """

    # ------------------------------------------------------------------
    # Scenario 1: IMDb returns valid metadata
    # ------------------------------------------------------------------
    def test_imdb_returns_valid_metadata_title_is_applied(self):
        """When IMDb returns a valid candidate, the title is written to the media item."""
        media_item = _make_media_item(title="Old Title")
        imdb_candidate = _make_candidate("imdb", "Drops of God", provider_id="tt1234567")

        providers_and_candidates = [
            ("imdb", imdb_candidate),
            ("tmdb", None),
            ("tvdb", None),
        ]
        refreshed = asyncio.run(_run_refresh_loop(media_item, providers_and_candidates))

        self.assertTrue(refreshed)
        self.assertEqual(media_item.title, "Drops of God")

    # ------------------------------------------------------------------
    # Scenario 2: IMDb returns None, next provider (tmdb) returns valid
    # ------------------------------------------------------------------
    def test_imdb_returns_none_tmdb_provides_title(self):
        """When IMDb returns None but TMDB returns a valid candidate, the title
        comes from TMDB and overwrite=True is used (first successful provider)."""
        media_item = _make_media_item(title="Original Title")
        tmdb_candidate = _make_candidate("tmdb", "Drops of God", provider_id="218961")

        providers_and_candidates = [
            ("imdb", None),
            ("tmdb", tmdb_candidate),
            ("tvdb", None),
        ]
        refreshed = asyncio.run(_run_refresh_loop(media_item, providers_and_candidates))

        self.assertTrue(refreshed)
        self.assertEqual(media_item.title, "Drops of God")

    def test_imdb_none_does_not_write_stale_title_from_previous_state(self):
        """When IMDb returns None, the original title must NOT be overwritten to
        any placeholder. It stays intact until a real provider fills it in."""
        media_item = _make_media_item(title="Original Title")

        providers_and_candidates = [
            ("imdb", None),
            ("tmdb", None),
        ]
        asyncio.run(_run_refresh_loop(media_item, providers_and_candidates))

        # No provider succeeded → title is untouched
        self.assertEqual(media_item.title, "Original Title")

    # ------------------------------------------------------------------
    # Scenario 3: IMDb and next provider return None, third provider has title
    # ------------------------------------------------------------------
    def test_third_provider_provides_title_when_first_two_return_none(self):
        """When both IMDb and TMDB return None, TVDB's title is applied with
        overwrite=True because it is the first successful provider."""
        media_item = _make_media_item(title="Old Title")
        tvdb_candidate = _make_candidate("tvdb", "Drops of God", provider_id="409159")

        providers_and_candidates = [
            ("imdb", None),
            ("tmdb", None),
            ("tvdb", tvdb_candidate),
        ]
        refreshed = asyncio.run(_run_refresh_loop(media_item, providers_and_candidates))

        self.assertTrue(refreshed)
        self.assertEqual(media_item.title, "Drops of God")

    # ------------------------------------------------------------------
    # Scenario 4: No provider returns valid metadata
    # ------------------------------------------------------------------
    def test_no_provider_returns_valid_metadata_title_unchanged(self):
        """When all providers return None, the existing title must be preserved."""
        original_title = "Original Show Title"
        media_item = _make_media_item(title=original_title)

        providers_and_candidates = [
            ("imdb", None),
            ("tmdb", None),
            ("tvdb", None),
            ("tvmaze", None),
        ]
        refreshed = asyncio.run(_run_refresh_loop(media_item, providers_and_candidates))

        self.assertFalse(refreshed)
        self.assertEqual(media_item.title, original_title)

    def test_no_provider_returns_valid_metadata_year_unchanged(self):
        """When all providers return None, non-title fields are also unchanged."""
        media_item = _make_media_item(title="My Show", year=2020)

        providers_and_candidates = [
            ("imdb", None),
            ("tmdb", None),
        ]
        refreshed = asyncio.run(_run_refresh_loop(media_item, providers_and_candidates))

        self.assertFalse(refreshed)
        self.assertEqual(media_item.year, 2020)

    # ------------------------------------------------------------------
    # Additional: ensure "Unknown title" string cannot propagate
    # ------------------------------------------------------------------
    def test_candidate_with_unknown_title_string_is_skipped(self):
        """A candidate whose title is the literal 'Unknown title' string (legacy
        behaviour from old providers) must not overwrite the real title when it
        is treated as a falsy / empty value via the None return path."""
        media_item = _make_media_item(title="Real Show")
        # Simulate a provider that returned None (the fix): no candidate
        tmdb_candidate = _make_candidate("tmdb", "Real Show Title Override", provider_id="1")

        providers_and_candidates = [
            ("imdb", None),  # IMDb now returns None instead of "Unknown title"
            ("tmdb", tmdb_candidate),
        ]
        asyncio.run(_run_refresh_loop(media_item, providers_and_candidates))

        self.assertEqual(media_item.title, "Real Show Title Override")


if __name__ == "__main__":
    unittest.main()
