from datetime import date, datetime, timedelta, timezone
from importlib import import_module
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
import pytest_asyncio
from alembic.migration import MigrationContext
from alembic.operations import Operations
from fastapi import FastAPI
from librarysync.api import routes_addon_watch_state, routes_admin, routes_stremio_addon, routes_stremio_addon_public
from librarysync.api.deps import get_current_user, get_db
from librarysync.connectors.metadata import anilist, tmdb, tvdb
from librarysync.connectors.metadata.base import MediaCandidate, ProviderContext
from librarysync.core import (
    addon_watch_state,
    metadata_enrichment,
    metadata_providers,
    rate_limiter,
    watch_pipeline,
    watch_state_events,
    watchlist,
)
from librarysync.core.http_client import retry_after_seconds
from librarysync.core.metadata_enrichment import enrich_watched_metadata
from librarysync.db.models import (
    Base,
    EpisodeItem,
    Integration,
    IntegrationSecret,
    MediaItem,
    OutboxJob,
    RateLimitBucket,
    StremioAddonConfig,
    User,
    WatchedItem,
    WatchEvent,
    WatchlistItem,
)
from librarysync.jobs import metadata_backfill, process_outbox
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


async def run_backfill(db, monkeypatch, provider, batch_size=metadata_backfill.METADATA_BACKFILL_BATCH_SIZE):
    monkeypatch.setattr(metadata_backfill, "extend_scheduled_job", AsyncMock())
    now = datetime.now(timezone.utc)
    # SQLite reads timestamps back without a time zone, so the bucket stays loaded for the limiter to reuse.
    bucket = await db.scalar(
        select(RateLimitBucket).where(RateLimitBucket.user_id == "user-one", RateLimitBucket.provider == "tmdb")
    )
    if bucket is None:
        bucket = tmdb_bucket(150.0)
        db.add(bucket)
        await db.commit()
    await metadata_backfill._backfill_media_items(
        db,
        "user-one",
        batch_size,
        None,
        now=now,
        tmdb_required=False,
        episode_refresh_cutoff=now,
        metadata_refresh_delta=metadata_backfill.METADATA_BACKFILL_METADATA_REFRESH_DELTA,
        force_metadata_refresh=True,
        force_episode_refresh=False,
        provider_overrides={"tmdb": provider},
        use_overrides_only=True,
    )


def tmdb_bucket(tokens):
    now = datetime.now(timezone.utc)
    return RateLimitBucket(user_id="user-one", provider="tmdb", tokens=tokens, last_refill_at=now)


async def finish_lookup_jobs(db):
    """Settle queued metadata_update jobs as a worker would, so the backfill no longer leaves their titles alone."""
    for job in (await db.scalars(select(OutboxJob).where(OutboxJob.job_type == "metadata_update"))).all():
        job.status, job.dedupe_key = "succeeded", None
    await db.commit()


def fake_enrich(**outcome_fields):
    """Stands in for enrich_watched_metadata and reports the given lookup outcome."""

    async def enrich(db, user_id, media_item, episode_item, outcome=None, **_kwargs):
        for name, value in outcome_fields.items():
            setattr(outcome, name, value)

    return enrich


def tmdb_provider(owner="user-one"):
    """A TMDB stand-in. Budgeted lookups take its requests from the owner's real rate limit bucket."""
    provider = AsyncMock()
    provider.provider = "tmdb"
    provider.context = SimpleNamespace(user_id=owner)
    return provider


def placeholder(imdb_id, tmdb_id):
    return MediaItem(
        media_type="movie",
        title=imdb_id,
        imdb_id=imdb_id,
        tmdb_id=tmdb_id,
        raw={"source": "aiostreams_watch_state", "stremio_id": imdb_id},
    )


@pytest_asyncio.fixture
async def context(monkeypatch):
    # The lookup gate asks for a configured metadata provider. Tests of the gate itself restore it.
    monkeypatch.setattr(watch_state_events, "has_metadata_provider", AsyncMock(return_value=True))
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
    provider = tmdb_provider()
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


@pytest.mark.asyncio
async def test_watchlisted_new_title_queues_one_metadata_update(context):
    client, db, _ = context
    url = f"{BASE}/watch_state/push/movie/tt0111161.json"
    await client.post(url, json=movie_event("watchlisted"))
    await client.post(url, json=movie_event("unwatchlisted", at=AT + 1))
    media = await db.scalar(select(MediaItem))
    jobs = (await db.scalars(select(OutboxJob).where(OutboxJob.job_type == "metadata_update"))).all()
    assert media.title == "tt0111161"
    assert [job.payload for job in jobs] == [{"media_item_id": media.id}]


@pytest.mark.asyncio
async def test_completed_watch_leaves_enrichment_to_new_item_job(context):
    client, db, _ = context
    await client.post(f"{BASE}/watch_state/push/movie/tt0111161.json", json=movie_event())
    assert (await db.scalars(select(OutboxJob.job_type))).all() == ["new_item_added"]


@pytest.mark.asyncio
async def test_metadata_update_job_enriches_its_title(context, monkeypatch):
    client, db, _ = context
    await client.post(f"{BASE}/watch_state/push/movie/tt0111161.json", json=movie_event("watchlisted"))
    media = await db.scalar(select(MediaItem))
    job = await db.scalar(select(OutboxJob).where(OutboxJob.job_type == "metadata_update"))
    enrich = AsyncMock()
    monkeypatch.setattr(watch_pipeline, "enrich_watched_metadata", enrich)
    await watch_pipeline.process_metadata_update_job(db, job)
    enrich.assert_awaited_once()
    assert enrich.await_args.args == (db, "user-one", media, None)
    assert enrich.await_args.kwargs["outcome"].budgeted
    job.payload = {"media_item_id": "merged-away"}
    await watch_pipeline.process_metadata_update_job(db, job)
    enrich.assert_awaited_once()


