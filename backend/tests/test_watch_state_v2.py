"""Watch State v2 contract tests, written before the implementation."""

from datetime import datetime, timezone

import pytest
from librarysync.db.models import EpisodeItem, MediaItem, WatchedItem, WatchlistItem
from librarysync.jobs.watch_state import drain_watch_state
from sqlalchemy import event, func, select
from test_addon_watch_state import AT, BASE, movie_event

pytest_plugins = ("test_addon_watch_state",)


async def push(client, name, **changes):
    changes.setdefault("id", f"{name}-{changes.get('at', AT)}")
    body = movie_event(name, **changes)
    return await client.post(f"{BASE}/watch_state/push/movie/tt0111161.json", json=body)


async def pull(client, **params):
    response = await client.get(f"{BASE}/watch_state/pull.json", params=params)
    assert response.status_code == 200
    return response.json()


@pytest.mark.asyncio
async def test_manifest_advertises_complete_contract(context):
    client, _, _ = context
    state = (await client.get(f"{BASE}/manifest.json")).json()["watchState"]
    assert set(state["push"]["events"]) == {
        "start",
        "pause",
        "stop",
        "played",
        "unplayed",
        "watchlisted",
        "unwatchlisted",
        "dropped",
        "undropped",
        "rated",
        "unrated",
    }
    assert state["push"]["bulk"] is True
    assert state["viewers"] is True
    assert all(state["pull"][key] for key in ["items", "watched", "watchlist", "ratings"])


@pytest.mark.asyncio
async def test_pause_resume_and_completion_are_distinct(context):
    client, db, _ = context
    assert (await push(client, "pause", played=False, positionMs=120000, durationMs=600000)).status_code == 204
    paused = await pull(client)
    assert paused["items"][0]["positionMs"] == 120000
    assert paused["items"][0]["progressPercent"] == 20
    assert paused["items"][0]["played"] is False
    assert await db.scalar(select(func.count()).select_from(WatchedItem)) == 0
    assert (await push(client, "start", at=AT + 1, positionMs=120000)).status_code == 204
    assert (await pull(client))["items"] == []
    assert (await push(client, "stop", at=AT + 2, positionMs=550000, durationMs=600000)).status_code == 204
    assert (await pull(client))["watched"]["movies"] == ["tt0111161"]
    assert await db.scalar(select(func.count()).select_from(WatchedItem)) == 1


@pytest.mark.asyncio
async def test_unknown_duration_preserves_position_without_guessing(context):
    client, db, _ = context
    assert (await push(client, "stop", played=False, positionMs=812160)).status_code == 204
    item = (await pull(client))["items"][0]
    assert item["positionMs"] == 812160
    assert "progressPercent" not in item and "durationMs" not in item
    assert await db.scalar(select(func.count()).select_from(WatchedItem)) == 0


@pytest.mark.asyncio
async def test_progress_does_not_invalidate_watched_snapshot(context):
    client, _, _ = context
    await push(client, "played")
    before = await pull(client)
    await push(client, "pause", at=AT + 1, positionMs=60000, durationMs=600000, played=False)
    after = await pull(client, since=before["version"])
    assert after["version"] == before["version"]
    assert "watched" not in after
    assert after["items"][0]["played"] is False


@pytest.mark.asyncio
async def test_unchanged_pull_never_scans_history_or_next_episodes(context):
    client, db, _ = context
    await push(client, "played")
    full = await pull(client)
    statements = []

    def capture(conn, cursor, statement, params, execution_context, executemany):
        if statement.lstrip().lower().startswith("select"):
            statements.append(statement.lower())

    engine = db.bind.sync_engine
    event.listen(engine, "before_cursor_execute", capture)
    try:
        unchanged = await pull(client, since=full["version"])
    finally:
        event.remove(engine, "before_cursor_execute", capture)
    assert unchanged["version"] == full["version"]
    assert not any("watched_items" in sql or "episode_items" in sql for sql in statements), statements


