import copy
from dataclasses import replace
from datetime import date, datetime, timezone

import httpx
import pytest
import pytest_asyncio
from librarysync.connectors.services import simkl_calendar as connector
from librarysync.connectors.services.simkl import SimklError
from librarysync.core.next_episode import episode_to_payload, find_next_episode
from librarysync.db.models import Base, EpisodeItem, MediaItem, ScheduledJob, User, WatchedItem
from librarysync.jobs import simkl_calendar as job
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

PAYLOAD = {
    "calendar": [
        {
            "simkl_id": 3437,
            "date": "2026-07-27T04:00:00Z",
            "finale_type": 2,
            "episode": {"season": 15, "episode": 10, "title": "Propane Recall"},
        }
    ],
    "metadata": {"3437": {"title": "King of the Hill", "ids": {"simkl_id": 3437, "imdb": "tt0118375"}}},
}


@pytest_asyncio.fixture
async def factory():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as db:
        await db.run_sync(Base.metadata.create_all)
    yield async_sessionmaker(engine, autoflush=False, expire_on_commit=False)
    await engine.dispose()


def test_parses_actual_v2_shape_and_joins_metadata():
    [airing] = connector.parse_calendar(PAYLOAD, "tv")
    assert airing.show_ids == {"simkl": "3437", "imdb": "tt0118375"}
    assert (airing.season, airing.number, airing.finale_type) == (15, 10, 2)
    assert airing.aired_at == datetime(2026, 7, 27, 4, tzinfo=timezone.utc)


def test_rejects_old_calendar_shape_and_skips_malformed_airings():
    with pytest.raises(SimklError):
        connector.parse_calendar(PAYLOAD["calendar"], "tv")
    payload = copy.deepcopy(PAYLOAD)
    payload["calendar"] += [None, {"simkl_id": 999}, {**PAYLOAD["calendar"][0], "date": "invalid"}]
    assert len(connector.parse_calendar(payload, "tv")) == 1


@pytest.mark.asyncio
async def test_fetches_public_v2_url_with_app_identity_without_oauth(monkeypatch):
    requests = []

    def respond(request):
        requests.append(request)
        return httpx.Response(200, json=PAYLOAD)

    from librarysync.core.http_client import get_http_client

    monkeypatch.setattr(
        connector, "get_http_client", lambda **kwargs: get_http_client(transport=httpx.MockTransport(respond), **kwargs)
    )
    assert len(await connector.fetch_calendar("calendar-client", "tv")) == 1
    assert len(await connector.fetch_calendar("calendar-client", "tv", date(2026, 7, 1))) == 1
    assert requests[0].url.path == "/calendar/v2/tv.json"
    assert requests[1].url.path == "/calendar/v2/2026/7/tv.json"
    assert requests[0].url.params["client_id"] == "calendar-client"
    assert requests[0].url.params["app-name"] == "librarySync"
    assert requests[0].url.params["app-version"]
    assert requests[0].headers["user-agent"].startswith("librarySync Version/")
    assert "authorization" not in requests[0].headers


async def _seed(factory):
    async with factory() as db:
        db.add(User(id="u", username="u", password_hash="unused"))
        db.add(MediaItem(id="show", media_type="tv", title="King of the Hill", imdb_id="tt0118375"))
        await db.commit()
        db.add(
            EpisodeItem(
                id="last-watched",
                show_media_item_id="show",
                season_number=15,
                episode_number=9,
                air_date=date(2026, 7, 20),
            )
        )
        await db.commit()
        db.add(WatchedItem(user_id="u", episode_item_id="last-watched", watched_at=datetime.now(timezone.utc)))
        await db.commit()


def _mock_files(monkeypatch, payload=PAYLOAD):
    calls = []

    async def fetch(client_id, catalog, month):
        calls.append((catalog, month))
        return connector.parse_calendar(payload, catalog) if catalog == "tv" else []

    monkeypatch.setattr(job, "fetch_calendar", fetch)
    return calls


@pytest.mark.asyncio
async def test_calendar_airing_reaches_up_next_without_importing_watches(factory, monkeypatch):
    await _seed(factory)
    calls = _mock_files(monkeypatch)
    async with factory() as db:
        await job.refresh_simkl_calendar(db, "client", date(2026, 8, 1))
        await db.commit()
        episode = await find_next_episode(db, "u", "show", date(2026, 8, 1))
        assert episode_to_payload(episode)["finale_type"] == 2
        assert episode.air_date == date(2026, 7, 27)
        assert episode.title == "Propane Recall"
        assert len(list((await db.scalars(select(WatchedItem))).all())) == 1
        assert len(list((await db.scalars(select(MediaItem))).all())) == 1
    assert calls[:3] == [("tv", date(2026, 7, 31)), ("tv", date(2026, 8, 1)), ("tv", None)]


@pytest.mark.asyncio
async def test_refresh_clears_stale_finale_and_keeps_other_provider_data(factory, monkeypatch):
    await _seed(factory)
    _mock_files(monkeypatch)
    async with factory() as db:
        await job.refresh_simkl_calendar(db, "client")
        await db.commit()
        episode = await db.scalar(select(EpisodeItem).where(EpisodeItem.episode_number == 10))
        episode.raw = {**episode.raw, "tmdb": {"id": 123}, "simkl": {"finale_type": 3}}
        await db.commit()
    payload = copy.deepcopy(PAYLOAD)
    payload["calendar"][0]["finale_type"] = None
    _mock_files(monkeypatch, payload)
    async with factory() as db:
        await job.refresh_simkl_calendar(db, "client")
        await db.commit()
        episode = await db.scalar(select(EpisodeItem).where(EpisodeItem.episode_number == 10))
        assert episode_to_payload(episode)["finale_type"] is None
        assert episode.raw["tmdb"] == {"id": 123}
        assert len(list((await db.scalars(select(EpisodeItem))).all())) == 2