@pytest.mark.asyncio
async def test_backfill_enriches_watchlist_only_titles(context, monkeypatch):
    client, db, _ = context
    await client.post(f"{BASE}/watch_state/push/movie/tt0111161.json", json=movie_event("watchlisted"))
    media = await db.scalar(select(MediaItem))
    provider = tmdb_provider()
    provider.get_details.return_value = MediaCandidate(
        provider="tmdb",
        provider_id="278",
        media_type="movie",
        title="The Shawshank Redemption",
        year=1994,
        poster_url=None,
        imdb_id="tt0111161",
    )
    await finish_lookup_jobs(db)
    await run_backfill(db, monkeypatch, provider)
    await db.refresh(media)
    assert media.title == "The Shawshank Redemption"
    assert media.metadata_refreshed_at is not None


@pytest.mark.asyncio
async def test_watchlisted_placeholder_series_is_enriched(context, monkeypatch):
    client, db, _ = context
    body = {"id": "watchlist-series", "event": "watchlisted", "scope": "series", "at": AT, "metaId": "tt0903747"}
    await client.post(f"{BASE}/watch_state/push/series/tt0903747.json", json=body)
    media = await db.scalar(select(MediaItem))
    media.year = 2008  # A Trakt or SIMKL import fills in the year and keeps the title.
    await db.commit()
    jobs = (await db.scalars(select(OutboxJob).where(OutboxJob.job_type == "metadata_update"))).all()
    assert [job.payload for job in jobs] == [{"media_item_id": media.id}]
    provider = tmdb_provider()
    provider.find_by_external_id.return_value = [
        MediaCandidate(
            provider="tmdb",
            provider_id="1396",
            media_type="tv",
            title="Breaking Bad",
            year=2008,
            poster_url=None,
            imdb_id="tt0903747",
        )
    ]
    await finish_lookup_jobs(db)
    await run_backfill(db, monkeypatch, provider)
    await db.refresh(media)
    assert media.title == "Breaking Bad"
    provider.find_by_external_id.assert_awaited_with("tt0903747", "tv")


@pytest.mark.asyncio
async def test_confirmed_miss_is_not_looked_up_again_during_cooldown(context, monkeypatch):
    client, db, _ = context
    url = f"{BASE}/watch_state/push/movie/tt0111161.json"
    await client.post(url, json=movie_event("watchlisted"))
    job = await db.scalar(select(OutboxJob).where(OutboxJob.job_type == "metadata_update"))
    monkeypatch.setattr(watch_pipeline, "enrich_watched_metadata", fake_enrich(requests=1))
    await watch_pipeline.process_metadata_update_job(db, job)
    await finish_lookup_jobs(db)
    media = await db.scalar(select(MediaItem))
    assert media.metadata_refreshed_at is None
    assert metadata_enrichment.LOOKUP_MISS_KEY in media.raw
    await client.post(url, json=movie_event("unwatchlisted", at=AT + 1))
    await client.post(url, json=movie_event("watchlisted", id="watchlisted-again", at=AT + 2))
    statuses = (await db.scalars(select(OutboxJob.status).where(OutboxJob.job_type == "metadata_update"))).all()
    assert statuses == ["succeeded"]
    provider = tmdb_provider()
    await run_backfill(db, monkeypatch, provider)
    provider.get_details.assert_not_awaited()


@pytest.mark.asyncio
async def test_lookup_that_asked_nothing_records_nothing(context, monkeypatch):
    client, db, _ = context
    url = f"{BASE}/watch_state/push/movie/tt0111161.json"
    await client.post(url, json=movie_event("watchlisted"))
    job = await db.scalar(select(OutboxJob).where(OutboxJob.job_type == "metadata_update"))
    monkeypatch.setattr(watch_pipeline, "enrich_watched_metadata", fake_enrich())
    await watch_pipeline.process_metadata_update_job(db, job)
    await finish_lookup_jobs(db)
    media = await db.scalar(select(MediaItem))
    assert media.metadata_refreshed_at is None
    assert metadata_enrichment.LOOKUP_MISS_KEY not in media.raw
    # A later event, for example once a provider is set up, still queues a lookup.
    await client.post(url, json=movie_event("unwatchlisted", at=AT + 1))
    await client.post(url, json=movie_event("watchlisted", id="watchlisted-again", at=AT + 2))
    statuses = (await db.scalars(select(OutboxJob.status).where(OutboxJob.job_type == "metadata_update"))).all()
    assert sorted(statuses) == ["pending", "succeeded"]


@pytest.mark.asyncio
async def test_anime_lookup_that_cannot_ask_any_provider_makes_no_request(context):
    client, db, _ = context
    body = {"id": "watchlist-kitsu", "event": "watchlisted", "scope": "series", "at": AT, "metaId": "kitsu:42323"}
    await client.post(f"{BASE}/watch_state/push/series/kitsu:42323.json", json=body)
    media = await db.scalar(select(MediaItem))
    media.imdb_id = "tt22248376"
    anilist = AsyncMock()
    anilist.provider = "anilist"
    outcome = metadata_enrichment.LookupOutcome()
    overrides = {"anilist": anilist}
    await enrich_watched_metadata(
        db, "user-one", media, None, provider_overrides=overrides, use_overrides_only=True, outcome=outcome
    )
    # AniList resolves only its own and MAL ids, and a placeholder title cannot be searched.
    anilist.find_by_external_id.assert_not_awaited()
    anilist.search.assert_not_awaited()
    assert outcome.requests == 0
    now = datetime.now(timezone.utc)
    assert metadata_enrichment.settle_placeholder_lookup(media, outcome, now) == "not_attempted"
    assert metadata_enrichment.LOOKUP_MISS_KEY not in media.raw


