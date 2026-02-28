import asyncio
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from librarysync.jobs import process_outbox


def test_publicmetadb_retry_precheck_skips_duplicate_push_watched() -> None:
    job = SimpleNamespace(
        attempts=2,
        user_id="user-1",
        payload={
            "media_type": "movie",
            "tmdb_id": 550,
            "watched_at": "2026-01-10T12:30:00+00:00",
            "watched_item_id": "watched-1",
        },
    )
    client = SimpleNamespace(
        list_watched=AsyncMock(
            return_value=(
                {
                    "items": [
                        {
                            "id": "pmdb-42",
                            "media_type": "movie",
                            "tmdb_id": 550,
                            "watched_at": "2026-01-10T19:00:00+00:00",
                        }
                    ]
                },
                200,
            )
        ),
        mark_watched=AsyncMock(return_value=({"id": "should-not-be-used"}, 201)),
    )
    with (
        patch(
            "librarysync.jobs.process_outbox._load_publicmetadb_client",
            new=AsyncMock(return_value=(client, "api-key")),
        ),
        patch(
            "librarysync.jobs.process_outbox._sync_local_watched_at",
            new=AsyncMock(),
        ) as sync_mock,
    ):
        response_code, external_id = asyncio.run(
            process_outbox._deliver_publicmetadb_watch(None, job)
        )

    assert response_code == 200
    assert external_id == "pmdb-42"
    client.mark_watched.assert_not_awaited()
    sync_mock.assert_awaited_once_with(
        None,
        job.payload,
        datetime(2026, 1, 10, 19, 0, tzinfo=timezone.utc),
    )


def test_publicmetadb_retry_precheck_falls_through_when_day_differs() -> None:
    job = SimpleNamespace(
        attempts=2,
        user_id="user-1",
        payload={
            "media_type": "movie",
            "tmdb_id": 550,
            "watched_at": "2026-01-11T00:10:00+00:00",
        },
    )
    client = SimpleNamespace(
        list_watched=AsyncMock(
            return_value=(
                {
                    "items": [
                        {
                            "id": "pmdb-42",
                            "media_type": "movie",
                            "tmdb_id": 550,
                            "watched_at": "2026-01-10T23:50:00+00:00",
                        }
                    ]
                },
                200,
            )
        ),
        mark_watched=AsyncMock(return_value=({"id": "pmdb-new"}, 201)),
    )
    with patch(
        "librarysync.jobs.process_outbox._load_publicmetadb_client",
        new=AsyncMock(return_value=(client, "api-key")),
    ):
        response_code, external_id = asyncio.run(
            process_outbox._deliver_publicmetadb_watch(None, job)
        )

    assert response_code == 201
    assert external_id == "pmdb-new"
    client.mark_watched.assert_awaited_once()
