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


def test_trakt_watchlist_removal_uses_post_sync_watchlist_remove() -> None:
    client = TraktClient(client_id="trakt-client", client_secret="secret")
    response = httpx.Response(
        200,
        json={"deleted": {"shows": 1}},
        request=httpx.Request("POST", "https://api.trakt.tv/sync/watchlist/remove"),
    )
    client._request = AsyncMock(return_value=response)  # type: ignore[method-assign]

    payload = {"shows": [{"ids": {"tmdb": 1399}}]}
    parsed, status_code = asyncio.run(client.remove_from_watchlist(payload, "token"))

    assert parsed == {"deleted": {"shows": 1}}
    assert status_code == 200
    client._request.assert_awaited_once_with(  # type: ignore[attr-defined]
        "POST",
        "/sync/watchlist/remove",
        access_token="token",
        json_body=payload,
    )


def test_trakt_add_hidden_items_posts_to_hidden_section() -> None:
    client = TraktClient(client_id="trakt-client", client_secret="secret")
    response = httpx.Response(
        201,
        json={"added": {"shows": 1}},
        request=httpx.Request("POST", "https://api.trakt.tv/users/hidden/dropped"),
    )
    client._request = AsyncMock(return_value=response)  # type: ignore[method-assign]

    payload = {"shows": [{"ids": {"tmdb": 1399}}]}
    parsed, status_code = asyncio.run(client.add_hidden_items("dropped", payload, "token"))

    assert parsed == {"added": {"shows": 1}}
    assert status_code == 201
    client._request.assert_awaited_once_with(  # type: ignore[attr-defined]
        "POST",
        "/users/hidden/dropped",
        access_token="token",
        json_body=payload,
    )


def test_trakt_remove_hidden_items_posts_to_hidden_section_remove() -> None:
    client = TraktClient(client_id="trakt-client", client_secret="secret")
    response = httpx.Response(
        200,
        json={"deleted": {"shows": 1}},
        request=httpx.Request("POST", "https://api.trakt.tv/users/hidden/dropped/remove"),
    )
    client._request = AsyncMock(return_value=response)  # type: ignore[method-assign]

    payload = {"shows": [{"ids": {"tmdb": 1399}}]}
    parsed, status_code = asyncio.run(client.remove_hidden_items("dropped", payload, "token"))

    assert parsed == {"deleted": {"shows": 1}}
    assert status_code == 200
    client._request.assert_awaited_once_with(  # type: ignore[attr-defined]
        "POST",
        "/users/hidden/dropped/remove",
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
        "shows": [{"ids": {"tmdb": 1399, "simkl": 42}, "to": "dropped"}],
    }


def test_simkl_drop_show_payload_moves_anime_to_dropped_list() -> None:
    payload = process_outbox._build_simkl_drop_watchlist_payload(
        {
            "media_type": "anime",
            "show_ids": {"tmdb": "1234", "simkl": "99"},
        }
    )

    # SIMKL treats anime as shows in POST payloads (no "anime" container).
    assert payload == {
        "shows": [{"ids": {"tmdb": 1234, "simkl": 99}, "to": "dropped"}],
    }


def test_simkl_drop_show_calls_add_to_list_endpoint() -> None:
    client = SimklClient(client_id="simkl-client", client_secret="secret")
    response = httpx.Response(
        200,
        json={},
        request=httpx.Request("POST", "https://api.simkl.com/sync/add-to-list"),
    )
    client._request = AsyncMock(return_value=response)  # type: ignore[method-assign]

    payload = {"shows": [{"ids": {"tmdb": 1399}, "to": "dropped"}]}
    parsed, status_code = asyncio.run(client.add_to_list(payload, "token"))

    assert parsed == {}
    assert status_code == 200
    client._request.assert_awaited_once_with(  # type: ignore[attr-defined]
        "POST",
        "/sync/add-to-list",
        access_token="token",
        json_body=payload,
    )


