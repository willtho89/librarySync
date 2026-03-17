import asyncio
import json
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import HTTPException

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.append(str(PROJECT_ROOT / "src"))

from librarysync.api import routes_admin  # noqa: E402


def _make_media_item(**overrides):
    values = {
        "id": "media-1",
        "media_type": "tv",
        "title": "Unknown title",
        "year": None,
        "imdb_id": None,
        "tmdb_id": None,
        "tvdb_id": None,
        "tvmaze_id": None,
        "kitsu_id": None,
        "myanimelist_id": None,
        "anilist_id": None,
        "poster_url": None,
        "release_date": None,
        "first_air_date": None,
        "last_air_date": None,
        "runtime_in_seconds": None,
        "genres": None,
        "overview": None,
        "raw": None,
        "metadata_refreshed_at": None,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def _decode_response(response) -> dict:
    return json.loads(response.body.decode("utf-8"))


def _scalar_result(items):
    scalars = MagicMock()
    scalars.all.return_value = items
    result = MagicMock()
    result.scalars.return_value = scalars
    return result


def test_update_media_item_external_ids_requires_payload() -> None:
    db = AsyncMock()

    with pytest.raises(HTTPException) as exc_info:
        asyncio.run(
            routes_admin.update_media_item_external_ids(
                routes_admin.MediaItemExternalIdsUpdateIn(),
                "media-1",
                db=db,
                _="admin-key",
            )
        )

    assert exc_info.value.status_code == 400
    db.commit.assert_not_awaited()


def test_update_media_item_external_ids_merges_conflicts_and_refreshes(monkeypatch) -> None:
    target = _make_media_item(id="media-1")
    conflict = _make_media_item(
        id="media-dup",
        title="Scrubs",
        imdb_id="tt40197357",
    )
    db = AsyncMock()
    merge_calls: list[str] = []

    async def fake_load_media_item_for_update(db, media_item_id):
        assert media_item_id == "media-1"
        return target

    async def fake_find_conflicting_media_item(db, media_item, field, value):
        assert media_item is target
        if field == "imdb_id" and value == "tt40197357":
            return conflict
        return None

    async def fake_merge_media_items(db, media_item, source_item):
        assert media_item is target
        merge_calls.append(source_item.id)

    async def fake_refresh_media_item_metadata(db, media_item):
        assert media_item is target
        return {
            "refreshed": True,
            "providers": ["imdb"],
            "attempted": ["imdb"],
            "errors": [],
        }

    monkeypatch.setattr(
        routes_admin,
        "_load_media_item_for_update",
        fake_load_media_item_for_update,
    )
    monkeypatch.setattr(
        routes_admin,
        "_find_conflicting_media_item",
        fake_find_conflicting_media_item,
    )
    monkeypatch.setattr(routes_admin, "_merge_media_items", fake_merge_media_items)
    monkeypatch.setattr(
        routes_admin,
        "_refresh_media_item_metadata",
        fake_refresh_media_item_metadata,
    )

    response = asyncio.run(
        routes_admin.update_media_item_external_ids(
            routes_admin.MediaItemExternalIdsUpdateIn(imdb_id="TT40197357"),
            "media-1",
            db=db,
            _="admin-key",
        )
    )
    payload = _decode_response(response)

    assert target.imdb_id == "tt40197357"
    assert merge_calls == ["media-dup"]
    assert payload["media_item_id"] == "media-1"
    assert payload["updated_ids"] == {"imdb_id": "tt40197357"}
    assert payload["merged_media_item_ids"] == ["media-dup"]
    assert payload["metadata_refresh"]["refreshed"] is True
    db.flush.assert_awaited_once()
    db.commit.assert_awaited_once()
    db.rollback.assert_not_awaited()


def test_merge_media_item_fields_prefers_existing_ids_and_richer_text() -> None:
    target = _make_media_item(
        title="JD & Turk",
        imdb_id=None,
        tmdb_id="295778",
        raw={"target": True},
    )
    source = _make_media_item(
        title="Scrubs",
        imdb_id="tt40197357",
        tmdb_id=None,
        tvdb_id="465690",
        genres=["Comedy"],
        overview="Sacred Heart returns.",
        raw={"source": True},
    )

    routes_admin._merge_media_item_fields(target, source)

    assert target.imdb_id == "tt40197357"
    assert target.tmdb_id == "295778"
    assert target.tvdb_id == "465690"
    assert target.genres == ["Comedy"]
    assert target.overview == "Sacred Heart returns."
    assert target.raw == {"target": True, "source": True}


def test_merge_episode_items_matches_duplicates_by_external_id() -> None:
    target = _make_media_item(id="show-target")
    source = _make_media_item(id="show-source")
    target_episode = SimpleNamespace(
        id="ep-target",
        show_media_item_id="show-target",
        season_number=1,
        episode_number=1,
        imdb_id=None,
        tmdb_id="101",
        tvdb_id=None,
        tvmaze_id=None,
        title=None,
        air_date=None,
        raw=None,
    )
    source_episode = SimpleNamespace(
        id="ep-source",
        show_media_item_id="show-source",
        season_number=9,
        episode_number=99,
        imdb_id=None,
        tmdb_id="101",
        tvdb_id=None,
        tvmaze_id=None,
        title="Pilot",
        air_date=None,
        raw=None,
    )
    db = AsyncMock()
    db.execute.side_effect = [
        _scalar_result([target_episode, source_episode]),
        None,
        None,
    ]

    asyncio.run(routes_admin._merge_episode_items(db, target, source))

    assert target_episode.title == "Pilot"
    assert source_episode.show_media_item_id == "show-source"
    db.delete.assert_awaited_once_with(source_episode)


def test_repoint_active_provider_watchlist_jobs_updates_media_item_payload() -> None:
    job = SimpleNamespace(
        id="job-1",
        user_id="user-1",
        target_provider="trakt",
        job_type="push_watchlist",
        status="pending",
        payload={"watchlist_item_id": "watchlist-1", "media_item_id": "media-old"},
        dedupe_key="user-1:trakt:push_watchlist:watchlist-1",
    )
    db = AsyncMock()
    db.execute.return_value = _scalar_result([job])

    asyncio.run(
        routes_admin._repoint_active_provider_watchlist_jobs(
            db,
            {"watchlist-1": "watchlist-1"},
            "media-new",
        )
    )

    assert job.payload["media_item_id"] == "media-new"
    assert job.payload["watchlist_item_id"] == "watchlist-1"
    assert job.dedupe_key == "user-1:trakt:push_watchlist:watchlist-1"
    db.delete.assert_not_awaited()


def test_repoint_active_provider_watchlist_jobs_dedupes_merged_watchlist_rows() -> None:
    target_job = SimpleNamespace(
        id="job-target",
        user_id="user-1",
        target_provider="trakt",
        job_type="push_watchlist",
        status="pending",
        payload={"watchlist_item_id": "watchlist-target", "media_item_id": "media-target"},
        dedupe_key="user-1:trakt:push_watchlist:watchlist-target",
    )
    source_job = SimpleNamespace(
        id="job-source",
        user_id="user-1",
        target_provider="trakt",
        job_type="push_watchlist",
        status="pending",
        payload={"watchlist_item_id": "watchlist-source", "media_item_id": "media-source"},
        dedupe_key="user-1:trakt:push_watchlist:watchlist-source",
    )
    db = AsyncMock()
    db.execute.return_value = _scalar_result([target_job, source_job])

    asyncio.run(
        routes_admin._repoint_active_provider_watchlist_jobs(
            db,
            {"watchlist-source": "watchlist-target"},
            "media-target",
        )
    )

    db.delete.assert_awaited_once_with(source_job)
