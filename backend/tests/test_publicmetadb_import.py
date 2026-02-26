import asyncio
import unittest
from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch

from librarysync.core.import_all import DEFAULT_IMPORT_QUEUE_ORDER, normalize_import_queue_order
from librarysync.db.models import Integration
from librarysync.jobs.import_base import ImportContext
from librarysync.jobs.publicmetadb_import import PublicMetaDbImportStrategy


class TestPublicMetaDbImport(unittest.TestCase):
    def test_default_import_queue_order_contains_publicmetadb(self) -> None:
        self.assertIn("publicmetadb", DEFAULT_IMPORT_QUEUE_ORDER)

    def test_normalize_import_queue_accepts_publicmetadb(self) -> None:
        normalized = normalize_import_queue_order(
            ["trakt", "publicmetadb", "SIMKL", "unknown-provider"]
        )
        self.assertEqual(normalized, ["trakt", "publicmetadb", "simkl"])

    def test_import_skips_when_sync_is_disabled(self) -> None:
        strategy = PublicMetaDbImportStrategy(lookback_days=7)
        integration = Integration(
            user_id="user-1",
            provider="publicmetadb",
            status="connected",
            config={"sync_enabled": False},
        )
        now = datetime.now(timezone.utc)
        with patch(
            "librarysync.jobs.publicmetadb_import.load_integration_with_secrets",
            new=AsyncMock(return_value=(integration, {"api_key": "pm-key"})),
        ), patch(
            "librarysync.jobs.publicmetadb_import.PublicMetaDbClient.list_watched",
            new=AsyncMock(return_value=({"items": []}, 200)),
        ) as mocked_list_watched:
            result = asyncio.run(
                strategy.import_for_integration(
                    ImportContext(db=AsyncMock(), now=now),
                    integration,
                    requested_at=None,
                )
            )
        self.assertEqual(result.imported, 0)
        self.assertFalse(result.attempted)
        mocked_list_watched.assert_not_awaited()

    def test_import_processes_watched_items(self) -> None:
        strategy = PublicMetaDbImportStrategy(lookback_days=7)
        integration = Integration(
            user_id="user-2",
            provider="publicmetadb",
            status="connected",
            config={"sync_enabled": True},
        )
        now = datetime.now(timezone.utc)
        payload = {
            "items": [
                {
                    "id": "w_1",
                    "tmdb_id": 550,
                    "media_type": "movie",
                    "title": "Fight Club",
                    "watched_at": now.isoformat(),
                    "score": 80,
                }
            ]
        }
        with patch(
            "librarysync.jobs.publicmetadb_import.load_integration_with_secrets",
            new=AsyncMock(return_value=(integration, {"api_key": "pm-key"})),
        ), patch(
            "librarysync.jobs.publicmetadb_import.PublicMetaDbClient.list_watched",
            new=AsyncMock(return_value=(payload, 200)),
        ), patch(
            "librarysync.jobs.publicmetadb_import.process_import_candidates",
            new=AsyncMock(return_value=1),
        ) as mocked_process:
            result = asyncio.run(
                strategy.import_for_integration(
                    ImportContext(db=AsyncMock(), now=now),
                    integration,
                    requested_at=None,
                )
            )

        self.assertTrue(result.attempted)
        self.assertEqual(result.imported, 1)
        mocked_process.assert_awaited_once()
        args = mocked_process.await_args.args
        self.assertEqual(args[1], "user-2")
        self.assertEqual(args[2], "publicmetadb")
        self.assertEqual(len(args[3]), 1)
        self.assertTrue(args[3][0].entry_key.startswith("publicmetadb:"))


if __name__ == "__main__":
    unittest.main()