@pytest.mark.asyncio
async def test_external_history_edits_invalidate_cached_snapshot(context):
    client, db, _ = context
    before = await pull(client)
    media = MediaItem(media_type="movie", title="Imported", imdb_id="tt9999999")
    db.add(media)
    await db.flush()
    watched = WatchedItem(user_id="user-one", media_item_id=media.id, watched_at=datetime.now(timezone.utc))
    db.add(watched)
    await db.commit()
    after = await pull(client, since=before["version"])
    assert after["version"] != before["version"]
    assert after["watched"]["movies"] == ["tt9999999"]
    await db.delete(watched)
    await db.commit()
    removed = await pull(client, since=after["version"])
    assert removed["watched"]["movies"] == []


@pytest.mark.asyncio
async def test_watchlist_roundtrip_and_removal(context):
    client, db, _ = context
    assert (await push(client, "watchlisted")).status_code == 204
    full = await pull(client)
    assert full["watchlist"] == [{"type": "movie", "metaId": "tt0111161", "at": AT}]
    assert await db.scalar(select(func.count()).select_from(WatchedItem)) == 0
    assert (await push(client, "unwatchlisted", at=AT + 1)).status_code == 204
    after = await pull(client, since=full["version"])
    assert after["watchlist"] == []
    assert after["version"] != full["version"]


@pytest.mark.asyncio
async def test_drop_roundtrip_and_new_play_undrops(context):
    client, db, _ = context
    body = {"id": "drop", "event": "dropped", "scope": "series", "at": AT, "metaId": "tt0903747"}
    url = f"{BASE}/watch_state/push/series/tt0903747.json"
    assert (await client.post(url, json=body)).status_code == 204
    assert (await pull(client))["watched"]["dropped"] == ["tt0903747"]
    item = await db.scalar(select(WatchlistItem))
    assert item.status == "dropped"
    body.update(id="undrop", event="undropped", at=AT + 1)
    assert (await client.post(url, json=body)).status_code == 204
    assert (await pull(client))["watched"]["dropped"] == []


@pytest.mark.asyncio
@pytest.mark.parametrize("rating", [0, 0.5, 8.5, 10])
async def test_ratings_without_watches_preserve_protocol_precision(context, rating):
    client, db, _ = context
    assert (await push(client, "rated", rating=rating)).status_code == 204
    full = await pull(client)
    assert full["ratings"] == [{"type": "movie", "metaId": "tt0111161", "rating": rating, "at": AT}]
    assert await db.scalar(select(func.count()).select_from(WatchedItem)) == 0
    assert (await push(client, "unrated", at=AT + 1)).status_code == 204
    assert (await pull(client, since=full["version"]))["ratings"] == []


@pytest.mark.asyncio
async def test_state_categories_do_not_suppress_each_other(context):
    client, _, _ = context
    assert (await push(client, "rated", rating=8.5, at=AT + 100)).status_code == 204
    assert (await push(client, "played", at=AT)).status_code == 204
    full = await pull(client)
    assert full["ratings"][0]["rating"] == 8.5
    assert full["watched"]["movies"] == ["tt0111161"]


@pytest.mark.asyncio
async def test_bulk_mark_retry_and_delayed_bulk_cannot_override_newer_mark(context):
    client, db, _ = context
    body = {
        "id": "bulk-part-1",
        "event": "played",
        "scope": "season",
        "at": AT,
        "metaId": "tt0903747",
        "season": 1,
        "part": 1,
        "parts": 2,
        "videos": [
            {"videoId": "tt0903747:1:1", "season": 1, "episode": 1},
            {"videoId": "tt0903747:0:1", "season": 0, "episode": 1},
        ],
    }
    url = f"{BASE}/watch_state/push/series/tt0903747.json"
    for _ in range(2):
        assert (await client.post(url, json=body)).status_code == 204
    assert await db.scalar(select(func.count()).select_from(WatchedItem)) == 0
    await drain_watch_state(db)
    assert await db.scalar(select(func.count()).select_from(WatchedItem)) == 2
    clear = {
        "id": "newer-single",
        "event": "unplayed",
        "at": AT + 10,
        "metaId": body["metaId"],
        "videoId": "tt0903747:1:1",
        "season": 1,
        "episode": 1,
    }
    assert (await client.post(f"{BASE}/watch_state/push/series/tt0903747:1:1.json", json=clear)).status_code == 204
    body["id"] = "delayed-bulk"
    assert (await client.post(url, json=body)).status_code == 204
    await drain_watch_state(db)
    assert (await pull(client))["watched"]["episodes"] == ["tt0903747:0:1"]


