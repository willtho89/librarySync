from datetime import date, datetime, timezone
from importlib import import_module
from unittest.mock import AsyncMock

import httpx
import pytest
import pytest_asyncio
from alembic.migration import MigrationContext
from alembic.operations import Operations
from fastapi import FastAPI
from librarysync.api import routes_addon_watch_state, routes_stremio_addon, routes_stremio_addon_public
from librarysync.api.deps import get_current_user, get_db
from librarysync.connectors.metadata.base import MediaCandidate
from librarysync.core import addon_watch_state
from librarysync.core.metadata_enrichment import enrich_watched_metadata
from librarysync.db.models import (
    Base,
    EpisodeItem,
    MediaItem,
    OutboxJob,
    StremioAddonConfig,
    User,
    WatchedItem,
    WatchEvent,
)
from sqlalchemy import create_engine, func, inspect, select, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

AT = 1790947444
BASE = "/stremio-addon/addon-one"


def movie_event(event="stop", **overrides):
    return {
        "id": f"movie-{event}",
        "event": event,
        "at": AT,
        "scope": "movie",
        "metaId": "tt0111161",
        "videoId": "tt0111161",
        "played": True,
        "ids": {"imdb": "tt0111161", "tmdb": "278"},
        **overrides,
    }


@pytest_asyncio.fixture
async def context():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as db:
        user = User(id="user-one", username="one", password_hash="unused")
        other = User(id="user-two", username="two", password_hash="unused")
        config = StremioAddonConfig(id="addon-one", user_id=user.id, watch_state_enabled=True)
        db.add_all([user, other, config])
        await db.commit()

        async def session():
            try:
                yield db
            except Exception:
                await db.rollback()
                raise

        app = FastAPI()
        app.include_router(routes_addon_watch_state.router)
        app.include_router(routes_stremio_addon.router)
        app.include_router(routes_stremio_addon_public.router)
        app.dependency_overrides[get_db] = session

        async def current_user():
            return await db.get(User, "user-one")

        app.dependency_overrides[get_current_user] = current_user
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            yield client, db, config
    await engine.dispose()


@pytest.mark.asyncio
async def test_opt_in_manifest_and_immediate_disable(context):
    client, db, config = context
    config.watch_state_enabled = False
    await db.commit()
    manifest = (await client.get(f"{BASE}/manifest.json")).json()
    assert "watchState" not in manifest
    assert (await client.get(f"{BASE}/watch_state/pull.json")).status_code == 404
    assert (await client.post(f"{BASE}/watch_state/push/movie/tt0111161.json", json=movie_event())).status_code == 404
    updated = await client.post("/api/stremio-addon/config", json={"watch_state_enabled": True})
    assert updated.json()["watch_state_enabled"] is True
    manifest = (await client.get(f"{BASE}/manifest.json")).json()
    assert manifest["watchState"]["version"] == 2
    assert {"start", "pause", "stop", "played", "unplayed"} <= set(manifest["watchState"]["push"]["events"])
    assert {"name": "watch_state", "types": ["movie", "series"]} in manifest["resources"]
    assert (await client.get("/api/stremio-addon/config")).json()["watch_state_enabled"] is True
    await client.post("/api/stremio-addon/config", json={"is_enabled": False})
    assert (await client.get(f"{BASE}/watch_state/pull.json")).status_code == 404


@pytest.mark.asyncio
async def test_completed_watch_retry_and_pipeline(context):
    client, db, _ = context
    url = f"{BASE}/watch_state/push/movie/tt0111161.json"
    for _ in range(2):
        response = await client.post(url, json=movie_event())
        assert response.status_code == 204
    assert await db.scalar(select(func.count()).select_from(WatchedItem)) == 1
    assert await db.scalar(select(func.count()).select_from(WatchEvent)) == 1
    watched = await db.scalar(select(WatchedItem))
    assert watched.source == "aiostreams"
    assert watched.watched_at.replace(tzinfo=timezone.utc).timestamp() == AT
    job = await db.scalar(select(OutboxJob))
    assert job.target_provider == "internal"
    assert job.job_type == "new_item_added"
    assert job.payload["watched_item_id"] == watched.id
    response = await client.get(f"{BASE}/watch_state/pull.json")
    assert response.headers["cache-control"] == "no-store"
    assert response.json()["watched"]["movies"] == ["tt0111161"]


