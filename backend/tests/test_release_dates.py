import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.append(str(PROJECT_ROOT / "src"))

from librarysync.core.release_dates import get_release_now_date  # noqa: E402


class TestReleaseDates(unittest.TestCase):
    def test_release_date_uses_configured_timezone(self) -> None:
        now = datetime(2026, 3, 18, 1, 30, tzinfo=timezone.utc)

        result = get_release_now_date(now, "America/Los_Angeles")

        self.assertEqual(result.isoformat(), "2026-03-17")

    def test_invalid_timezone_falls_back_to_utc(self) -> None:
        now = datetime(2026, 3, 18, 1, 30, tzinfo=timezone.utc)

        result = get_release_now_date(now, "Not/A_Real_Timezone")

        self.assertEqual(result.isoformat(), "2026-03-18")


if __name__ == "__main__":
    unittest.main()
