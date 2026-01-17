import sys
from dataclasses import dataclass
from datetime import date
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.append(str(PROJECT_ROOT / "src"))

from librarysync.core.watchlist import determine_show_watchlist_status  # noqa: E402


@dataclass(frozen=True)
class FakeWatchlistItem:
    item_id: str
    status: str


def _build_tv_item(
    item_id: str,
    *,
    total_released: int,
    watched_count: int,
    now_date: date,
) -> FakeWatchlistItem:
    status = determine_show_watchlist_status(
        total_released=total_released,
        watched_count=watched_count,
        first_air_date=now_date,
        earliest_air_date=now_date,
        now_date=now_date,
    )
    return FakeWatchlistItem(item_id=item_id, status=status)


def _apply_status_filter(
    items: list[FakeWatchlistItem],
    statuses: list[str] | None,
) -> list[FakeWatchlistItem]:
    if statuses:
        return [item for item in items if item.status in statuses]
    return list(items)


def test_tv_watchlist_filters_select_expected_statuses() -> None:
    now = date(2024, 1, 1)
    items = [
        _build_tv_item("added", total_released=10, watched_count=0, now_date=now),
        _build_tv_item("in_progress", total_released=10, watched_count=3, now_date=now),
        _build_tv_item("watched", total_released=10, watched_count=10, now_date=now),
    ]

    assert [item.status for item in items] == ["added", "in_progress", "watched"]
    assert [item.item_id for item in _apply_status_filter(items, ["added"])] == ["added"]
    assert [item.item_id for item in _apply_status_filter(items, ["in_progress"])] == [
        "in_progress"
    ]
    assert [item.item_id for item in _apply_status_filter(items, ["watched"])] == ["watched"]
    assert [item.item_id for item in _apply_status_filter(items, ["added", "in_progress"])] == [
        "added",
        "in_progress",
    ]
