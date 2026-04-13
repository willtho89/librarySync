import sys
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from librarysync.connectors.services.publicmetadb import PublicMetaDbError  # noqa: E402
from librarysync.connectors.services.trakt import TraktError  # noqa: E402
from librarysync.jobs import process_outbox  # noqa: E402


class TestTraktUpdateHistoryFallback:
    """Regression tests for Trakt update_history behavior.

    Bug: When a user changes the watched_at timestamp on an item that was previously
    synced to Trakt, the system tries to find the old history entry to remove it.
    Previously, if the old entry couldn't be found (e.g., never synced there, or
    deleted from Trakt directly), the job would fail permanently with:
    "Trakt update could not locate prior history entry"

    Fix: When prior entry is not found, gracefully fall back to adding a new entry
    instead of failing. The sync should succeed even if the prior state is unknown.
    """

    @pytest.fixture
    def mock_trakt_client(self):
        client = MagicMock()
        client.remove_history = AsyncMock(return_value=({}, 200))
        client.add_history = AsyncMock(return_value=({"history": {"movies": [12345]}}, 201))
        client.update_history = AsyncMock(return_value=({}, 200))
        return client

    @pytest.fixture
    def base_payload(self):
        return {
            "media_type": "movie",
            "movie_ids": {"imdb": "tt1234567", "tmdb": 550},
            "watched_at": "2026-04-11T19:00:00+00:00",
            "previous_watched_at": "2026-04-10T20:00:00+00:00",
            "watched_item_id": "watched-1",
        }

    @pytest.fixture
    def payload_for_update_history(self):
        return {
            "media_type": "movie",
            "movie_ids": {"imdb": "tt1234567", "tmdb": 550},
            "watched_at": "2026-04-11T19:00:00+00:00",
            "history_id": "trakt-history-456",
            "watched_item_id": "watched-1",
        }

    @pytest.fixture
    def mock_settings(self):
        settings = MagicMock()
        settings.trakt_client_id = "test_client_id"
        settings.trakt_client_secret = "test_client_secret"
        return settings

    @pytest.mark.asyncio
    async def test_trakt_update_falls_back_when_prior_entry_not_found(
        self, mock_trakt_client, base_payload, mock_settings
    ):
        """When prior history entry is not found, should add new entry instead of failing."""
        mock_job = SimpleNamespace(id="job-1", user_id="user-1", attempts=1, payload=base_payload)

        with (
            patch(
                "librarysync.jobs.process_outbox.load_integration_with_secrets",
                new=AsyncMock(
                    return_value=(
                        MagicMock(id="int-1"),
                        {"access_token": "token", "refresh_token": "refresh"},
                    )
                ),
            ),
            patch(
                "librarysync.jobs.process_outbox._ensure_trakt_access_token",
                new=AsyncMock(return_value="token"),
            ),
            patch(
                "librarysync.jobs.process_outbox._resolve_trakt_history_match",
                new=AsyncMock(return_value=None),
            ),
            patch(
                "librarysync.jobs.process_outbox.TraktClient",
                return_value=mock_trakt_client,
            ),
            patch("librarysync.jobs.process_outbox.settings", mock_settings),
        ):
            response_code, external_id = await process_outbox._deliver_trakt_update(None, mock_job)

        assert response_code == 201
        mock_trakt_client.add_history.assert_awaited_once()
        # update_history should NOT be called when prior entry is not found
        mock_trakt_client.update_history.assert_not_awaited()
        # remove_history should NOT be called either
        mock_trakt_client.remove_history.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_trakt_update_uses_existing_when_found(
        self, mock_trakt_client, base_payload, mock_settings
    ):
        """When prior history entry exists, should remove and re-add."""
        mock_job = SimpleNamespace(id="job-1", user_id="user-1", attempts=1, payload=base_payload)

        with (
            patch(
                "librarysync.jobs.process_outbox.load_integration_with_secrets",
                new=AsyncMock(
                    return_value=(
                        MagicMock(id="int-1"),
                        {"access_token": "token", "refresh_token": "refresh"},
                    )
                ),
            ),
            patch(
                "librarysync.jobs.process_outbox._ensure_trakt_access_token",
                new=AsyncMock(return_value="token"),
            ),
            patch(
                "librarysync.jobs.process_outbox._resolve_trakt_history_match",
                new=AsyncMock(
                    return_value=(
                        "trakt-history-123",
                        datetime(2026, 4, 10, 20, 0, tzinfo=timezone.utc),
                    )
                ),
            ),
            patch(
                "librarysync.jobs.process_outbox.TraktClient",
                return_value=mock_trakt_client,
            ),
            patch("librarysync.jobs.process_outbox.settings", mock_settings),
        ):
            response_code, external_id = await process_outbox._deliver_trakt_update(None, mock_job)

        assert response_code == 201
        mock_trakt_client.remove_history.assert_awaited_once()
        mock_trakt_client.add_history.assert_awaited_once()
        # update_history is not used when we have a prior entry match
        mock_trakt_client.update_history.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_trakt_update_falls_back_when_update_history_fails_with_404(
        self, mock_trakt_client, payload_for_update_history, mock_settings
    ):
        mock_job = SimpleNamespace(
            id="job-1", user_id="user-1", attempts=1, payload=payload_for_update_history
        )

        mock_trakt_client.update_history = AsyncMock(
            side_effect=TraktError("Not found", status_code=404)
        )

        with (
            patch(
                "librarysync.jobs.process_outbox.load_integration_with_secrets",
                new=AsyncMock(
                    return_value=(
                        MagicMock(id="int-1"),
                        {"access_token": "token", "refresh_token": "refresh"},
                    )
                ),
            ),
            patch(
                "librarysync.jobs.process_outbox._ensure_trakt_access_token",
                new=AsyncMock(return_value="token"),
            ),
            patch(
                "librarysync.jobs.process_outbox.TraktClient",
                return_value=mock_trakt_client,
            ),
            patch("librarysync.jobs.process_outbox.settings", mock_settings),
        ):
            response_code, external_id = await process_outbox._deliver_trakt_update(None, mock_job)

        assert response_code == 201
        mock_trakt_client.update_history.assert_awaited_once()
        mock_trakt_client.remove_history.assert_not_awaited()
        mock_trakt_client.add_history.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_trakt_update_falls_back_when_update_history_fails_with_400(
        self, mock_trakt_client, payload_for_update_history, mock_settings
    ):
        mock_job = SimpleNamespace(
            id="job-1", user_id="user-1", attempts=1, payload=payload_for_update_history
        )

        mock_trakt_client.update_history = AsyncMock(
            side_effect=TraktError("Bad Request", status_code=400)
        )

        with (
            patch(
                "librarysync.jobs.process_outbox.load_integration_with_secrets",
                new=AsyncMock(
                    return_value=(
                        MagicMock(id="int-1"),
                        {"access_token": "token", "refresh_token": "refresh"},
                    )
                ),
            ),
            patch(
                "librarysync.jobs.process_outbox._ensure_trakt_access_token",
                new=AsyncMock(return_value="token"),
            ),
            patch(
                "librarysync.jobs.process_outbox.TraktClient",
                return_value=mock_trakt_client,
            ),
            patch("librarysync.jobs.process_outbox.settings", mock_settings),
        ):
            response_code, external_id = await process_outbox._deliver_trakt_update(None, mock_job)

        assert response_code == 201
        mock_trakt_client.update_history.assert_awaited_once()
        mock_trakt_client.remove_history.assert_not_awaited()
        mock_trakt_client.add_history.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_trakt_update_does_not_fallback_on_unexpected_trakt_error(
        self, mock_trakt_client, payload_for_update_history, mock_settings
    ):
        mock_job = SimpleNamespace(
            id="job-1", user_id="user-1", attempts=1, payload=payload_for_update_history
        )

        mock_trakt_client.update_history = AsyncMock(
            side_effect=TraktError("Internal Server Error", status_code=500)
        )

        with (
            patch(
                "librarysync.jobs.process_outbox.load_integration_with_secrets",
                new=AsyncMock(
                    return_value=(
                        MagicMock(id="int-1"),
                        {"access_token": "token", "refresh_token": "refresh"},
                    )
                ),
            ),
            patch(
                "librarysync.jobs.process_outbox._ensure_trakt_access_token",
                new=AsyncMock(return_value="token"),
            ),
            patch(
                "librarysync.jobs.process_outbox.TraktClient",
                return_value=mock_trakt_client,
            ),
            patch("librarysync.jobs.process_outbox.settings", mock_settings),
        ):
            with pytest.raises(TraktError) as exc_info:
                await process_outbox._deliver_trakt_update(None, mock_job)

        assert exc_info.value.status_code == 500
        mock_trakt_client.remove_history.assert_not_awaited()
        mock_trakt_client.add_history.assert_not_awaited()


