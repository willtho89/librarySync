import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx
from librarysync.connectors.services.simkl import SimklClient
from librarysync.connectors.services.trakt import TraktClient
from librarysync.core import watch_pipeline, watchlist_sync
from librarysync.jobs import process_outbox


def test_watchlist_update_dedupe_key_uses_media_id() -> None:
    key = watch_pipeline._build_outbox_dedupe_key(
        "user-1",
        "internal",
        "watchlist_update",
        {"media_item_id": "media-1"},
    )
    assert key == "user-1:internal:watchlist_update:media-1"


def test_watchlist_update_dedupe_key_requires_media_id() -> None:
    key = watch_pipeline._build_outbox_dedupe_key(
        "user-1",
        "internal",
        "watchlist_update",
        {},
    )
    assert key is None


def test_publicmetadb_watchlist_payload_movie() -> None:
    payload = watchlist_sync._build_publicmetadb_payload(
        SimpleNamespace(id="wl-1", type="movie"),
        SimpleNamespace(id="m-1", imdb_id="tt0137523", tmdb_id="550", tvdb_id=None),
    )
    assert payload is not None
    assert payload["media_type"] == "movie"
    assert payload["tmdb_id"] == "550"


def test_publicmetadb_watchlist_payload_tv() -> None:
    payload = watchlist_sync._build_publicmetadb_payload(
        SimpleNamespace(id="wl-1", type="tv"),
        SimpleNamespace(id="m-1", imdb_id=None, tmdb_id="1399", tvdb_id="121361"),
    )
    assert payload is not None
    assert payload["media_type"] == "tv"
    assert payload["tmdb_id"] == "1399"


def test_publicmetadb_watchlist_payload_requires_tmdb() -> None:
    payload = watchlist_sync._build_publicmetadb_payload(
        SimpleNamespace(id="wl-1", type="movie"),
        SimpleNamespace(id="m-1", imdb_id="tt0137523", tmdb_id=None, tvdb_id=None),
    )
    assert payload is None


def test_trakt_watchlist_removal_uses_delete_sync_watchlist() -> None:
    client = TraktClient(client_id="trakt-client", client_secret="secret")
    response = httpx.Response(204, request=httpx.Request("DELETE", "https://api.trakt.tv/sync/watchlist"))
    client._request = AsyncMock(return_value=response)  # type: ignore[method-assign]

    payload = {"shows": [{"ids": {"tmdb": 1399}}]}
    parsed, status_code = asyncio.run(client.remove_from_watchlist(payload, "token"))

    assert parsed == {}
    assert status_code == 204
    client._request.assert_awaited_once_with(  # type: ignore[attr-defined]
        "DELETE",
        "/sync/watchlist",
        access_token="token",
        json_body=payload,
    )


def test_simkl_drop_show_payload_moves_tv_show_to_dropped_list() -> None:
    payload = process_outbox._build_simkl_drop_watchlist_payload(
        {
            "media_type": "tv",
            "show_ids": {"tmdb": "1399", "simkl": "42"},
        }
    )

    assert payload == {
        "to": "dropped",
        "shows": [{"ids": {"tmdb": 1399, "simkl": 42}}],
    }


def test_simkl_drop_show_payload_moves_anime_to_dropped_list() -> None:
    payload = process_outbox._build_simkl_drop_watchlist_payload(
        {
            "media_type": "anime",
            "show_ids": {"tmdb": "1234", "simkl": "99"},
        }
    )

    assert payload == {
        "to": "dropped",
        "anime": [{"ids": {"tmdb": 1234, "simkl": 99}}],
    }


def test_simkl_drop_show_calls_add_to_list_endpoint() -> None:
    client = SimklClient(client_id="simkl-client", client_secret="secret")
    response = httpx.Response(201, json={"added": {"shows": 1}}, request=httpx.Request("POST", "https://api.simkl.com/sync/add-to-list"))
    client._request = AsyncMock(return_value=response)  # type: ignore[method-assign]

    payload = {"to": "dropped", "shows": [{"ids": {"tmdb": 1399}}]}
    parsed, status_code = asyncio.run(client.add_to_list(payload, "token"))

    assert parsed == {"added": {"shows": 1}}
    assert status_code == 201
    client._request.assert_awaited_once_with(  # type: ignore[attr-defined]
        "POST",
        "/sync/add-to-list",
        access_token="token",
        json_body=payload,
    )


def test_simkl_watchlist_payload_uses_anime_container_for_anime() -> None:
    payload = process_outbox._build_simkl_watchlist_payload(
        {
            "media_type": "anime",
            "show_ids": {"tmdb": "1234", "simkl": "99"},
        }
    )

    assert payload == {
        "anime": [{"ids": {"tmdb": 1234, "simkl": 99}}],
    }