def test_simkl_watchlist_payload_uses_shows_container_for_anime() -> None:
    payload = process_outbox._build_simkl_watchlist_payload(
        {
            "media_type": "anime",
            "show_ids": {"tmdb": "1234", "simkl": "99"},
        }
    )

    # SIMKL treats anime as shows in POST payloads (no "anime" container).
    assert payload == {
        "shows": [{"ids": {"tmdb": 1234, "simkl": 99}}],
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


def test_simkl_watchlist_remove_delivery_moves_dropped_show_to_dropped_list() -> None:
    job = SimpleNamespace(
        user_id="user-1",
        payload={"media_type": "tv", "show_ids": {"tmdb": "1399"}, "hide_dropped": True},
    )
    integration = SimpleNamespace(id="integration-1")
    client = SimpleNamespace(
        add_to_list=AsyncMock(return_value=({}, 200)),
        remove_from_watchlist=AsyncMock(return_value=({}, 200)),
    )
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
    client.add_to_list.assert_awaited_once_with(
        {"shows": [{"ids": {"tmdb": 1399}, "to": "dropped"}]},
        "token",
    )
    client.remove_from_watchlist.assert_not_awaited()


def test_simkl_watchlist_remove_delivery_without_drop_removes_watchlist() -> None:
    job = SimpleNamespace(
        user_id="user-1",
        payload={"media_type": "tv", "show_ids": {"tmdb": "1399"}},
    )
    integration = SimpleNamespace(id="integration-1")
    client = SimpleNamespace(
        add_to_list=AsyncMock(return_value=({}, 200)),
        remove_from_watchlist=AsyncMock(return_value=({}, 200)),
    )
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
        {"shows": [{"ids": {"tmdb": 1399}}]},
        "token",
    )
    client.add_to_list.assert_not_awaited()


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


def _make_trakt_delivery_mocks() -> tuple[SimpleNamespace, SimpleNamespace]:
    integration = SimpleNamespace(id="integration-1")
    client = SimpleNamespace(
        add_to_watchlist=AsyncMock(return_value=({}, 200)),
        remove_from_watchlist=AsyncMock(return_value=({}, 200)),
        add_hidden_items=AsyncMock(return_value=({}, 200)),
        remove_hidden_items=AsyncMock(return_value=({}, 200)),
    )
    return integration, client


def _run_trakt_delivery(deliver, client, integration, job):
    settings = SimpleNamespace(trakt_client_id="trakt-client", trakt_client_secret="secret")
    with (
        patch(
            "librarysync.jobs.process_outbox.load_integration_with_secrets",
            new=AsyncMock(
                return_value=(integration, {"access_token": "token", "refresh_token": "refresh"})
            ),
        ),
        patch("librarysync.jobs.process_outbox.settings", settings),
        patch("librarysync.jobs.process_outbox.TraktClient", return_value=client),
        patch(
            "librarysync.jobs.process_outbox._ensure_trakt_access_token",
            new=AsyncMock(return_value="token"),
        ),
    ):
        return asyncio.run(deliver(None, job))


def test_trakt_watchlist_remove_delivery_hides_dropped_show() -> None:
    integration, client = _make_trakt_delivery_mocks()
    job = SimpleNamespace(
        user_id="user-1",
        payload={"media_type": "tv", "show_ids": {"tmdb": "1399"}, "hide_dropped": True},
    )

    response_code, external_id = _run_trakt_delivery(
        process_outbox._deliver_trakt_watchlist_remove, client, integration, job
    )

    assert response_code == 200
    assert external_id is None
    client.remove_from_watchlist.assert_awaited_once_with(
        {"shows": [{"ids": {"tmdb": "1399"}}]},
        "token",
    )
    client.add_hidden_items.assert_awaited_once_with(
        "dropped",
        {"shows": [{"ids": {"tmdb": "1399"}}]},
        "token",
    )


def test_trakt_watchlist_remove_delivery_dropped_anime_uses_show_ids() -> None:
    integration, client = _make_trakt_delivery_mocks()
    job = SimpleNamespace(
        user_id="user-1",
        # Anime ids are stored under movie_ids (Trakt add/remove mapping), but
        # the hidden dropped section only accepts show objects.
        payload={"media_type": "anime", "movie_ids": {"tmdb": "1234"}, "hide_dropped": True},
    )

    response_code, _ = _run_trakt_delivery(
        process_outbox._deliver_trakt_watchlist_remove, client, integration, job
    )

    assert response_code == 200
    client.add_hidden_items.assert_awaited_once_with(
        "dropped",
        {"shows": [{"ids": {"tmdb": "1234"}}]},
        "token",
    )


def test_trakt_watchlist_remove_delivery_without_drop_skips_hidden() -> None:
    integration, client = _make_trakt_delivery_mocks()
    job = SimpleNamespace(
        user_id="user-1",
        payload={"media_type": "tv", "show_ids": {"tmdb": "1399"}},
    )

    response_code, _ = _run_trakt_delivery(
        process_outbox._deliver_trakt_watchlist_remove, client, integration, job
    )

    assert response_code == 200
    client.remove_from_watchlist.assert_awaited_once()
    client.add_hidden_items.assert_not_awaited()


