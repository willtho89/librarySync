import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.append(str(PROJECT_ROOT / "src"))

from librarysync.core import watch_pipeline  # noqa: E402


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