@pytest.mark.asyncio
async def test_absolute_numbering_is_accepted_without_fake_canonical_episode(context):
    client, db, _ = context
    body = {
        "id": "absolute",
        "event": "played",
        "at": AT,
        "metaId": "kitsu:42323",
        "videoId": "kitsu:42323:7",
        "season": None,
        "episode": 7,
    }
    assert (await client.post(f"{BASE}/watch_state/push/series/kitsu:42323:7.json", json=body)).status_code == 204
    full = await pull(client)
    assert full["watched"]["episodes"] == ["kitsu:42323:7"]
    assert await db.scalar(select(func.count()).select_from(EpisodeItem)) == 0


@pytest.mark.asyncio
async def test_viewer_binding_requires_target_user_acceptance(context):
    client, _, _ = context
    response = await client.post("/api/stremio-addon/watch-state/viewers", json={"viewer": "sam"})
    assert response.status_code == 201
    token = response.json()["invitation"]
    assert (await client.get(f"{BASE}/watch_state/pull.json", params={"viewer": "sam"})).status_code == 404
    accepted = await client.post("/api/stremio-addon/watch-state/viewers/accept", json={"invitation": token})
    assert accepted.status_code == 200
    assert (await client.get(f"{BASE}/watch_state/pull.json", params={"viewer": "sam"})).status_code == 200


@pytest.mark.asyncio
async def test_diagnostics_explain_accepted_and_duplicate_events_without_credentials(context):
    client, _, _ = context
    for _ in range(2):
        await push(client, "played")
    response = await client.get("/api/stremio-addon/watch-state/status")
    assert response.status_code == 200
    status = response.json()
    assert status["last_received_at"] is not None
    assert status["events"][0]["duplicates"] == 1
    assert status["events"][0]["status"] == "applied"
    assert status["deliveries"] == []  # Internal metadata jobs are not connected-service deliveries.
    assert "addon-one" not in response.text


@pytest.mark.asyncio
async def test_unknown_clear_remains_a_tombstone_after_title_is_resolved(context):
    client, db, _ = context
    assert (await push(client, "unplayed", at=AT + 10)).status_code == 204
    assert (await push(client, "played", at=AT)).status_code == 204
    assert (await pull(client))["watched"]["movies"] == []
    assert await db.scalar(select(func.count()).select_from(WatchedItem)) == 0


@pytest.mark.asyncio
async def test_bound_viewer_has_isolated_history_and_can_revoke_consent(context):
    client, db, _ = context
    from librarysync.api.deps import get_current_user
    from librarysync.db.models import User

    invitation = (await client.post("/api/stremio-addon/watch-state/viewers", json={"viewer": "sam"})).json()
    app = client._transport.app

    async def other_user():
        return await db.get(User, "user-two")

    app.dependency_overrides[get_current_user] = other_user
    assert (
        await client.post(
            "/api/stremio-addon/watch-state/viewers/accept",
            json={
                "invitation": invitation["invitation"],
            },
        )
    ).status_code == 200
    url = f"{BASE}/watch_state/push/movie/tt0111161.json"
    assert (await client.post(url, params={"viewer": "sam"}, json=movie_event())).status_code == 204
    assert (await pull(client))["watched"]["movies"] == []
    assert (await pull(client, viewer="sam"))["watched"]["movies"] == ["tt0111161"]
    status = (await client.get("/api/stremio-addon/watch-state/status")).json()
    assert len(status["events"]) == 1 and status["events"][0]["viewer"] == "sam"
    binding_id = status["viewers"][0]["id"]
    assert (await client.delete(f"/api/stremio-addon/watch-state/viewers/{binding_id}")).status_code == 204
    assert (await client.get(f"{BASE}/watch_state/pull.json", params={"viewer": "sam"})).status_code == 404


