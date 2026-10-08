from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from librarysync.core.watch_state_ratings import RATING_SCOPES
from librarysync.db.models import OutboxJob
from librarysync.jobs import process_outbox as outbox


def test_trakt_title_and_season_ratings_have_their_own_scope():
    payload = {"media_type": "tv", "show_ids": {"imdb": "tt1234567"}, "rating_scope": "series"}
    assert outbox._build_trakt_rating_payload(payload, 9) == {"shows": [{"ids": {"imdb": "tt1234567"}, "rating": 9}]}
    payload.update(rating_scope="season", season_number=2)
    assert outbox._build_trakt_rating_payload(payload, 9) == {
        "shows": [{"ids": {"imdb": "tt1234567"}, "seasons": [{"number": 2, "rating": 9}]}]
    }


def test_simkl_only_advertises_documented_movie_and_show_ratings():
    assert RATING_SCOPES["simkl"] == {"movie", "series"}
    assert outbox._build_simkl_rating_payload(
        {"media_type": "tv", "rating_scope": "series", "show_ids": {"imdb": "tt1234567"}}, 9
    ) == {"shows": [{"ids": {"imdb": "tt1234567"}, "rating": 9}]}


@pytest.mark.asyncio
async def test_trakt_clear_uses_rating_removal_without_history(monkeypatch):
    client = SimpleNamespace(remove_ratings=AsyncMock(return_value=({}, 200)), add_ratings=AsyncMock())
    monkeypatch.setattr(
        outbox,
        "load_integration_with_secrets",
        AsyncMock(return_value=(SimpleNamespace(id="i"), {"access_token": "test", "refresh_token": "test"})),
    )
    monkeypatch.setattr(outbox, "has_required_trakt_fields", lambda _: True)
    monkeypatch.setattr(outbox, "_ensure_trakt_access_token", AsyncMock(return_value="test"))
    monkeypatch.setattr(outbox, "TraktClient", lambda **_: client)
    monkeypatch.setattr(outbox, "settings", SimpleNamespace(trakt_client_id="test", trakt_client_secret="test"))
    job = OutboxJob(
        user_id="u",
        target_provider="trakt",
        job_type="remove_rating",
        payload={"media_type": "movie", "movie_ids": {"imdb": "tt1234567"}},
    )
    await outbox.TraktOutboxHandler().deliver(None, job)
    client.remove_ratings.assert_awaited_once_with({"movies": [{"ids": {"imdb": "tt1234567"}}]}, "test")
    client.add_ratings.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("operation,score", [("push_rating", 8.5), ("remove_rating", 0)])
async def test_anilist_standalone_rating_preserves_watch_progress(monkeypatch, operation, score):
    client = SimpleNamespace(
        get_viewer=AsyncMock(return_value={"id": 42}),
        get_media_list_entry=AsyncMock(return_value={"id": 12, "status": "CURRENT", "progress": 3}),
        add_media_list_entry=AsyncMock(return_value={"id": 12}),
    )
    monkeypatch.setattr(
        outbox, "load_integration_with_secrets", AsyncMock(return_value=(object(), {"access_token": "test"}))
    )
    monkeypatch.setattr(outbox, "has_required_anilist_fields", lambda _: True)
    monkeypatch.setattr(outbox, "AniListClient", lambda **_: client)
    job = OutboxJob(
        user_id="u",
        target_provider="anilist",
        job_type=operation,
        payload={"state_entry_id": "rating", "anilist_id": 1, "protocol_rating": 8.5},
    )
    await outbox.AniListOutboxHandler().deliver(None, job)
    client.add_media_list_entry.assert_awaited_once_with(media_id=1, status="CURRENT", score=score)


@pytest.mark.asyncio
async def test_publicmetadb_series_clear_does_not_create_an_episode_rating(monkeypatch):
    client = SimpleNamespace(delete_rating=AsyncMock(return_value=({}, 200)))
    monkeypatch.setattr(outbox, "_load_publicmetadb_client", AsyncMock(return_value=(client, "test")))
    monkeypatch.setattr(outbox, "_find_publicmetadb_rating_id", AsyncMock(return_value="rating-id"))
    job = OutboxJob(
        user_id="u",
        target_provider="publicmetadb",
        job_type="remove_rating",
        payload={"rating_scope": "series", "media_type": "tv", "tmdb_id": 1},
    )
    await outbox.PublicMetaDbOutboxHandler().deliver(None, job)
    client.delete_rating.assert_awaited_once_with("test", "rating-id")