@pytest.mark.asyncio
async def test_lookups_charge_each_provider_owner_for_every_request(context, monkeypatch):
    _, db, _ = context
    media = placeholder("tt0111161", None)
    db.add(media)
    await db.flush()
    tmdb = tmdb_provider(owner="user-one")
    tmdb.find_by_external_id.return_value = []
    tvdb = tmdb_provider(owner="user-two")
    tvdb.provider = "tvdb"
    tvdb.find_by_external_id.return_value = []
    acquire = AsyncMock(return_value=None)
    monkeypatch.setattr(metadata_enrichment.RATE_LIMITER, "try_acquire", acquire)
    outcome = metadata_enrichment.LookupOutcome(budgeted=True)
    overrides = {"tmdb": tmdb, "tvdb": tvdb}
    await enrich_watched_metadata(
        db, "user-one", media, None, provider_overrides=overrides, use_overrides_only=True, outcome=outcome
    )
    charges = [(call.args[1], call.args[2], call.kwargs["cost"]) for call in acquire.await_args_list]
    assert charges == [("user-one", "tmdb", 2), ("user-two", "tvdb", 7)]
    assert outcome.requests == 2


@pytest.mark.asyncio
async def test_rate_limiter_takes_one_token_per_request(context):
    _, db, _ = context
    limiter = rate_limiter.RateLimiter({"tmdb": rate_limiter.RateLimitConfig(capacity=10.0, refill_per_second=0.01)})
    now = datetime.now(timezone.utc)
    bucket = tmdb_bucket(2.0)
    db.add(bucket)
    await db.commit()
    refused = await limiter.try_acquire(db, "user-one", "tmdb", now=now, cost=3)
    assert not refused.allowed
    assert refused.retry_at == now + timedelta(seconds=100)
    assert (await limiter.try_acquire(db, "user-one", "tmdb", now=now, cost=2)).allowed
    assert bucket.tokens == 0.0
    bucket.tokens = 10.0
    assert not (await limiter.try_acquire(db, "user-one", "tmdb", now=now, cost=11)).allowed


@pytest.mark.asyncio
async def test_provider_whose_limit_cannot_cover_one_call_sits_the_lookup_out(context, monkeypatch):
    _, db, _ = context
    media = placeholder("tt0111161", None)
    db.add(media)
    await db.flush()
    tvdb = tmdb_provider()
    tvdb.provider = "tvdb"
    limiter = rate_limiter.RateLimiter({"tvdb": rate_limiter.RateLimitConfig(capacity=5.0, refill_per_second=1.0)})
    monkeypatch.setattr(metadata_enrichment, "RATE_LIMITER", limiter)
    outcome = metadata_enrichment.LookupOutcome(budgeted=True)
    await enrich_watched_metadata(
        db, "user-one", media, None, provider_overrides={"tvdb": tvdb}, use_overrides_only=True, outcome=outcome
    )
    tvdb.find_by_external_id.assert_not_awaited()
    assert (outcome.requests, outcome.retry_at) == (0, None)


def scripted_http(monkeypatch, module, responses):
    """Serves the responses in order through the connector's HTTP client and records each outbound request."""
    sent = []

    def handle(request):
        sent.append(request)
        status, body = responses[len(sent) - 1]
        return httpx.Response(status, json=body)

    def client(**kwargs):
        return httpx.AsyncClient(transport=httpx.MockTransport(handle), **kwargs)

    monkeypatch.setattr(module, "get_http_client", client)
    return sent


def real_tmdb():
    return tmdb.TmdbMetadataProvider(
        tmdb.TmdbConfig(language="de-DE"), tmdb.TmdbSecrets(api_key="key"), ProviderContext(user_id="user-one")
    )


def real_tvdb(language="eng"):
    return tvdb.TvdbMetadataProvider(
        tvdb.TvdbConfig(language=language), tvdb.TvdbSecrets(api_key="key"), ProviderContext(user_id="user-one")
    )


LOGIN = (200, {"data": {"token": "token"}})
EXPIRED = (401, {})
TMDB_EMPTY_FIND = (200, {"movie_results": [], "tv_results": []})


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("module", "make", "call", "run", "responses"),
    [
        (tmdb, real_tmdb, "find", lambda p: p.find_by_external_id("tt0111161", "movie"), [TMDB_EMPTY_FIND] * 2),
        (tmdb, real_tmdb, "search", lambda p: p.search("Shawshank", "movie"), [(200, {"results": []})] * 2),
        (tmdb, real_tmdb, "details", lambda p: p.get_details("278", "movie"), [(200, {"id": 278, "title": "S"})]),
        (
            tvdb,
            real_tvdb,
            "details",
            lambda p: p.get_details("1", "movie"),
            [LOGIN, EXPIRED, LOGIN, (404, {}), EXPIRED, LOGIN, (200, {"data": {"id": 1, "name": "S"}})],
        ),
        (
            tvdb,
            real_tvdb,
            "search",
            lambda p: p.search("Shawshank", "movie"),
            [LOGIN, EXPIRED, LOGIN, (200, {"data": []}), EXPIRED, LOGIN, (200, {"data": []})],
        ),
        (
            tvdb,
            real_tvdb,
            "find",
            lambda p: p.find_by_external_id("tt0111161", "movie"),
            [LOGIN, EXPIRED, LOGIN, (200, {"data": []}), EXPIRED, LOGIN, (200, {"data": []})],
        ),
    ],
)
async def test_reserved_cost_covers_every_request_a_call_can_send(monkeypatch, module, make, call, run, responses):
    # The worst path: TMDB's language fallback, TVDB's login, re-login after a 401, and 404 or empty-search fallback.
    sent = scripted_http(monkeypatch, module, responses)
    provider = make()
    await run(provider)
    assert len(sent) == len(responses)
    assert len(sent) <= metadata_enrichment.REQUEST_COSTS.get((provider.provider, call), 1)