@pytest.mark.asyncio
async def test_abandoned_playback_does_not_create_history(context):
    client, db, _ = context
    response = await client.post(f"{BASE}/watch_state/push/movie/tt0111161.json", json=movie_event(played=False))
    assert response.status_code == 204
    for model in [WatchedItem, MediaItem, WatchEvent, OutboxJob]:
        assert await db.scalar(select(func.count()).select_from(model)) == 0


@pytest.mark.asyncio
async def test_mark_played_does_not_add_rewatch_but_new_play_does(context):
    client, db, _ = context
    url = f"{BASE}/watch_state/push/movie/tt0111161.json"
    await client.post(url, json=movie_event())
    await client.post(url, json=movie_event("played", at=AT + 1))
    assert await db.scalar(select(func.count()).select_from(WatchedItem)) == 1
    await client.post(url, json=movie_event(id="later-stop", at=AT + 86400))
    assert await db.scalar(select(func.count()).select_from(WatchedItem)) == 2
    jobs = (await db.scalars(select(OutboxJob))).all()
    assert [job.payload["is_rewatch"] for job in jobs] == [False, True]


@pytest.mark.asyncio
async def test_unplayed_user_isolation_retries_and_changed_version(context, monkeypatch):
    client, db, _ = context
    removal = AsyncMock()
    monkeypatch.setattr(addon_watch_state.SYNC_COORDINATOR, "enqueue_delete_all", removal)
    url = f"{BASE}/watch_state/push/movie/tt0111161.json"
    await client.post(url, json=movie_event())
    media = await db.scalar(select(MediaItem))
    db.add(WatchedItem(user_id="user-two", media_item_id=media.id, watched_at=datetime.fromtimestamp(AT, timezone.utc)))
    await db.commit()
    before = (await client.get(f"{BASE}/watch_state/pull.json")).json()
    for _ in range(2):
        assert (await client.post(url, json=movie_event("unplayed", at=AT + 1))).status_code == 204
    removal.assert_awaited_once()
    watches = (await db.scalars(select(WatchedItem))).all()
    assert len(watches) == 1 and watches[0].user_id == "user-two"
    after = (await client.get(f"{BASE}/watch_state/pull.json", params={"since": before["version"]})).json()
    assert after["version"] != before["version"]
    assert after["watched"]["movies"] == []
    # A delayed old play and a retry of the original play cannot restore the mark.
    await client.post(url, json=movie_event("played", id="delayed", at=AT))
    await client.post(url, json=movie_event())
    assert await db.scalar(select(func.count()).select_from(WatchedItem)) == 1


@pytest.mark.asyncio
async def test_pull_full_history_and_version_gate(context):
    client, db, _ = context
    movie = MediaItem(media_type="movie", title="Movie", tmdb_id="42")
    series = MediaItem(media_type="tv", title="Show", imdb_id="tt0903747")
    hidden = MediaItem(media_type="movie", title="Other user's movie", imdb_id="tt9999999")
    db.add_all([movie, series, hidden])
    await db.flush()
    first = EpisodeItem(show_media_item_id=series.id, season_number=3, episode_number=7, air_date=date(2020, 1, 1))
    following = EpisodeItem(show_media_item_id=series.id, season_number=3, episode_number=8, air_date=date(2020, 1, 2))
    db.add_all([first, following])
    await db.flush()
    at = datetime.fromtimestamp(AT, timezone.utc)
    db.add_all(
        [
            WatchedItem(user_id="user-one", media_item_id=movie.id, watched_at=at, source="trakt"),
            WatchedItem(user_id="user-one", episode_item_id=first.id, watched_at=at, source="stremio"),
            WatchedItem(user_id="user-two", media_item_id=hidden.id, watched_at=at),
        ]
    )
    await db.commit()
    full = (await client.get(f"{BASE}/watch_state/pull.json")).json()
    assert full["watched"]["movies"] == ["tmdb:42"]
    assert full["watched"]["episodes"] == ["tt0903747:3:7"]
    assert full["watched"]["counts"] == {"tt0903747": {"watched": 1, "total": 0}}
    assert full["watched"]["nextUp"][0]["videoId"] == "tt0903747:3:8"
    # AIOStreams requires videoId on every recent item, including movies. A missing
    # movie id rejects the whole payload and prevents all episode history importing.
    by_type = {item["type"]: item for item in full["items"]}
    assert by_type["movie"]["videoId"] == by_type["movie"]["metaId"] == "tmdb:42"
    assert by_type["series"]["videoId"] == "tt0903747:3:7"
    unchanged = (await client.get(f"{BASE}/watch_state/pull.json", params={"since": full["version"]})).json()
    assert "watched" not in unchanged
    assert unchanged["items"] == full["items"]
    assert unchanged["version"] == full["version"]