@pytest.mark.asyncio
async def test_cached_snapshot_invalidates_on_bulk_sql_delete_and_metadata_change(context):
    client, db, _ = context
    from sqlalchemy import delete

    await push(client, "played")
    before = await pull(client)
    media = await db.scalar(select(MediaItem))
    media.imdb_id = "tt1234567"
    media.raw = {"stremio_id": "tt1234567"}
    await db.commit()
    after = await pull(client, since=before["version"])
    assert after["version"] != before["version"]
    assert after["watched"]["movies"] == ["tt1234567"]
    await db.execute(delete(WatchedItem).where(WatchedItem.user_id == "user-one"))
    await db.commit()
    cleared = await pull(client, since=after["version"])
    assert cleared["watched"]["movies"] == []


@pytest.mark.asyncio
async def test_unfavouriting_preserves_another_source_membership(context):
    client, db, _ = context
    from librarysync.core.watchlist_sources import ensure_manual_watchlist_source, upsert_watchlist_source_item

    await push(client, "watchlisted")
    item = await db.scalar(select(WatchlistItem))
    manual = await ensure_manual_watchlist_source(db, "user-one")
    await upsert_watchlist_source_item(db, manual, item)
    await db.commit()
    await push(client, "unwatchlisted", at=AT + 1)
    assert (await pull(client))["watchlist"][0]["metaId"] == "tt0111161"


@pytest.mark.asyncio
async def test_rating_clear_replaces_pending_set_operation(context):
    client, db, _ = context
    from librarysync.db.models import Integration, OutboxJob

    db.add(Integration(user_id="user-one", provider="trakt", status="connected"))
    await db.commit()
    await push(client, "rated", rating=8.5)
    await push(client, "unrated", at=AT + 1)
    jobs = list((await db.scalars(select(OutboxJob).where(OutboxJob.target_provider == "trakt"))).all())
    assert len(jobs) == 1
    assert jobs[0].job_type == "remove_rating"


@pytest.mark.asyncio
async def test_new_rating_is_not_lost_while_previous_delivery_is_in_progress(context):
    client, db, _ = context
    from librarysync.db.models import Integration, OutboxJob

    db.add(Integration(user_id="user-one", provider="trakt", status="connected"))
    await db.commit()
    await push(client, "rated", rating=8.5)
    job = await db.scalar(select(OutboxJob).where(OutboxJob.target_provider == "trakt"))
    job.status = "in_progress"
    await db.commit()
    await push(client, "rated", rating=2, at=AT + 1)
    jobs = list((await db.scalars(select(OutboxJob).where(OutboxJob.target_provider == "trakt"))).all())
    assert len(jobs) == 2
    assert sorted(j.payload["rating"] for j in jobs) == [1, 4.5]


@pytest.mark.asyncio
async def test_legacy_ratings_are_pulled_but_explicit_clear_wins(context):
    client, db, _ = context
    await push(client, "played")
    watched = await db.scalar(select(WatchedItem))
    watched.rating = 4.5
    await db.commit()
    assert (await pull(client))["ratings"][0]["rating"] == 9
    await push(client, "unrated", at=AT + 1)
    assert (await pull(client))["ratings"] == []


@pytest.mark.asyncio
async def test_clear_prevents_delayed_provider_import_from_restoring_history(context):
    client, db, _ = context
    from librarysync.jobs.import_pipeline import ImportCandidate, ImportItems, process_import_candidates

    await push(client, "played")
    media = await db.scalar(select(MediaItem))
    await push(client, "unplayed", at=AT + 10)

    async def build_items(_):
        return ImportItems(media, None, None)

    candidate = ImportCandidate(
        "external-old",
        datetime.fromtimestamp(AT, timezone.utc),
        "movie",
        {},
        None,
        None,
        None,
        False,
        False,
        build_items,
    )
    count = await process_import_candidates(db, "user-one", "trakt", [candidate])
    assert count == 0
    assert await db.scalar(select(func.count()).select_from(WatchedItem)) == 0