def tvdb_movie(tvdb_id, name, tmdb_id=None):
    remote_ids = [{"sourceName": "TheMovieDB.com", "id": tmdb_id}] if tmdb_id else []
    return {"id": f"movie-{tvdb_id}", "type": "movie", "name": name, "remote_ids": remote_ids}


@pytest.mark.asyncio
async def test_tvdb_external_id_lookup_keeps_only_results_carrying_the_id(monkeypatch):
    results = [tvdb_movie(1, "1833238 Ways to Die"), tvdb_movie(2, "Nathan Fielder", tmdb_id="1833238")]
    scripted_http(monkeypatch, tvdb, [LOGIN, (200, {"data": results})])
    candidates = await real_tvdb(language=None).find_by_external_id("1833238", "movie")
    assert [candidate.title for candidate in candidates] == ["Nathan Fielder"]


@pytest.mark.asyncio
async def test_tmdb_only_placeholder_never_takes_an_unrelated_tvdb_result(context, monkeypatch):
    _, db, _ = context
    meta_id = "tmdb:1833238"
    media = MediaItem(
        media_type="movie",
        title=meta_id,
        tmdb_id="1833238",
        raw={"source": "aiostreams_watch_state", "stremio_id": meta_id},
    )
    db.add(media)
    await db.flush()
    scripted_http(monkeypatch, tvdb, [LOGIN, (200, {"data": [tvdb_movie(1, "1833238 Ways to Die")]})])
    outcome = metadata_enrichment.LookupOutcome()
    overrides = {"tvdb": real_tvdb(language=None)}
    await enrich_watched_metadata(
        db, "user-one", media, None, provider_overrides=overrides, use_overrides_only=True, outcome=outcome
    )
    assert (media.title, media.tvdb_id, outcome.requests) == (meta_id, None, 1)


def tmdb_limited_to(monkeypatch, capacity):
    limiter = rate_limiter.RateLimiter(
        {"tmdb": rate_limiter.RateLimitConfig(capacity=capacity, refill_per_second=0.001)}
    )
    monkeypatch.setattr(metadata_enrichment, "RATE_LIMITER", limiter)
    monkeypatch.setattr(watchlist, "RATE_LIMITER", limiter)


SEASONS = (200, {"seasons": [{"season_number": 1}, {"season_number": 2}]})
EPISODES = (200, {"episodes": [{"episode_number": 1, "id": 62085, "name": "Pilot"}]})


@pytest.mark.asyncio
async def test_episode_refresh_sends_nothing_under_a_one_token_limit(context, monkeypatch):
    _, db, _ = context
    show = MediaItem(media_type="tv", title="Breaking Bad", tmdb_id="1396")
    db.add(show)
    await db.commit()
    tmdb_limited_to(monkeypatch, 1.0)
    sent = scripted_http(monkeypatch, tmdb, [SEASONS] * 2)
    assert not await watchlist.backfill_show_episodes(db, "user-one", show, provider_override=real_tmdb())
    assert sent == []


@pytest.mark.asyncio
async def test_episode_refresh_charges_the_provider_owner_for_every_request(context, monkeypatch):
    _, db, _ = context
    show = MediaItem(media_type="tv", title="Breaking Bad", tmdb_id="1396")
    # The provider belongs to user-one. Held, so SQLite does not hand the limiter a naive timestamp.
    bucket = tmdb_bucket(4.0)
    db.add_all([show, bucket])
    await db.commit()
    tmdb_limited_to(monkeypatch, 4.0)
    sent = scripted_http(monkeypatch, tmdb, [SEASONS] * 2 + [EPISODES] * 2)
    await watchlist.backfill_show_episodes(db, "user-two", show, provider_override=real_tmdb())
    # Seasons and season 1 take the four tokens. Season 2 is refused before it sends anything.
    assert len(sent) == 4
    assert bucket.tokens < 1


def test_long_retry_after_is_honoured_up_to_a_day():
    assert process_outbox._throttle_delay(7200) == timedelta(hours=2)
    assert process_outbox._throttle_delay(None) == timedelta(seconds=60)
    assert process_outbox._throttle_delay(1e12) == timedelta(days=1)
    for header in ("1e12", "Fri, 31 Dec 9999 23:59:59 GMT"):
        response = status_error(429, {"Retry-After": header}).response
        assert retry_after_seconds(response) == timedelta(days=1).total_seconds()


