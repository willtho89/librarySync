import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from librarysync.jobs import process_outbox


def test_stremio_remove_series_skips_retry_when_cinemeta_has_no_episodes() -> None:
    job = SimpleNamespace(
        user_id="user-1",
        payload={
            "item_id": "series-1",
            "media_type": "tv",
        },
    )
    client = SimpleNamespace(update_library_items=AsyncMock())
    with (
        patch(
            "librarysync.jobs.process_outbox.load_integration_with_secrets",
            new=AsyncMock(
                return_value=(SimpleNamespace(config={}), {"auth_key": "auth-key"})
            ),
        ),
        patch(
            "librarysync.jobs.process_outbox._has_newer_stremio_series_job",
            new=AsyncMock(return_value=False),
        ),
        patch(
            "librarysync.jobs.process_outbox.StremioClient",
            return_value=client,
        ),
        patch(
            "librarysync.jobs.process_outbox.fetch_cinemeta_video_ids",
            new=AsyncMock(return_value=[]),
        ),
    ):
        response_code, external_id = asyncio.run(process_outbox._deliver_stremio_remove(None, job))

    assert response_code == 200
    assert external_id == "series-1"
    client.update_library_items.assert_not_awaited()