@pytest.mark.asyncio
async def test_same_event_id_for_two_profiles_bound_to_same_user_does_not_stick(context):
    client, db, _ = context
    invite = (await client.post("/api/stremio-addon/watch-state/viewers", json={"viewer": "sam"})).json()
    await client.post("/api/stremio-addon/watch-state/viewers/accept", json={"invitation": invite["invitation"]})
    await push(client, "played")
    body = movie_event("played", id="played-" + str(AT))
    response = await client.post(f"{BASE}/watch_state/push/movie/tt0111161.json", params={"viewer": "sam"}, json=body)
    assert response.status_code == 204
    assert "application/json" not in response.headers.get("content-type", "")
    events = (await client.get("/api/stremio-addon/watch-state/status")).json()["events"]
    assert len(events) == 2
    assert {row["status"] for row in events} == {"applied"}


@pytest.mark.asyncio
async def test_absolute_episode_mapping_can_be_repaired_and_retried(context):
    client, db, _ = context
    body = {
        "id": "repair-me",
        "event": "played",
        "at": AT,
        "metaId": "kitsu:7",
        "videoId": "kitsu:7:15",
        "season": None,
        "episode": 15,
    }
    await client.post(f"{BASE}/watch_state/push/series/kitsu:7:15.json", json=body)
    receipt = (await client.get("/api/stremio-addon/watch-state/status")).json()["events"][0]
    assert receipt["error"]
    media = await db.scalar(select(MediaItem))
    episode = EpisodeItem(show_media_item_id=media.id, season_number=2, episode_number=3)
    db.add(episode)
    await db.commit()
    resolved = await client.post(
        f"/api/stremio-addon/watch-state/events/{receipt['id']}/resolve",
        json={"video_id": "kitsu:7:15", "media_item_id": media.id, "episode_item_id": episode.id},
    )
    assert resolved.status_code == 200
    await drain_watch_state(db)
    assert await db.scalar(select(func.count()).select_from(WatchedItem)) == 1
    assert (await pull(client))["watched"]["episodes"] == ["kitsu:7:15"]
    body.update(id="clear-repaired", event="unplayed", at=AT + 1)
    assert (await client.post(f"{BASE}/watch_state/push/series/kitsu:7:15.json", json=body)).status_code == 204
    assert await db.scalar(select(func.count()).select_from(WatchedItem)) == 0
    assert (await pull(client))["watched"]["episodes"] == []


@pytest.mark.asyncio
async def test_private_diagnostics_include_resume_and_ratings_without_counting_as_a_pull(context):
    client, _, _ = context
    await push(client, "pause", positionMs=120000, played=False)
    status = (await client.get("/api/stremio-addon/watch-state/status")).json()
    assert status["last_pulled_at"] is None
    assert status["resume"][0]["positionMs"] == 120000
    resume_id = status["resume"][0]["id"]
    assert (await client.delete(f"/api/stremio-addon/watch-state/resume/{resume_id}")).status_code == 204
    assert (await pull(client))["items"] == []


@pytest.mark.asyncio
async def test_private_rating_management_does_not_create_watches(context):
    client, db, _ = context
    body = movie_event("rated", rating=8.5)
    response = await client.post("/api/stremio-addon/watch-state/ratings/movie", json=body)
    assert response.status_code == 204
    assert (await pull(client))["ratings"][0]["rating"] == 8.5
    assert await db.scalar(select(func.count()).select_from(WatchedItem)) == 0
    body.update(event="unrated", rating=None)
    assert (await client.post("/api/stremio-addon/watch-state/ratings/movie", json=body)).status_code == 204
    assert (await pull(client))["ratings"] == []


@pytest.mark.asyncio
async def test_superseded_rating_is_not_sent_to_provider(context):
    client, db, _ = context
    from unittest.mock import AsyncMock

    from librarysync.db.models import Integration, OutboxJob
    from librarysync.jobs.process_outbox import OutboxDispatcher, OutboxHandlerRegistry

    db.add(Integration(user_id="user-one", provider="trakt", status="connected"))
    await db.commit()
    await push(client, "rated", rating=8.5)
    job = await db.scalar(select(OutboxJob).where(OutboxJob.target_provider == "trakt"))
    job.status = "in_progress"
    await db.commit()
    await push(client, "unrated", at=AT + 1)
    handler = AsyncMock()
    handler.provider = "trakt"
    dispatcher = OutboxDispatcher(OutboxHandlerRegistry([handler]))
    await dispatcher.deliver(db, job)
    handler.deliver.assert_not_awaited()