class TestPublicMetaDBRatingDuplicate:
    """Regression tests for PublicMetaDB rating sync behavior.

    Bug: When syncing a rating to PublicMetaDB after changing it (e.g., from 4 stars
    to 2.5 stars), the API would return 409 Conflict because an entry with those
    values already existed. The code didn't check for existing ratings before creating,
    causing permanent sync failures.

    Fix: Before creating a rating, check if one exists for that item. If it does,
    delete the old one first, then create the new rating. Also added 409 exception
    handling as a safety net for race conditions.
    """

    @pytest.fixture
    def mock_client_movie(self):
        client = MagicMock()
        client.list_ratings = AsyncMock(
            return_value=(
                {"items": [{"id": "rating-42", "score": 50, "label": "overall"}]},
                200,
            )
        )
        client.create_rating = AsyncMock(return_value=({"id": "rating-new"}, 201))
        client.delete_rating = AsyncMock(return_value=({}, 200))
        return client

    @pytest.fixture
    def mock_client_episode(self):
        client = MagicMock()
        client.list_episode_ratings = AsyncMock(
            return_value=(
                {"items": [{"id": "episode-rating-99", "score": 70, "label": "overall"}]},
                200,
            )
        )
        client.create_episode_rating = AsyncMock(return_value=({"id": "episode-rating-new"}, 201))
        client.delete_episode_rating = AsyncMock(return_value=({}, 200))
        return client

    @pytest.fixture
    def movie_job(self):
        return SimpleNamespace(
            id="job-1",
            user_id="user-1",
            attempts=1,
            payload={
                "media_type": "movie",
                "tmdb_id": 550,
                "rating": 2.5,
                "watched_item_id": "watched-1",
            },
        )

    @pytest.fixture
    def episode_job(self):
        return SimpleNamespace(
            id="job-2",
            user_id="user-1",
            attempts=1,
            payload={
                "media_type": "tv",
                "tmdb_id": 1399,
                "season_number": 1,
                "episode_number": 5,
                "rating": 4.0,
                "watched_item_id": "watched-2",
            },
        )

    @pytest.mark.asyncio
    async def test_publicmetadb_rating_handles_existing_rating(self, mock_client_movie, movie_job):
        """When rating exists, should delete old and create new."""
        with (
            patch(
                "librarysync.jobs.process_outbox._load_publicmetadb_client",
                new=AsyncMock(return_value=(mock_client_movie, "api-key")),
            ),
        ):
            response_code, external_id = await process_outbox._deliver_publicmetadb_rating(
                None, movie_job
            )

        assert response_code == 201
        mock_client_movie.delete_rating.assert_awaited_once_with("api-key", "rating-42")
        mock_client_movie.create_rating.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_publicmetadb_rating_creates_new_when_no_existing(
        self, mock_client_movie, movie_job
    ):
        """When no existing rating, should create directly without deleting."""
        mock_client_movie.list_ratings = AsyncMock(return_value=({"items": []}, 200))

        with (
            patch(
                "librarysync.jobs.process_outbox._load_publicmetadb_client",
                new=AsyncMock(return_value=(mock_client_movie, "api-key")),
            ),
        ):
            response_code, external_id = await process_outbox._deliver_publicmetadb_rating(
                None, movie_job
            )

        assert response_code == 201
        mock_client_movie.delete_rating.assert_not_awaited()
        mock_client_movie.create_rating.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_publicmetadb_rating_409_safety_net_retry(self, mock_client_movie, movie_job):
        mock_client_movie.create_rating = AsyncMock(
            side_effect=[
                PublicMetaDbError("Conflict", status_code=409),
                ({"id": "rating-retry"}, 201),
            ]
        )

        with (
            patch(
                "librarysync.jobs.process_outbox._load_publicmetadb_client",
                new=AsyncMock(return_value=(mock_client_movie, "api-key")),
            ),
        ):
            response_code, external_id = await process_outbox._deliver_publicmetadb_rating(
                None, movie_job
            )

        assert response_code == 201
        assert mock_client_movie.delete_rating.call_count == 2
        mock_client_movie.delete_rating.assert_awaited_with("api-key", "rating-42")
        assert mock_client_movie.create_rating.call_count == 2

    @pytest.mark.asyncio
    async def test_publicmetadb_rating_409_safety_net_raises_when_no_existing_id(
        self, mock_client_movie, movie_job
    ):
        """When create_rating raises 409 but no existing_id was found, should re-raise."""
        mock_client_movie.list_ratings = AsyncMock(return_value=({"items": []}, 200))
        mock_client_movie.create_rating = AsyncMock(
            side_effect=PublicMetaDbError("Conflict", status_code=409)
        )

        with (
            patch(
                "librarysync.jobs.process_outbox._load_publicmetadb_client",
                new=AsyncMock(return_value=(mock_client_movie, "api-key")),
            ),
        ):
            with pytest.raises(PublicMetaDbError) as exc_info:
                await process_outbox._deliver_publicmetadb_rating(None, movie_job)

        assert exc_info.value.status_code == 409
        # No existing ID to delete, so we can't retry - must re-raise

    @pytest.mark.asyncio
    async def test_publicmetadb_episode_rating_handles_existing_rating(
        self, mock_client_episode, episode_job
    ):
        """When episode rating exists, should delete old and create new."""
        with (
            patch(
                "librarysync.jobs.process_outbox._load_publicmetadb_client",
                new=AsyncMock(return_value=(mock_client_episode, "api-key")),
            ),
        ):
            response_code, external_id = await process_outbox._deliver_publicmetadb_rating(
                None, episode_job
            )

        assert response_code == 201
        mock_client_episode.delete_episode_rating.assert_awaited_once_with(
            "api-key", "episode-rating-99"
        )
        mock_client_episode.create_episode_rating.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_publicmetadb_episode_rating_creates_new_when_no_existing(
        self, mock_client_episode, episode_job
    ):
        """When no existing episode rating, should create directly."""
        mock_client_episode.list_episode_ratings = AsyncMock(return_value=({"items": []}, 200))

        with (
            patch(
                "librarysync.jobs.process_outbox._load_publicmetadb_client",
                new=AsyncMock(return_value=(mock_client_episode, "api-key")),
            ),
        ):
            response_code, external_id = await process_outbox._deliver_publicmetadb_rating(
                None, episode_job
            )

        assert response_code == 201
        mock_client_episode.delete_episode_rating.assert_not_awaited()
        mock_client_episode.create_episode_rating.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_publicmetadb_episode_rating_409_safety_net_retry(
        self, mock_client_episode, episode_job
    ):
        mock_client_episode.create_episode_rating = AsyncMock(
            side_effect=[
                PublicMetaDbError("Conflict", status_code=409),
                ({"id": "episode-rating-retry"}, 201),
            ]
        )

        with (
            patch(
                "librarysync.jobs.process_outbox._load_publicmetadb_client",
                new=AsyncMock(return_value=(mock_client_episode, "api-key")),
            ),
        ):
            response_code, external_id = await process_outbox._deliver_publicmetadb_rating(
                None, episode_job
            )

        assert response_code == 201
        assert mock_client_episode.delete_episode_rating.call_count == 2
        mock_client_episode.delete_episode_rating.assert_awaited_with(
            "api-key", "episode-rating-99"
        )
        assert mock_client_episode.create_episode_rating.call_count == 2

    @pytest.mark.asyncio
    async def test_publicmetadb_episode_rating_409_raises_when_no_existing_id(
        self, mock_client_episode, episode_job
    ):
        """When create_episode_rating raises 409 but no existing_id, should re-raise."""
        mock_client_episode.list_episode_ratings = AsyncMock(return_value=({"items": []}, 200))
        mock_client_episode.create_episode_rating = AsyncMock(
            side_effect=PublicMetaDbError("Conflict", status_code=409)
        )

        with (
            patch(
                "librarysync.jobs.process_outbox._load_publicmetadb_client",
                new=AsyncMock(return_value=(mock_client_episode, "api-key")),
            ),
        ):
            with pytest.raises(PublicMetaDbError) as exc_info:
                await process_outbox._deliver_publicmetadb_rating(None, episode_job)

        assert exc_info.value.status_code == 409