@pytest.mark.asyncio
async def test_provider_429_requeues_the_lookup_without_an_attempt(context, monkeypatch):
    client, db, _ = context
    await client.post(f"{BASE}/watch_state/push/movie/tt0111161.json", json=movie_event("watchlisted"))
    job = await db.scalar(select(OutboxJob).where(OutboxJob.job_type == "metadata_update"))
    # Held, so SQLite does not hand the limiter a timestamp without a time zone.
    bucket = tmdb_bucket(150.0)
    db.add(bucket)
    await db.commit()
    provider = tmdb_provider()
    limited = status_error(429, {"Retry-After": "1e12"})
    provider.get_details.side_effect = provider.find_by_external_id.side_effect = limited

    async def load_provider(self, name):
        return provider if name == "tmdb" else None

    monkeypatch.setattr(metadata_enrichment.MetadataProviderService, "load_provider", load_provider)
    await process_outbox._process_job(db, job)
    assert provider.get_details.await_count + provider.find_by_external_id.await_count == 1
    assert (job.status, job.attempts) == ("failed_retryable", 0)
    run_after = job.run_after if job.run_after.tzinfo else job.run_after.replace(tzinfo=timezone.utc)
    assert run_after > datetime.now(timezone.utc) + timedelta(hours=23)


def status_error(status, headers=None):
    request = httpx.Request("GET", "https://api.example.test/title")
    response = httpx.Response(status, headers=headers, request=request)
    return httpx.HTTPStatusError(f"HTTP {status}", request=request, response=response)


def test_provider_errors_are_classified_by_status():
    provider = tmdb_provider()
    now = datetime.now(timezone.utc)
    limited = metadata_enrichment.LookupOutcome()
    limited.fail(provider, status_error(429, {"Retry-After": "120"}))
    assert now + timedelta(seconds=110) < limited.retry_at < now + timedelta(seconds=130)
    default = metadata_enrichment.LookupOutcome()
    default.fail(provider, status_error(429))
    assert now + timedelta(seconds=50) < default.retry_at < now + timedelta(seconds=70)
    outcome = metadata_enrichment.LookupOutcome()
    outcome.fail(provider, status_error(404))
    assert (outcome.failed, outcome.denied, outcome.retry_at) == ([], [], None)
    outcome.fail(provider, status_error(401))
    outcome.fail(provider, status_error(503))
    assert (len(outcome.denied), len(outcome.failed)) == (1, 1)


@pytest.mark.asyncio
async def test_anilist_rate_limit_keeps_its_retry_after(monkeypatch):
    def limited_client(**kwargs):
        transport = httpx.MockTransport(lambda request: httpx.Response(429, headers={"Retry-After": "30"}))
        return httpx.AsyncClient(transport=transport, **kwargs)

    monkeypatch.setattr(anilist, "get_http_client", limited_client)
    provider = anilist.AniListMetadataProvider(anilist.AniListConfig(), None, ProviderContext(user_id="user-one"))
    with pytest.raises(httpx.HTTPStatusError) as raised:
        await provider.search("Frieren", "anime")
    outcome = metadata_enrichment.LookupOutcome()
    outcome.fail(provider, raised.value)
    assert outcome.retry_at > datetime.now(timezone.utc) + timedelta(seconds=25)
    assert outcome.failed == []


@pytest.mark.asyncio
async def test_title_the_provider_does_not_know_is_a_confirmed_miss(context):
    _, db, _ = context
    media = placeholder("tt0111161", "278")
    db.add(media)
    await db.flush()
    provider = tmdb_provider()
    provider.get_details.side_effect = status_error(404)
    outcome = metadata_enrichment.LookupOutcome()
    await enrich_watched_metadata(
        db, "user-one", media, None, provider_overrides={"tmdb": provider}, use_overrides_only=True, outcome=outcome
    )
    now = datetime.now(timezone.utc)
    assert metadata_enrichment.settle_placeholder_lookup(media, outcome, now) == "missed"
    assert media.raw[metadata_enrichment.LOOKUP_MISS_KEY] == now.isoformat()
    # A miss already in its cooldown is not rewritten, so the shared catalog is not rebuilt for nothing.
    later = now + timedelta(hours=1)
    assert metadata_enrichment.settle_placeholder_lookup(media, outcome, later) == "missed"
    assert media.raw[metadata_enrichment.LOOKUP_MISS_KEY] == now.isoformat()


@pytest.mark.asyncio
async def test_rejected_credentials_fail_the_lookup_for_good(context, monkeypatch):
    client, db, _ = context
    await client.post(f"{BASE}/watch_state/push/movie/tt0111161.json", json=movie_event("watchlisted"))
    job = await db.scalar(select(OutboxJob).where(OutboxJob.job_type == "metadata_update"))
    monkeypatch.setattr(watch_pipeline, "enrich_watched_metadata", fake_enrich(requests=1, denied=["tmdb: HTTP 401"]))
    await process_outbox._process_job(db, job)
    assert job.status == "failed_permanent"
    assert metadata_enrichment.LOOKUP_MISS_KEY not in (await db.scalar(select(MediaItem))).raw


@pytest.mark.asyncio
async def test_title_held_by_another_worker_is_retried_later(context, monkeypatch):
    client, db, _ = context
    await client.post(f"{BASE}/watch_state/push/movie/tt0111161.json", json=movie_event("watchlisted"))
    job = await db.scalar(select(OutboxJob).where(OutboxJob.job_type == "metadata_update"))
    enrich = AsyncMock()
    monkeypatch.setattr(watch_pipeline, "enrich_watched_metadata", enrich)
    # SQLite has no row locks, so the skipped lock is simulated.
    monkeypatch.setattr(db, "get", AsyncMock(return_value=None))
    with pytest.raises(metadata_enrichment.MetadataLookupThrottled) as raised:
        await watch_pipeline.process_metadata_update_job(db, job)
    assert 50 < raised.value.retry_after <= 60
    enrich.assert_not_awaited()