def test_trakt_watchlist_add_delivery_unhides_show_from_dropped() -> None:
    integration, client = _make_trakt_delivery_mocks()
    job = SimpleNamespace(
        user_id="user-1",
        payload={"media_type": "tv", "show_ids": {"tmdb": "1399"}, "unhide_dropped": True},
    )

    response_code, _ = _run_trakt_delivery(
        process_outbox._deliver_trakt_watchlist, client, integration, job
    )

    assert response_code == 200
    client.add_to_watchlist.assert_awaited_once_with(
        {"shows": [{"ids": {"tmdb": "1399"}}]},
        "token",
    )
    client.remove_hidden_items.assert_awaited_once_with(
        "dropped",
        {"shows": [{"ids": {"tmdb": "1399"}}]},
        "token",
    )


def test_trakt_watchlist_add_delivery_skips_unhide_without_flag() -> None:
    integration, client = _make_trakt_delivery_mocks()
    job = SimpleNamespace(
        user_id="user-1",
        payload={"media_type": "tv", "show_ids": {"tmdb": "1399"}},
    )

    response_code, _ = _run_trakt_delivery(
        process_outbox._deliver_trakt_watchlist, client, integration, job
    )

    assert response_code == 200
    client.add_to_watchlist.assert_awaited_once()
    client.remove_hidden_items.assert_not_awaited()


def test_trakt_watchlist_add_delivery_skips_unhide_for_movies() -> None:
    integration, client = _make_trakt_delivery_mocks()
    job = SimpleNamespace(
        user_id="user-1",
        payload={"media_type": "movie", "movie_ids": {"tmdb": "550"}, "unhide_dropped": True},
    )

    response_code, _ = _run_trakt_delivery(
        process_outbox._deliver_trakt_watchlist, client, integration, job
    )

    assert response_code == 200
    client.add_to_watchlist.assert_awaited_once()
    client.remove_hidden_items.assert_not_awaited()


def test_trakt_removal_payload_marks_dropped_items() -> None:
    payload = watchlist_sync._build_trakt_removal_payload(
        SimpleNamespace(id="wl-1", type="tv", status="dropped"),
        SimpleNamespace(id="media-1", imdb_id=None, tmdb_id="1399", tvdb_id=None),
    )

    assert payload is not None
    assert payload["hide_dropped"] is True
    assert payload["show_ids"] == {"tmdb": "1399"}


def test_trakt_removal_payload_leaves_regular_items_unmarked() -> None:
    payload = watchlist_sync._build_trakt_removal_payload(
        SimpleNamespace(id="wl-1", type="tv", status="added"),
        SimpleNamespace(id="media-1", imdb_id=None, tmdb_id="1399", tvdb_id=None),
    )

    assert payload is not None
    assert "hide_dropped" not in payload


def _run_simkl_removal_enqueue(item_status: str) -> AsyncMock:
    watchlist_item = SimpleNamespace(id="wl-1", user_id="user-1", type="tv", status=item_status)
    media_item = SimpleNamespace(id="media-1", imdb_id=None, tmdb_id="1399", tvdb_id=None, raw={})
    integration = SimpleNamespace(status="connected", config={})
    enqueue_mock = AsyncMock()

    with (
        patch(
            "librarysync.core.watchlist_sync.load_integration_with_secrets",
            new=AsyncMock(return_value=(integration, {"access_token": "token"})),
        ),
        patch(
            "librarysync.core.watchlist_sync.ensure_personal_watchlist_source",
            new=AsyncMock(return_value=SimpleNamespace(is_enabled=True)),
        ),
        patch("librarysync.core.watchlist_sync.enqueue_outbox_job", new=enqueue_mock),
    ):
        asyncio.run(watchlist_sync._enqueue_simkl_watchlist_removal(None, watchlist_item, media_item))

    return enqueue_mock


def test_simkl_removal_enqueue_marks_dropped_items() -> None:
    enqueue_mock = _run_simkl_removal_enqueue("dropped")

    enqueue_mock.assert_awaited_once()
    payload = enqueue_mock.await_args.kwargs["payload"]
    assert payload["hide_dropped"] is True
    assert payload["show_ids"] == {"tmdb": "1399"}


def test_simkl_removal_enqueue_leaves_regular_items_unmarked() -> None:
    enqueue_mock = _run_simkl_removal_enqueue("added")

    enqueue_mock.assert_awaited_once()
    payload = enqueue_mock.await_args.kwargs["payload"]
    assert "hide_dropped" not in payload


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
