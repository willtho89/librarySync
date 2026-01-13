import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.append(str(PROJECT_ROOT / "src"))

from librarysync.core.import_history import update_import_history_merge  # noqa: E402


class TestImportHistoryMerge(unittest.TestCase):
    def test_update_import_history_merge_updates_matching_entry(self) -> None:
        required_at = datetime(2024, 1, 2, tzinfo=timezone.utc)
        completed_at = datetime(2024, 1, 3, tzinfo=timezone.utc)
        config = {
            "import_history": [
                {
                    "id": "older",
                    "merge_required_at": "2023-01-01T00:00:00+00:00",
                    "merge_completed_at": None,
                    "merge_error": None,
                },
                {
                    "id": "target",
                    "merge_required_at": required_at.isoformat(),
                    "merge_completed_at": None,
                    "merge_error": None,
                },
            ]
        }

        updated = update_import_history_merge(config, required_at, completed_at, "merge failed")
        target = next(
            entry for entry in updated["import_history"] if entry.get("id") == "target"
        )
        self.assertEqual(target["merge_completed_at"], completed_at.isoformat())
        self.assertEqual(target["merge_error"], "merge failed")

        older = next(
            entry for entry in updated["import_history"] if entry.get("id") == "older"
        )
        self.assertIsNone(older["merge_completed_at"])
        self.assertIsNone(older["merge_error"])

    def test_update_import_history_merge_no_history(self) -> None:
        required_at = datetime(2024, 1, 2, tzinfo=timezone.utc)
        config = {"other": "value"}

        updated = update_import_history_merge(config, required_at, None, None)
        self.assertEqual(updated, config)