@pytest.mark.asyncio
async def test_failed_lookup_is_retried_and_not_recorded(context, monkeypatch):
    client, db, _ = context
    await client.post(f"{BASE}/watch_state/push/movie/tt0111161.json", json=movie_event("watchlisted"))
    job = await db.scalar(select(OutboxJob).where(OutboxJob.job_type == "metadata_update"))
    monkeypatch.setattr(watch_pipeline, "enrich_watched_metadata", fake_enrich(requests=1, failed=["tmdb: timed out"]))
    await process_outbox._process_job(db, job)
    assert (job.status, job.attempts) == ("failed_retryable", 1)
    media = await db.scalar(select(MediaItem))
    assert media.metadata_refreshed_at is None
    assert metadata_enrichment.LOOKUP_MISS_KEY not in media.raw


@pytest.mark.asyncio
async def test_throttled_lookup_is_requeued_without_an_attempt(context, monkeypatch):
    client, db, _ = context
    await client.post(f"{BASE}/watch_state/push/movie/tt0111161.json", json=movie_event("watchlisted"))
    job = await db.scalar(select(OutboxJob).where(OutboxJob.job_type == "metadata_update"))
    retry_at = datetime.now(timezone.utc) + timedelta(hours=2)
    monkeypatch.setattr(watch_pipeline, "enrich_watched_metadata", fake_enrich(requests=1, retry_at=retry_at))
    await process_outbox._process_job(db, job)
    assert (job.status, job.attempts) == ("failed_retryable", 0)
    run_after = job.run_after if job.run_after.tzinfo else job.run_after.replace(tzinfo=timezone.utc)
    assert run_after > datetime.now(timezone.utc) + timedelta(minutes=115)
    assert (await db.scalar(select(MediaItem))).metadata_refreshed_at is None


@pytest.mark.asyncio
async def test_provider_errors_and_budget_are_recorded_in_the_outcome(context, monkeypatch):
    client, db, _ = context
    await client.post(f"{BASE}/watch_state/push/movie/tt0111161.json", json=movie_event("watchlisted"))
    media = await db.scalar(select(MediaItem))
    provider = tmdb_provider()
    provider.provider = "tmdb"
    provider.get_details.side_effect = httpx.ReadTimeout("timed out")
    outcome = metadata_enrichment.LookupOutcome()
    await enrich_watched_metadata(
        db, "user-one", media, None, provider_overrides={"tmdb": provider}, use_overrides_only=True, outcome=outcome
    )
    assert (outcome.requests, outcome.failed, media.title) == (1, ["tmdb: timed out"], "tt0111161")

    provider.get_details.reset_mock()
    retry_at = datetime.now(timezone.utc) + timedelta(minutes=1)
    refuse = AsyncMock(return_value=SimpleNamespace(allowed=False, retry_at=retry_at))
    monkeypatch.setattr(metadata_enrichment.RATE_LIMITER, "try_acquire", refuse)
    budgeted = metadata_enrichment.LookupOutcome(budgeted=True)
    await enrich_watched_metadata(
        db, "user-one", media, None, provider_overrides={"tmdb": provider}, use_overrides_only=True, outcome=budgeted
    )
    provider.get_details.assert_not_awaited()
    assert (budgeted.retry_at, budgeted.requests) == (retry_at, 0)
    assert refuse.await_args.args[1:] == ("user-one", "tmdb")
    assert refuse.await_args.kwargs == {"cost": 1}


@pytest.mark.asyncio
async def test_each_user_gets_a_lookup_and_a_resolved_title_needs_no_second(context, monkeypatch):
    _, db, _ = context
    media = placeholder("tt0111161", "278")
    db.add(media)
    await db.flush()
    first = await watch_pipeline.enqueue_metadata_update_job(db, "user-one", media.id)
    second = await watch_pipeline.enqueue_metadata_update_job(db, "user-two", media.id)
    await db.commit()
    assert first is not second

    async def resolve(db, user_id, media_item, episode_item, outcome=None, **_kwargs):
        outcome.requests = 1
        media_item.title = "The Shawshank Redemption"

    enrich = AsyncMock(side_effect=resolve)
    monkeypatch.setattr(watch_pipeline, "enrich_watched_metadata", enrich)
    await watch_pipeline.process_metadata_update_job(db, first)
    await watch_pipeline.process_metadata_update_job(db, second)
    assert enrich.await_args.args[1] == "user-one"
    enrich.assert_awaited_once()


@pytest.mark.asyncio
async def test_requeueing_a_waiting_lookup_keeps_its_backoff(context):
    _, db, _ = context
    media = placeholder("tt0111161", "278")
    db.add(media)
    await db.flush()
    with db.no_autoflush:
        # Production sessions do not autoflush, so the second call must find the unflushed job.
        job = await watch_pipeline.enqueue_metadata_update_job(db, "user-one", media.id)
        assert await watch_pipeline.enqueue_metadata_update_job(db, "user-one", media.id) is job
    await db.commit()
    run_after = datetime.now(timezone.utc) + timedelta(minutes=30)
    job.status, job.run_after, job.last_error = "failed_retryable", run_after, "tmdb: timed out"
    await db.commit()
    assert await watch_pipeline.enqueue_metadata_update_job(db, "user-one", media.id) is job
    assert (job.status, job.run_after, job.last_error) == ("failed_retryable", run_after, "tmdb: timed out")
    assert await db.scalar(select(func.count()).select_from(OutboxJob)) == 1