@pytest.mark.asyncio
async def test_episode_payload_uses_explicit_numbers_and_cross_space_video(context):
    client, db, _ = context
    response = await client.post(
        f"{BASE}/watch_state/push/series/kitsu:42323:7.json",
        json={
            "id": "episode-stop",
            "event": "stop",
            "scope": "episode",
            "at": AT,
            "metaId": "tt13293588",
            "videoId": "kitsu:42323:7",
            "season": 0,
            "episode": 7,
            "played": True,
            "ids": {"imdb": "tt13293588", "kitsu": "42323", "mal": "39535"},
        },
    )
    assert response.status_code == 204
    episode = await db.scalar(select(EpisodeItem))
    assert episode.season_number == 0 and episode.episode_number == 7
    pull = (await client.get(f"{BASE}/watch_state/pull.json")).json()
    assert pull["watched"]["episodes"] == ["kitsu:42323:7"]
    assert pull["items"][0]["metaId"] == "tt13293588"


@pytest.mark.asyncio
async def test_reuse_existing_anime_without_duplicating_media(context):
    client, db, _ = context
    db.add(MediaItem(media_type="anime", title="Anime", kitsu_id="42323"))
    await db.commit()
    response = await client.post(
        f"{BASE}/watch_state/push/series/kitsu:42323:7.json",
        json={
            "id": "anime-stop",
            "event": "played",
            "at": AT,
            "metaId": "kitsu:42323",
            "videoId": "kitsu:42323:7",
            "season": 1,
            "episode": 7,
        },
    )
    assert response.status_code == 204
    assert await db.scalar(select(func.count()).select_from(MediaItem)) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "updates",
    [
        {"at": -1},
        {"at": "yesterday"},
        {"played": "false"},
        {"metaId": ""},
        {"scope": "episode"},
        {"event": "rated"},
        {"videoId": "tt9999999"},
    ],
)
async def test_invalid_movie_payload_has_no_side_effects(context, updates):
    client, db, _ = context
    response = await client.post(f"{BASE}/watch_state/push/movie/tt0111161.json", json=movie_event(**updates))
    assert response.status_code == 422
    assert await db.scalar(select(func.count()).select_from(WatchedItem)) == 0


@pytest.mark.asyncio
async def test_unknown_addon_and_viewer_are_rejected(context):
    client, _, _ = context
    for base in ["/stremio-addon/unknown", BASE]:
        params = {"viewer": "someone"} if base == BASE else {}
        assert (await client.get(f"{base}/watch_state/pull.json", params=params)).status_code == 404
        assert (
            await client.post(f"{base}/watch_state/push/movie/tt0111161.json", params=params, json=movie_event())
        ).status_code == 404


@pytest.mark.asyncio
async def test_absolute_episode_is_retained_without_guessing_season(context):
    client, db, _ = context
    response = await client.post(
        f"{BASE}/watch_state/push/series/kitsu:42323:7.json",
        json={
            "id": "absolute-stop",
            "event": "played",
            "at": AT,
            "metaId": "kitsu:42323",
            "videoId": "kitsu:42323:7",
            "season": None,
            "episode": 7,
        },
    )
    assert response.status_code == 204
    assert await db.scalar(select(func.count()).select_from(EpisodeItem)) == 0
    pull = (await client.get(f"{BASE}/watch_state/pull.json")).json()
    assert pull["watched"]["episodes"] == ["kitsu:42323:7"]