@pytest.mark.asyncio
async def test_show_counts_include_every_known_alias_and_absolute_videos(context):
    client, _, _ = context
    body = {
        "id": "alias-count",
        "event": "played",
        "at": AT,
        "metaId": "tt1234567",
        "videoId": "kitsu:7:15",
        "season": None,
        "episode": 15,
        "ids": {"tmdb": "123", "kitsu": "7"},
    }
    await client.post(f"{BASE}/watch_state/push/series/kitsu:7:15.json", json=body)
    counts = (await pull(client))["watched"]["counts"]
    for alias in ["tt1234567", "tmdb:123", "kitsu:7"]:
        assert counts[alias] == {"watched": 1, "total": 0}


@pytest.mark.asyncio
async def test_manual_rating_update_overrides_protocol_rating(context):
    client, db, _ = context
    from librarysync.api.routes_history import router as history_router

    client._transport.app.include_router(history_router)
    await push(client, "played")
    await push(client, "rated", rating=8.5)
    watched = await db.scalar(select(WatchedItem))
    response = await client.patch(f"/api/history/items/{watched.id}", json={"rating": 2})
    assert response.status_code == 200
    assert (await pull(client))["ratings"][0]["rating"] == 4


@pytest.mark.asyncio
async def test_manual_backdated_watch_overrides_clear_and_removes_resume_point(context):
    client, _, _ = context
    from librarysync.api.routes_history import router as history_router

    client._transport.app.include_router(history_router)
    await push(client, "played")
    await push(client, "unplayed", at=AT + 1)
    await push(client, "pause", positionMs=60000, played=False, at=AT + 2)
    response = await client.post(
        "/api/history/items",
        json={
            "imdb_id": "tt0111161",
            "title": "The Shawshank Redemption",
            "media_type": "movie",
            "watched_at": datetime.fromtimestamp(AT - 100, timezone.utc).isoformat(),
        },
    )
    assert response.status_code == 201
    state = await pull(client)
    assert state["watched"]["movies"] == ["tt0111161"]
    assert not any(not row["played"] for row in state["items"])


@pytest.mark.asyncio
async def test_reused_position_event_id_can_start_a_new_playback_session(context):
    client, db, _ = context
    await push(client, "start", id="same-start", at=AT, positionMs=0)
    await push(client, "stop", id="same-stop", at=AT + 100, positionMs=500000, played=True)
    await push(client, "start", id="same-start", at=AT + 200, positionMs=0)
    assert (await pull(client))["items"] == []
    await push(client, "stop", id="same-stop", at=AT + 300, positionMs=500000, played=True)
    assert await db.scalar(select(func.count()).select_from(WatchedItem)) == 2
    # The previous session's delayed retry remains a duplicate after the new session.
    await push(client, "stop", id="same-stop", at=AT + 100, positionMs=500000, played=True)
    assert await db.scalar(select(func.count()).select_from(WatchedItem)) == 2


@pytest.mark.asyncio
async def test_mapped_episode_rating_uses_canonical_numbers_for_provider_delivery(context):
    from librarysync.db.models import Integration, OutboxJob

    client, db, _ = context
    media = MediaItem(media_type="anime", title="Mapped anime", kitsu_id="7", imdb_id="tt1234567")
    db.add(media)
    await db.flush()
    episode = EpisodeItem(show_media_item_id=media.id, season_number=2, episode_number=3)
    db.add_all([episode, Integration(user_id="user-one", provider="trakt", status="connected")])
    await db.commit()
    body = {
        "id": "map-rating",
        "event": "rated",
        "at": AT,
        "rating": 8.5,
        "metaId": "kitsu:7",
        "videoId": "kitsu:7:15",
        "season": None,
        "episode": 15,
    }
    await client.post(f"{BASE}/watch_state/push/series/kitsu:7:15.json", json=body)
    receipt = (await client.get("/api/stremio-addon/watch-state/status")).json()["events"][0]
    response = await client.post(
        f"/api/stremio-addon/watch-state/events/{receipt['id']}/resolve",
        json={"video_id": "kitsu:7:15", "media_item_id": media.id, "episode_item_id": episode.id},
    )
    assert response.status_code == 200
    await drain_watch_state(db)
    job = await db.scalar(select(OutboxJob).where(OutboxJob.status == "pending"))
    assert job.payload["season_number"] == 2
    assert job.payload["episode_number"] == 3
    assert (await pull(client))["ratings"][0]["videoId"] == "kitsu:7:15"