@pytest.mark.asyncio
async def test_backfill_pages_by_id_while_fixing_placeholders(context, monkeypatch):
    _, db, _ = context
    for number in (1, 2, 3):
        media = placeholder(f"tt000000{number}", str(number))
        db.add(media)
        await db.flush()
        db.add(WatchlistItem(user_id="user-one", media_item_id=media.id, type="movie", source="aiostreams"))
    await db.commit()

    def details(provider_id, scope):
        return MediaCandidate(
            provider="tmdb",
            provider_id=provider_id,
            media_type="movie",
            title=f"Movie {provider_id}",
            year=2000,
            poster_url=None,
            imdb_id=None,
        )

    provider = tmdb_provider()
    provider.get_details.side_effect = details
    await run_backfill(db, monkeypatch, provider, batch_size=1)
    titles = (await db.scalars(select(MediaItem.title).order_by(MediaItem.title))).all()
    assert titles == ["Movie 1", "Movie 2", "Movie 3"]


@pytest.mark.asyncio
async def test_backfill_leaves_titles_with_a_queued_lookup_to_the_job(context, monkeypatch):
    client, db, _ = context
    await client.post(f"{BASE}/watch_state/push/movie/tt0111161.json", json=movie_event("watchlisted"))
    provider = tmdb_provider()
    await run_backfill(db, monkeypatch, provider)
    provider.get_details.assert_not_awaited()


@pytest.mark.asyncio
async def test_known_kitsu_id_names_the_placeholder_but_never_a_real_title(context):
    client, db, _ = context
    body = {"id": "watchlist-kitsu", "event": "watchlisted", "scope": "series", "at": AT, "metaId": "kitsu:42323"}
    await client.post(f"{BASE}/watch_state/push/series/kitsu:42323.json", json=body)
    media = await db.scalar(select(MediaItem))
    assert (media.title, media.kitsu_id) == ("kitsu:42323", "42323")
    provider = tmdb_provider()
    provider.provider = "kitsu"
    provider.get_details.return_value = MediaCandidate(
        provider="kitsu",
        provider_id="42323",
        media_type="anime",
        title="Frieren: Beyond Journey's End",
        year=2023,
        poster_url="https://media.kitsu.app/anime/42323/poster.jpg",
        imdb_id=None,
    )
    overrides = {"kitsu": provider}
    await enrich_watched_metadata(db, "user-one", media, None, provider_overrides=overrides, use_overrides_only=True)
    provider.get_details.assert_awaited_once_with("42323", "anime")
    assert media.title == "Frieren: Beyond Journey's End"

    media.title = "Sousou no Frieren"
    await enrich_watched_metadata(db, "user-one", media, None, provider_overrides=overrides, use_overrides_only=True)
    provider.get_details.assert_awaited_once()
    assert media.title == "Sousou no Frieren"


@pytest.mark.asyncio
async def test_backfill_leaves_other_watchlist_titles_alone(context, monkeypatch):
    _, db, _ = context
    media = MediaItem(media_type="movie", title="The Shawshank Redemption", imdb_id="tt0111161")
    db.add(media)
    await db.flush()
    db.add(WatchlistItem(user_id="user-one", media_item_id=media.id, type="movie", source="trakt"))
    await db.commit()
    provider = tmdb_provider()
    await run_backfill(db, monkeypatch, provider)
    provider.get_details.assert_not_awaited()
    provider.find_by_external_id.assert_not_awaited()


@pytest.mark.asyncio
async def test_playback_does_not_queue_but_rating_does(context):
    client, db, _ = context
    url = f"{BASE}/watch_state/push/movie/tt0111161.json"
    await client.post(url, json=movie_event("pause", played=False, positionMs=120000, durationMs=600000))
    assert await db.scalar(select(MediaItem.title)) == "tt0111161"
    query = select(OutboxJob).where(OutboxJob.job_type == "metadata_update")
    assert (await db.scalars(query)).all() == []
    await client.post(url, json=movie_event("rated", rating=8.5, at=AT + 1))
    assert len((await db.scalars(query)).all()) == 1


@pytest.mark.asyncio
async def test_metadata_update_job_skips_titles_already_enriched(context, monkeypatch):
    client, db, _ = context
    await client.post(f"{BASE}/watch_state/push/movie/tt0111161.json", json=movie_event("watchlisted"))
    media = await db.scalar(select(MediaItem))
    media.title = "The Shawshank Redemption"  # new_item_added got there first.
    job = await db.scalar(select(OutboxJob).where(OutboxJob.job_type == "metadata_update"))
    enrich = AsyncMock()
    monkeypatch.setattr(watch_pipeline, "enrich_watched_metadata", enrich)
    await watch_pipeline.process_metadata_update_job(db, job)
    enrich.assert_not_awaited()
    assert media.metadata_refreshed_at is None


def shawshank(*_args):
    return MediaCandidate(
        provider="tmdb",
        provider_id="278",
        media_type="movie",
        title="The Shawshank Redemption",
        year=1994,
        poster_url=None,
        imdb_id="tt0111161",
    )