def test_watchlist_sync_simkl_payload_uses_show_ids_for_anime() -> None:
    payload = watchlist_sync._build_simkl_payload(
        SimpleNamespace(id="wl-1", type="anime"),
        SimpleNamespace(
            id="media-1",
            imdb_id="tt1234567",
            tmdb_id="1234",
            tvdb_id="5678",
            raw={"simkl_id": "99"},
        ),
    )

    assert payload is not None
    assert "show_ids" in payload
    assert "movie_ids" not in payload
    assert payload["show_ids"] == {
        "imdb": "tt1234567",
        "tmdb": "1234",
        "tvdb": "5678",
        "simkl": "99",
    }


def test_simkl_watchlist_remove_delivery_drops_show() -> None:
    job = SimpleNamespace(
        user_id="user-1",
        payload={"media_type": "tv", "show_ids": {"tmdb": "1399"}},
    )
    integration = SimpleNamespace(id="integration-1")
    client = SimpleNamespace(add_to_list=AsyncMock(return_value=({}, 201)))
    settings = SimpleNamespace(simkl_client_id="simkl-client", simkl_client_secret="secret")

    with (
        patch(
            "librarysync.jobs.process_outbox.load_integration_with_secrets",
            new=AsyncMock(return_value=(integration, {"access_token": "token"})),
        ),
        patch("librarysync.jobs.process_outbox.settings", settings),
        patch("librarysync.jobs.process_outbox.SimklClient", return_value=client),
        patch(
            "librarysync.jobs.process_outbox._ensure_simkl_access_token",
            new=AsyncMock(return_value="token"),
        ),
    ):
        response_code, external_id = asyncio.run(
            process_outbox._deliver_simkl_watchlist_remove(None, job)
        )

    assert response_code == 201
    assert external_id is None
    client.add_to_list.assert_awaited_once_with(
        {"to": "dropped", "shows": [{"ids": {"tmdb": 1399}}]},
        "token",
    )


def test_simkl_watchlist_remove_delivery_removes_movie() -> None:
    job = SimpleNamespace(
        user_id="user-1",
        payload={"media_type": "movie", "movie_ids": {"tmdb": "550"}},
    )
    integration = SimpleNamespace(id="integration-1")
    client = SimpleNamespace(remove_from_watchlist=AsyncMock(return_value=({}, 200)))
    settings = SimpleNamespace(simkl_client_id="simkl-client", simkl_client_secret="secret")

    with (
        patch(
            "librarysync.jobs.process_outbox.load_integration_with_secrets",
            new=AsyncMock(return_value=(integration, {"access_token": "token"})),
        ),
        patch("librarysync.jobs.process_outbox.settings", settings),
        patch("librarysync.jobs.process_outbox.SimklClient", return_value=client),
        patch(
            "librarysync.jobs.process_outbox._ensure_simkl_access_token",
            new=AsyncMock(return_value="token"),
        ),
    ):
        response_code, external_id = asyncio.run(
            process_outbox._deliver_simkl_watchlist_remove(None, job)
        )

    assert response_code == 200
    assert external_id is None
    client.remove_from_watchlist.assert_awaited_once_with(
        {"movies": [{"ids": {"tmdb": 550}}]},
        "token",
    )


class TestPublicMetaDbWatchlistOutbox(unittest.TestCase):
    def test_handler_supports_push_watchlist(self) -> None:
        handler = process_outbox.PublicMetaDbOutboxHandler()
        job = SimpleNamespace(job_type="push_watchlist")
        with patch(
            "librarysync.jobs.process_outbox._deliver_publicmetadb_watchlist",
            new=AsyncMock(return_value=(200, "external-1")),
        ) as mocked:
            result = asyncio.run(handler.deliver(None, job))
        self.assertEqual(result.response_code, 200)
        self.assertEqual(result.external_id, "external-1")
        mocked.assert_awaited_once_with(None, job)

    def test_handler_supports_remove_watchlist(self) -> None:
        handler = process_outbox.PublicMetaDbOutboxHandler()
        job = SimpleNamespace(job_type="remove_watchlist")
        with patch(
            "librarysync.jobs.process_outbox._deliver_publicmetadb_watchlist_remove",
            new=AsyncMock(return_value=(200, None)),
        ) as mocked:
            result = asyncio.run(handler.deliver(None, job))
        self.assertEqual(result.response_code, 200)
        self.assertIsNone(result.external_id)
        mocked.assert_awaited_once_with(None, job)
