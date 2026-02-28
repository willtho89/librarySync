import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

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