@pytest.mark.asyncio
async def test_backfill_looks_up_placeholders_that_are_only_rated(context, monkeypatch):
    client, db, _ = context
    await client.post(f"{BASE}/watch_state/push/movie/tt0111161.json", json=movie_event("rated", rating=8.5))
    assert await db.scalar(select(func.count()).select_from(WatchlistItem)) == 0
    await finish_lookup_jobs(db)
    provider = tmdb_provider()
    provider.get_details.side_effect = shawshank
    await run_backfill(db, monkeypatch, provider)
    assert await db.scalar(select(MediaItem.title)) == "The Shawshank Redemption"


@pytest.mark.asyncio
async def test_backfill_skips_watched_placeholders_with_a_recent_miss(context, monkeypatch):
    client, db, _ = context
    await client.post(f"{BASE}/watch_state/push/movie/tt0111161.json", json=movie_event())
    media = await db.scalar(select(MediaItem))
    media.raw = {**media.raw, metadata_enrichment.LOOKUP_MISS_KEY: datetime.now(timezone.utc).isoformat()}
    await db.commit()
    provider = tmdb_provider()
    await run_backfill(db, monkeypatch, provider)
    provider.get_details.assert_not_awaited()


@pytest.mark.asyncio
async def test_backfill_rechecks_the_queue_and_commits_each_title(context, monkeypatch):
    _, db, _ = context
    first, second, third = placeholder("tt0000001", "1"), placeholder("tt0000002", "2"), placeholder("tt0000003", "3")
    bucket = tmdb_bucket(150.0)
    db.add_all([first, second, third, bucket])
    await db.commit()
    ordered = sorted([first, second, third], key=lambda item: item.id)

    async def details(provider_id, scope):
        # While the first title is looked up, an event queues a lookup job for the second.
        if not await db.scalar(select(OutboxJob.id)):
            await watch_pipeline.enqueue_metadata_update_job(db, "user-one", ordered[1].id)
        return None

    provider = tmdb_provider()
    provider.get_details.side_effect = details
    commit = AsyncMock(wraps=db.commit)
    monkeypatch.setattr(db, "commit", commit)
    await run_backfill(db, monkeypatch, provider)
    assert provider.get_details.await_count == 2
    assert commit.await_count == 3


@pytest.mark.asyncio
async def test_backfill_stops_at_the_rate_limit_without_recording(context, monkeypatch):
    _, db, _ = context
    for number in (1, 2, 3):
        db.add(placeholder(f"tt000000{number}", str(number)))
    # An empty bucket: the real limiter refuses the first request.
    bucket = tmdb_bucket(0.0)
    db.add(bucket)
    await db.commit()
    acquire = AsyncMock(wraps=metadata_enrichment.RATE_LIMITER.try_acquire)
    monkeypatch.setattr(metadata_enrichment.RATE_LIMITER, "try_acquire", acquire)
    provider = tmdb_provider()
    await run_backfill(db, monkeypatch, provider)
    assert acquire.await_count == 1
    provider.get_details.assert_not_awaited()
    for media in (await db.scalars(select(MediaItem))).all():
        assert media.metadata_refreshed_at is None
        assert metadata_enrichment.LOOKUP_MISS_KEY not in media.raw


@pytest.mark.asyncio
async def test_undo_events_and_users_without_providers_queue_no_lookup(context, monkeypatch):
    client, db, _ = context
    url = f"{BASE}/watch_state/push/movie/tt0111161.json"
    query = select(OutboxJob).where(OutboxJob.job_type == "metadata_update")
    await client.post(url, json=movie_event("watchlisted"))
    await finish_lookup_jobs(db)
    await client.post(url, json=movie_event("unwatchlisted", at=AT + 1))
    await client.post(url, json=movie_event("unrated", at=AT + 2))
    assert len((await db.scalars(query)).all()) == 1
    monkeypatch.setattr(watch_state_events, "has_metadata_provider", metadata_providers.has_metadata_provider)
    await client.post(url, json=movie_event("watchlisted", id="watchlisted-again", at=AT + 3))
    assert len((await db.scalars(query)).all()) == 1


@pytest.mark.asyncio
async def test_has_metadata_provider_needs_an_enabled_provider_with_credentials(context):
    _, db, _ = context
    providers = metadata_enrichment.ENRICHMENT_PROVIDERS
    assert not await metadata_providers.has_metadata_provider(db, "user-one", providers)
    tmdb = Integration(user_id="user-one", provider="tmdb", config={"enabled": True})
    imdb = Integration(user_id="user-two", provider="imdb", config={"enabled": True})
    db.add_all([tmdb, imdb, Integration(user_id="user-one", provider="imdb", config={"enabled": False})])
    await db.flush()
    assert not await metadata_providers.has_metadata_provider(db, "user-one", providers)
    assert await metadata_providers.has_metadata_provider(db, "user-two", providers)
    db.add(IntegrationSecret(integration_id=tmdb.id, secret_data="encrypted"))
    await db.flush()
    assert await metadata_providers.has_metadata_provider(db, "user-one", providers)


def test_admin_merge_does_not_carry_a_lookup_miss_over():
    marker = {metadata_enrichment.LOOKUP_MISS_KEY: "2026-01-01T00:00:00+00:00"}
    target = MediaItem(media_type="movie", title="The Shawshank Redemption", raw={"source": "trakt"})
    source = MediaItem(media_type="movie", title="tt0111161", raw={"stremio_id": "tt0111161", **marker})
    routes_admin._merge_media_item_fields(target, source)
    assert target.raw == {"source": "trakt", "stremio_id": "tt0111161"}
    empty = MediaItem(media_type="movie", title="The Shawshank Redemption", raw=None)
    routes_admin._merge_media_item_fields(empty, source)
    assert empty.raw == {"stremio_id": "tt0111161"}