@pytest.mark.asyncio
async def test_provider_refresh_cannot_remove_an_aiostreams_drop(context):
    from librarysync.core.watchlist_sources import (
        ensure_dropped_watchlist_source,
        reconcile_dropped_source,
        upsert_watchlist_source_item,
    )

    client, db, _ = context
    body = {"id": "drop", "event": "dropped", "scope": "series", "at": AT, "metaId": "tt0903747"}
    url = f"{BASE}/watch_state/push/series/tt0903747.json"
    await client.post(url, json=body)
    item = await db.scalar(select(WatchlistItem))
    source = await ensure_dropped_watchlist_source(db, "user-one", "trakt", name="Trakt dropped")
    await upsert_watchlist_source_item(db, source, item)
    await db.commit()
    await reconcile_dropped_source(db, source, now=datetime.now(timezone.utc), seen_item_ids=[])
    assert (await pull(client))["watched"]["dropped"] == ["tt0903747"]


@pytest.mark.asyncio
async def test_reused_pause_id_with_new_timestamp_is_a_new_event_without_a_start(context):
    client, _, _ = context
    # AIOStreams can coalesce the pending start before it delivers a pause.
    await push(client, "pause", id="position-pause", at=AT, played=False, positionMs=120000)
    await push(client, "unplayed", at=AT + 1)
    await push(client, "pause", id="position-pause", at=AT + 2, played=False, positionMs=120000)
    assert (await pull(client))["items"][0]["positionMs"] == 120000


@pytest.mark.asyncio
async def test_queued_history_rating_cannot_overwrite_independent_clear(context, monkeypatch):
    from unittest.mock import AsyncMock

    from librarysync.db.models import OutboxJob
    from librarysync.jobs import process_outbox as outbox

    client, db, _ = context
    await push(client, "played")
    watched = await db.scalar(select(WatchedItem))
    job = OutboxJob(
        user_id="user-one",
        target_provider="trakt",
        job_type="push_rating",
        payload={"watched_item_id": watched.id, "rating": 4.5},
        status="in_progress",
        attempts=0,
    )
    db.add(job)
    await db.commit()
    await push(client, "unrated", at=AT + 1)
    handler = AsyncMock()
    handler.provider = "trakt"
    dispatcher = outbox.OutboxDispatcher(outbox.OutboxHandlerRegistry([handler]))
    await dispatcher.deliver(db, job)
    handler.deliver.assert_not_awaited()
    delivery = AsyncMock(return_value=200)
    monkeypatch.setattr(outbox, "_deliver_batch", delivery)
    await outbox._process_job_batch(db, [job])
    delivery.assert_not_awaited()
    assert job.status == "succeeded"


@pytest.mark.asyncio
async def test_absolute_and_season_ratings_can_share_a_title(context):
    client, _, _ = context
    body = {
        "id": "absolute-rating",
        "event": "rated",
        "rating": 8.5,
        "at": AT,
        "metaId": "kitsu:7",
        "videoId": "kitsu:7:15",
        "season": None,
        "episode": 15,
    }
    assert (await client.post(f"{BASE}/watch_state/push/series/kitsu:7:15.json", json=body)).status_code == 204
    body.update(id="season-rating", scope="season", videoId=None, episode=None, season=1)
    assert (await client.post(f"{BASE}/watch_state/push/series/kitsu:7.json", json=body)).status_code == 204
    assert len((await pull(client))["ratings"]) == 2