def test_migration_keeps_existing_addons_opted_out():
    migration = import_module("librarysync.db.migrations.versions.7d8e9f0a1b2c_add_addon_watch_state")
    engine = create_engine("sqlite:///:memory:")
    with engine.begin() as connection:
        connection.execute(text("CREATE TABLE stremio_addon_configs (id VARCHAR(36) PRIMARY KEY)"))
        connection.execute(text("INSERT INTO stremio_addon_configs (id) VALUES ('existing-addon')"))
        with Operations.context(MigrationContext.configure(connection)):
            migration.upgrade()
            assert connection.scalar(text("SELECT watch_state_enabled FROM stremio_addon_configs")) == 0
            migration.downgrade()
        assert [column["name"] for column in inspect(connection).get_columns("stremio_addon_configs")] == ["id"]
    engine.dispose()


@pytest.mark.asyncio
async def test_inactive_user_cannot_access_history(context):
    client, db, _ = context
    user = await db.get(User, "user-one")
    user.is_active = False
    await db.commit()
    assert (await client.get(f"{BASE}/watch_state/pull.json")).status_code == 404
    assert (await client.post(f"{BASE}/watch_state/push/movie/tt0111161.json", json=movie_event())).status_code == 404


@pytest.mark.asyncio
async def test_unknown_unplayed_does_not_create_placeholder(context):
    client, db, _ = context
    assert (
        await client.post(f"{BASE}/watch_state/push/movie/tt0111161.json", json=movie_event("unplayed"))
    ).status_code == 204
    assert await db.scalar(select(func.count()).select_from(MediaItem)) == 0


@pytest.mark.asyncio
async def test_conflicting_meta_id_rejected(context):
    client, db, _ = context
    response = await client.post(
        f"{BASE}/watch_state/push/movie/tt0111161.json", json=movie_event(ids={"imdb": "tt9999999"})
    )
    assert response.status_code == 409
    assert await db.scalar(select(func.count()).select_from(MediaItem)) == 0


@pytest.mark.asyncio
async def test_existing_episode_preserves_client_video_id(context):
    client, db, _ = context
    series = MediaItem(media_type="tv", title="Show", imdb_id="tt13293588")
    db.add(series)
    await db.flush()
    db.add(EpisodeItem(show_media_item_id=series.id, season_number=1, episode_number=7))
    await db.commit()
    response = await client.post(
        f"{BASE}/watch_state/push/series/kitsu:42323:7.json",
        json={
            "id": "existing-episode-stop",
            "event": "stop",
            "at": AT,
            "played": True,
            "metaId": "tt13293588",
            "videoId": "kitsu:42323:7",
            "season": 1,
            "episode": 7,
            "ids": {"imdb": "tt13293588", "tmdb": "94664"},
        },
    )
    assert response.status_code == 204
    pull = (await client.get(f"{BASE}/watch_state/pull.json")).json()
    assert pull["watched"]["episodes"] == ["kitsu:42323:7"]
    await db.refresh(series)
    assert series.tmdb_id == "94664"
    assert await db.scalar(select(func.count()).select_from(EpisodeItem)) == 1


@pytest.mark.asyncio
async def test_new_title_is_enriched_without_replacing_real_title(context):
    client, db, _ = context
    await client.post(f"{BASE}/watch_state/push/movie/tt0111161.json", json=movie_event())
    media = await db.scalar(select(MediaItem))
    provider = AsyncMock()
    provider.get_details.return_value = MediaCandidate(
        provider="tmdb",
        provider_id="278",
        media_type="movie",
        title="The Shawshank Redemption",
        year=1994,
        poster_url=None,
        imdb_id="tt0111161",
    )
    await enrich_watched_metadata(
        db, "user-one", media, None, provider_overrides={"tmdb": provider}, use_overrides_only=True
    )
    assert media.title == "The Shawshank Redemption"
    media.title = "My custom title"
    await enrich_watched_metadata(
        db, "user-one", media, None, provider_overrides={"tmdb": provider}, use_overrides_only=True
    )
    assert media.title == "My custom title"