class TestPublicMetaDBWatch409:
    @pytest.fixture
    def mock_client(self):
        client = MagicMock()
        client.list_watched = AsyncMock(
            return_value=(
                {"items": [{"id": "watched-42", "tmdb_id": 1399, "type": "tv"}]},
                200,
            )
        )
        client.delete_watched = AsyncMock(return_value=({}, 200))
        client.mark_watched = AsyncMock(return_value=({"id": "watched-new"}, 201))
        return client

    @pytest.fixture
    def episode_job(self):
        return SimpleNamespace(
            id="job-1",
            user_id="user-1",
            attempts=1,
            payload={
                "media_type": "tv",
                "tmdb_id": 1399,
                "season_number": 1,
                "episode_number": 5,
                "watched_at": "2026-04-11T19:00:00+00:00",
                "watched_item_id": "watched-1",
            },
        )

    @pytest.mark.asyncio
    async def test_publicmetadb_watch_success(self, mock_client, episode_job):
        with (
            patch(
                "librarysync.jobs.process_outbox._load_publicmetadb_client",
                new=AsyncMock(return_value=(mock_client, "api-key")),
            ),
        ):
            response_code, external_id = await process_outbox._deliver_publicmetadb_watch(
                None, episode_job
            )

        assert response_code == 201
        mock_client.mark_watched.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_publicmetadb_watch_raises_when_409_but_no_existing_id(
        self, mock_client, episode_job
    ):
        mock_client.list_watched = AsyncMock(return_value=({"items": []}, 200))
        mock_client.mark_watched = AsyncMock(
            side_effect=PublicMetaDbError("Conflict", status_code=409)
        )

        with (
            patch(
                "librarysync.jobs.process_outbox._load_publicmetadb_client",
                new=AsyncMock(return_value=(mock_client, "api-key")),
            ),
        ):
            with pytest.raises(PublicMetaDbError) as exc_info:
                await process_outbox._deliver_publicmetadb_watch(None, episode_job)

        assert exc_info.value.status_code == 409


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
