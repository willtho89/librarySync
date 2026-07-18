import asyncio
import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.append(str(PROJECT_ROOT / "src"))

from librarysync.core.import_control import (  # noqa: E402
    QUICK_IMPORT_LEASE_OWNER_KEY,
    QUICK_IMPORT_LEASE_SECONDS,
    QUICK_IMPORT_LEASE_UNTIL_KEY,
    clear_quick_import_lease,
    mark_quick_import_completed,
    mark_quick_import_failed,
    mark_quick_import_lease,
    quick_import_lease_blocked,
)
from librarysync.db.models import Integration  # noqa: E402
from librarysync.jobs import imports  # noqa: E402

NOW = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
WORKER_A = "worker-a"
WORKER_B = "worker-b"


def _leased_config(owner: str, lease_until: datetime) -> dict:
    return {
        "quick_import_status": "in_progress",
        "quick_import_queue": ["trakt", "simkl"],
        "quick_import_index": 1,
        "quick_import_started_at": NOW.isoformat(),
        "merge_required_at": NOW.isoformat(),
        QUICK_IMPORT_LEASE_OWNER_KEY: owner,
        QUICK_IMPORT_LEASE_UNTIL_KEY: lease_until.isoformat(),
    }


def _mock_db(integrations: list[Integration]) -> MagicMock:
    db = MagicMock()
    result = MagicMock()
    result.scalars.return_value.all.return_value = integrations
    db.execute = AsyncMock(return_value=result)
    return db


class TestQuickImportLeaseHelpers(unittest.TestCase):
    def test_lease_not_blocked_without_lease(self) -> None:
        self.assertFalse(quick_import_lease_blocked({}, NOW, WORKER_A))
        self.assertFalse(quick_import_lease_blocked(None, NOW, WORKER_A))

    def test_lease_not_blocked_when_expired(self) -> None:
        config = _leased_config(WORKER_B, NOW - timedelta(seconds=1))
        self.assertFalse(quick_import_lease_blocked(config, NOW, WORKER_A))

    def test_lease_not_blocked_for_same_owner(self) -> None:
        config = _leased_config(WORKER_A, NOW + timedelta(seconds=60))
        self.assertFalse(quick_import_lease_blocked(config, NOW, WORKER_A))

    def test_lease_blocked_for_other_owner(self) -> None:
        config = _leased_config(WORKER_B, NOW + timedelta(seconds=60))
        self.assertTrue(quick_import_lease_blocked(config, NOW, WORKER_A))

    def test_mark_lease_sets_owner_and_expiry(self) -> None:
        updated = mark_quick_import_lease({}, WORKER_A, NOW)
        self.assertEqual(updated[QUICK_IMPORT_LEASE_OWNER_KEY], WORKER_A)
        lease_until = datetime.fromisoformat(updated[QUICK_IMPORT_LEASE_UNTIL_KEY])
        self.assertEqual(lease_until, NOW + timedelta(seconds=QUICK_IMPORT_LEASE_SECONDS))

    def test_completed_clears_lease(self) -> None:
        config = _leased_config(WORKER_A, NOW + timedelta(seconds=60))
        updated = mark_quick_import_completed(config, NOW)
        self.assertNotIn(QUICK_IMPORT_LEASE_OWNER_KEY, updated)
        self.assertNotIn(QUICK_IMPORT_LEASE_UNTIL_KEY, updated)

    def test_failed_clears_lease(self) -> None:
        config = _leased_config(WORKER_A, NOW + timedelta(seconds=60))
        updated = mark_quick_import_failed(config, NOW, "boom")
        self.assertNotIn(QUICK_IMPORT_LEASE_OWNER_KEY, updated)
        self.assertNotIn(QUICK_IMPORT_LEASE_UNTIL_KEY, updated)

    def test_clear_lease_keeps_other_keys(self) -> None:
        config = _leased_config(WORKER_A, NOW + timedelta(seconds=60))
        updated = clear_quick_import_lease(config)
        self.assertEqual(updated["quick_import_status"], "in_progress")
        self.assertEqual(updated["quick_import_index"], 1)


class TestClaimQuickImportRuns(unittest.TestCase):
    def _claim(self, integration: Integration) -> list[Integration]:
        db = _mock_db([integration])
        with (
            patch.object(imports, "worker_instance_id", return_value=WORKER_A),
            patch.object(
                imports, "build_import_all_queue", new=AsyncMock(return_value=["trakt"])
            ),
        ):
            return asyncio.run(imports._claim_quick_import_runs(db, 1))

    def test_skips_run_leased_by_another_worker(self) -> None:
        integration = Integration(
            user_id="user-1",
            provider="system",
            config=_leased_config(WORKER_B, datetime.now(timezone.utc) + timedelta(minutes=5)),
        )
        runs = self._claim(integration)
        self.assertEqual(runs, [])

    def test_resumes_stuck_run_with_expired_lease(self) -> None:
        integration = Integration(
            user_id="user-1",
            provider="system",
            config=_leased_config(WORKER_B, datetime.now(timezone.utc) - timedelta(minutes=5)),
        )
        runs = self._claim(integration)
        self.assertEqual(runs, [integration])
        config = integration.config
        # Run is resumed, not restarted: status and progress are preserved.
        self.assertEqual(config["quick_import_status"], "in_progress")
        self.assertEqual(config["quick_import_index"], 1)
        # Lease is taken over by this worker.
        self.assertEqual(config[QUICK_IMPORT_LEASE_OWNER_KEY], WORKER_A)
        lease_until = datetime.fromisoformat(config[QUICK_IMPORT_LEASE_UNTIL_KEY])
        self.assertGreater(lease_until, datetime.now(timezone.utc))

    def test_claims_run_with_lease_held_by_self(self) -> None:
        integration = Integration(
            user_id="user-1",
            provider="system",
            config=_leased_config(WORKER_A, datetime.now(timezone.utc) + timedelta(minutes=5)),
        )
        runs = self._claim(integration)
        self.assertEqual(runs, [integration])

    def test_starts_scheduled_run_and_takes_lease(self) -> None:
        integration = Integration(
            user_id="user-1",
            provider="system",
            config={
                "quick_import_status": "completed",
                "quick_import_interval_seconds": 1800,
                "quick_import_last_run_at": (
                    datetime.now(timezone.utc) - timedelta(hours=1)
                ).isoformat(),
            },
        )
        runs = self._claim(integration)
        self.assertEqual(runs, [integration])
        config = integration.config
        self.assertEqual(config["quick_import_status"], "in_progress")
        self.assertEqual(config["quick_import_queue"], ["trakt"])
        self.assertEqual(config[QUICK_IMPORT_LEASE_OWNER_KEY], WORKER_A)


if __name__ == "__main__":
    unittest.main()
