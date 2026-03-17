import asyncio
from dataclasses import replace
from datetime import date
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from librarysync.api import routes_dashboard


class FakeResult:
    def __init__(self, *, row=None, rows=None, scalar_value=None) -> None:
        self._row = row
        self._rows = list(rows or [])
        self._scalar_value = scalar_value

    def fetchone(self):
        return self._row

    def fetchall(self):
        return list(self._rows)

    def scalar(self):
        return self._scalar_value


class FakeDB:
    def __init__(self, *results: FakeResult) -> None:
        self._results = list(results)
        self.calls: list[tuple[str, dict | None]] = []

    async def execute(self, query, params=None):
        self.calls.append((str(query), params))
        if not self._results:
            raise AssertionError("Unexpected execute() call")
        return self._results.pop(0)


def _current_user():
    return SimpleNamespace(id="user-1")


def test_get_dashboard_stats_rejects_when_disabled(monkeypatch) -> None:
    monkeypatch.setattr(
        routes_dashboard,
        "settings",
        replace(routes_dashboard.settings, enable_dashboard_stats=False),
    )
    db = FakeDB()

    with pytest.raises(HTTPException) as exc_info:
        asyncio.run(routes_dashboard.get_dashboard_stats(current_user=_current_user(), db=db))

    assert exc_info.value.status_code == 403
    assert db.calls == []


def test_get_dashboard_stats_returns_empty_defaults(monkeypatch) -> None:
    monkeypatch.setattr(
        routes_dashboard,
        "settings",
        replace(routes_dashboard.settings, enable_dashboard_stats=True),
    )
    db = FakeDB(
        FakeResult(row=None),
        FakeResult(rows=[]),
        FakeResult(rows=[]),
        FakeResult(row=None),
        FakeResult(row=None),
        FakeResult(scalar_value=0),
        FakeResult(scalar_value=0),
        FakeResult(rows=[]),
        FakeResult(rows=[]),
    )

    payload = asyncio.run(routes_dashboard.get_dashboard_stats(current_user=_current_user(), db=db))

    assert payload["user_stats"] == {
        "movies_watched": 0,
        "episodes_watched": 0,
        "shows_watched": 0,
        "items_rated": 0,
        "avg_rating": 0,
        "first_watch_date": None,
        "last_watch_date": None,
        "total_watch_days": 0,
    }
    assert payload["daily_activity"] == []
    assert payload["rating_distribution"] == []
    assert payload["integration_summary"] == {
        "total_integrations": 0,
        "configured_integrations": 0,
        "providers": [],
    }
    assert payload["system_stats"] == {
        "total_media_items": 0,
        "total_episode_items": 0,
        "total_integrations": 0,
        "total_sync_events": 0,
        "total_users": 0,
        "total_watched_items": 0,
        "active_users": 0,
    }
    assert payload["activity_summary"] == {
        "last_7_days": 0,
        "last_30_days": 0,
    }
    assert payload["overall_daily_activity"] == []
    assert payload["overall_rating_distribution"] == []
    assert len(db.calls) == 9


def test_get_dashboard_stats_serializes_populated_rows(monkeypatch) -> None:
    monkeypatch.setattr(
        routes_dashboard,
        "settings",
        replace(routes_dashboard.settings, enable_dashboard_stats=True),
    )
    db = FakeDB(
        FakeResult(
            row=SimpleNamespace(
                movies_watched=12,
                episodes_watched=34,
                shows_watched=5,
                items_rated=20,
                avg_rating=4.25,
                first_watch_date=date(2024, 1, 10),
                last_watch_date=date(2024, 3, 5),
                total_watch_days=18,
            )
        ),
        FakeResult(
            rows=[
                SimpleNamespace(
                    watch_date=date(2024, 3, 1),
                    movies_count=2,
                    episodes_count=3,
                )
            ]
        ),
        FakeResult(rows=[SimpleNamespace(rating_bucket=4.5, count=7)]),
        FakeResult(
            row=SimpleNamespace(
                integration_count=4,
                configured_count=3,
                providers=["trakt", "simkl"],
            )
        ),
        FakeResult(
            row=SimpleNamespace(
                total_media_items=100,
                total_episode_items=250,
                total_integrations=9,
                total_sync_events=450,
                total_users=2,
                total_watched_items=99,
                active_users=2,
            )
        ),
        FakeResult(scalar_value=6),
        FakeResult(scalar_value=15),
        FakeResult(
            rows=[
                SimpleNamespace(
                    watch_date=date(2024, 3, 2),
                    movies_count=1,
                    episodes_count=4,
                )
            ]
        ),
        FakeResult(rows=[SimpleNamespace(rating_bucket=3.5, count=9)]),
    )

    payload = asyncio.run(routes_dashboard.get_dashboard_stats(current_user=_current_user(), db=db))

    assert payload["user_stats"]["movies_watched"] == 12
    assert payload["user_stats"]["avg_rating"] == 4.25
    assert payload["user_stats"]["first_watch_date"] == date(2024, 1, 10)
    assert payload["user_stats"]["last_watch_date"] == date(2024, 3, 5)
    assert payload["daily_activity"] == [
        {"date": "2024-03-01", "movies": 2, "episodes": 3}
    ]
    assert payload["rating_distribution"] == [{"rating": 4.5, "count": 7}]
    assert payload["integration_summary"] == {
        "total_integrations": 4,
        "configured_integrations": 3,
        "providers": ["trakt", "simkl"],
    }
    assert payload["system_stats"]["total_media_items"] == 100
    assert payload["activity_summary"] == {"last_7_days": 6, "last_30_days": 15}
    assert payload["overall_daily_activity"] == [
        {"date": "2024-03-02", "movies": 1, "episodes": 4}
    ]
    assert payload["overall_rating_distribution"] == [{"rating": 3.5, "count": 9}]