@pytest.mark.asyncio
async def test_skips_conflicting_show_ids_and_does_not_create_unknown_shows(factory, monkeypatch):
    await _seed(factory)
    async with factory() as db:
        db.add(MediaItem(id="different", media_type="tv", title="Different", tmdb_id="1434"))
        await db.commit()
    payload = copy.deepcopy(PAYLOAD)
    payload["metadata"]["3437"]["ids"]["tmdb"] = 1434
    _mock_files(monkeypatch, payload)
    async with factory() as db:
        assert await job.refresh_simkl_calendar(db, "client") == 0
        assert len(list((await db.scalars(select(EpisodeItem))).all())) == 1


@pytest.mark.asyncio
async def test_does_not_guess_anime_seasons_from_absolute_numbering(factory, monkeypatch):
    await _seed(factory)
    payload = copy.deepcopy(PAYLOAD)
    del payload["calendar"][0]["episode"]["season"]

    async def fetch(client_id, catalog, month):
        return connector.parse_calendar(payload, catalog) if catalog == "anime" else []

    monkeypatch.setattr(job, "fetch_calendar", fetch)
    async with factory() as db:
        assert await job.refresh_simkl_calendar(db, "client") == 0
        assert len(list((await db.scalars(select(EpisodeItem))).all())) == 1


@pytest.mark.asyncio
async def test_anime_absolute_number_updates_an_existing_single_season_episode(factory, monkeypatch):
    await _seed(factory)
    async with factory() as db:
        episode = await db.get(EpisodeItem, "last-watched")
        episode.season_number = 1
        episode.episode_number = 10
        await db.commit()
    payload = copy.deepcopy(PAYLOAD)
    del payload["calendar"][0]["episode"]["season"]

    async def fetch(client_id, catalog, month):
        return connector.parse_calendar(payload, catalog) if catalog == "anime" else []

    monkeypatch.setattr(job, "fetch_calendar", fetch)
    async with factory() as db:
        await job.refresh_simkl_calendar(db, "client")
        await db.commit()
        episode = await db.get(EpisodeItem, "last-watched")
        assert episode_to_payload(episode)["finale_type"] == 2
        assert episode.air_date == date(2026, 7, 27)
        assert len(list((await db.scalars(select(EpisodeItem))).all())) == 1


@pytest.mark.asyncio
async def test_rolling_calendar_overrides_archived_dates_and_finale_markers(factory, monkeypatch):
    await _seed(factory)

    async def fetch(client_id, catalog, month):
        if catalog != "tv":
            return []
        payload = copy.deepcopy(PAYLOAD)
        if month is None:
            payload["calendar"][0].update(date="2026-08-03T04:00:00Z", finale_type=3)
        return connector.parse_calendar(payload, catalog)

    monkeypatch.setattr(job, "fetch_calendar", fetch)
    async with factory() as db:
        await job.refresh_simkl_calendar(db, "client", date(2026, 8, 1))
        await db.commit()
        assert await find_next_episode(db, "u", "show", date(2026, 8, 1)) is None
        episode = await find_next_episode(db, "u", "show", date(2026, 8, 3))
        assert episode_to_payload(episode)["finale_type"] == 3
        assert episode.air_date == date(2026, 8, 3)


@pytest.mark.asyncio
async def test_worker_refreshes_calendar_when_metadata_backfill_is_not_due(factory, monkeypatch):
    from librarysync.jobs import metadata_backfill

    calls = []

    async def process():
        calls.append("calendar")
        return 1

    async def not_due(*args):
        return None

    monkeypatch.setattr(metadata_backfill, "process_simkl_calendar_once", process)
    monkeypatch.setattr(metadata_backfill, "claim_scheduled_job", not_due)
    monkeypatch.setattr(metadata_backfill, "init_session_factory", lambda: None)
    monkeypatch.setattr(metadata_backfill, "SessionLocal", factory)
    assert await metadata_backfill.process_metadata_backfill_once() == 1
    assert calls == ["calendar"]


@pytest.mark.asyncio
async def test_scheduled_refresh_runs_once_and_retries_failures(factory, monkeypatch):
    await _seed(factory)
    calls = _mock_files(monkeypatch)
    monkeypatch.setattr(job, "settings", replace(job.settings, simkl_client_id="client"))
    monkeypatch.setattr(job, "init_session_factory", lambda: None)
    monkeypatch.setattr(job, "SessionLocal", factory)
    assert await job.process_simkl_calendar_once() == 1
    assert await job.process_simkl_calendar_once() == 0
    assert len(calls) == 6
    async with factory() as db:
        scheduled = await db.get(ScheduledJob, job.CALENDAR_JOB)
        scheduled.next_run_at = datetime(2020, 1, 1, tzinfo=timezone.utc)
        await db.commit()

    async def fail(*args):
        raise SimklError("Unreachable calendar")

    monkeypatch.setattr(job, "fetch_calendar", fail)
    assert await job.process_simkl_calendar_once() == 0
    assert await job.process_simkl_calendar_once() == 0
    async with factory() as db:
        scheduled = await db.get(ScheduledJob, job.CALENDAR_JOB)
        assert scheduled.lease_owner is None
        assert scheduled.next_run_at.replace(tzinfo=timezone.utc) > datetime.now(timezone.utc)
        assert len(list((await db.scalars(select(EpisodeItem))).all())) == 2


@pytest.mark.asyncio
async def test_no_calendar_client_id_does_not_fetch(monkeypatch):
    monkeypatch.setattr(job, "settings", replace(job.settings, simkl_client_id=None))
    assert await job.process_simkl_calendar_once() == 0
