"""Regression tests for external-ID unique-constraint conflicts.

Covers the worker outages caused by unguarded ID stamping:
- watchlist upsert filling tmdb_id/tvdb_id already held by another media item
- episode list persist filling a tmdb_id held by an episode of another season
- scheduled-job error handlers committing on a poisoned session
"""

from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from librarysync.core.scheduler import fail_scheduled_job
from librarysync.core.watchlist import (
    _persist_episode_list_for_media_item,
    upsert_watchlist_item,
)
from librarysync.db.models import (
    Base,
    EpisodeItem,
    MediaItem,
    ScheduledJob,
    User,
)
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

NOW = datetime(2026, 7, 20, 12, 0, 0, tzinfo=timezone.utc)


async def _make_session():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    return engine, async_sessionmaker(engine, expire_on_commit=False)


@pytest.mark.asyncio
async def test_upsert_watchlist_item_skips_conflicting_ids() -> None:
    """Two media rows for the same show (different imdb ids): an import carrying
    ids that match both must not stamp tmdb/tvdb already held by the other row."""
    engine, session_factory = await _make_session()
    async with session_factory() as db:
        db.add(User(id="user-1", username="user1", password_hash="x"))
        db.add(
            MediaItem(
                id="show-a",
                media_type="tv",
                title="Drops of God",
                year=2023,
                imdb_id="tt6867482",
            )
        )
        db.add(
            MediaItem(
                id="show-b",
                media_type="tv",
                title="Drops of God",
                year=2023,
                imdb_id="tt15282746",
                tmdb_id="218961",
                tvdb_id="409159",
            )
        )
        await db.commit()

        item, status = await upsert_watchlist_item(
            db,
            "user-1",
            "tv",
            {"imdb_id": "tt6867482", "tmdb_id": "218961", "tvdb_id": "409159"},
            "Drops of God",
            2023,
            None,
            "trakt",
            now=NOW,
        )
        await db.commit()

        assert item is not None
        assert status == "created"
        show_a = (await db.execute(select(MediaItem).where(MediaItem.id == "show-a"))).scalars().one()
        show_b = (await db.execute(select(MediaItem).where(MediaItem.id == "show-b"))).scalars().one()
        # The conflicting ids must not be duplicated onto the other row.
        assert show_a.tmdb_id is None
        assert show_a.tvdb_id is None
        assert show_b.tmdb_id == "218961"
        assert show_b.tvdb_id == "409159"
    await engine.dispose()


@pytest.mark.asyncio
async def test_upsert_watchlist_item_fills_unconflicted_ids() -> None:
    engine, session_factory = await _make_session()
    async with session_factory() as db:
        db.add(User(id="user-1", username="user1", password_hash="x"))
        db.add(
            MediaItem(
                id="show-a",
                media_type="tv",
                title="Some Show",
                year=2020,
                imdb_id="tt0944947",
            )
        )
        await db.commit()

        item, status = await upsert_watchlist_item(
            db,
            "user-1",
            "tv",
            {"imdb_id": "tt0944947", "tmdb_id": "1399", "tvdb_id": "121361"},
            "Some Show",
            2020,
            None,
            "trakt",
            now=NOW,
        )
        await db.commit()

        assert item is not None
        assert status == "created"
        show_a = (await db.execute(select(MediaItem).where(MediaItem.id == "show-a"))).scalars().one()
        assert show_a.tmdb_id == "1399"
        assert show_a.tvdb_id == "121361"
    await engine.dispose()


def _tmdb_episode(number: int, provider_id: str, title: str, air_date: str):
    return SimpleNamespace(
        episode_number=number,
        provider_id=provider_id,
        title=title,
        air_date=air_date,
        still_url=None,
    )


@pytest.mark.asyncio
async def test_persist_episode_list_skips_tmdb_id_held_by_other_season() -> None:
    """TMDB id already attached to a season-0 row must not be stamped onto the
    season-6 row with the same episode number."""
    engine, session_factory = await _make_session()
    async with session_factory() as db:
        db.add(MediaItem(id="show-1", media_type="tv", title="Some Show", tmdb_id="100"))
        db.add(
            EpisodeItem(
                id="ep-s0",
                show_media_item_id="show-1",
                season_number=0,
                episode_number=1,
                title="One for the Road",
                tmdb_id="5582066",
            )
        )
        db.add(
            EpisodeItem(
                id="ep-s6",
                show_media_item_id="show-1",
                season_number=6,
                episode_number=1,
            )
        )
        await db.commit()

        dirty = await _persist_episode_list_for_media_item(
            db,
            (await db.execute(select(MediaItem).where(MediaItem.id == "show-1"))).scalars().one(),
            "tmdb",
            "100",
            6,
            [_tmdb_episode(1, "5582066", "One for the Road", "2024-09-13")],
        )
        await db.commit()

        assert dirty is True
        ep_s6 = (await db.execute(select(EpisodeItem).where(EpisodeItem.id == "ep-s6"))).scalars().one()
        assert ep_s6.title == "One for the Road"
        assert ep_s6.air_date == date(2024, 9, 13)
        assert ep_s6.tmdb_id is None
        ep_s0 = (await db.execute(select(EpisodeItem).where(EpisodeItem.id == "ep-s0"))).scalars().one()
        assert ep_s0.tmdb_id == "5582066"
    await engine.dispose()


@pytest.mark.asyncio
async def test_persist_episode_list_insert_skips_conflicting_tmdb_id() -> None:
    engine, session_factory = await _make_session()
    async with session_factory() as db:
        db.add(MediaItem(id="show-1", media_type="tv", title="Some Show", tmdb_id="100"))
        db.add(
            EpisodeItem(
                id="ep-s0",
                show_media_item_id="show-1",
                season_number=0,
                episode_number=1,
                title="One for the Road",
                tmdb_id="5582066",
            )
        )
        await db.commit()

        await _persist_episode_list_for_media_item(
            db,
            (await db.execute(select(MediaItem).where(MediaItem.id == "show-1"))).scalars().one(),
            "tmdb",
            "100",
            7,
            [_tmdb_episode(1, "5582066", "One for the Road", "2024-09-13")],
        )
        await db.commit()

        new_ep = (
            await db.execute(
                select(EpisodeItem).where(
                    EpisodeItem.show_media_item_id == "show-1",
                    EpisodeItem.season_number == 7,
                    EpisodeItem.episode_number == 1,
                )
            )
        ).scalars().one()
        assert new_ep.title == "One for the Road"
        assert new_ep.tmdb_id is None
    await engine.dispose()


@pytest.mark.asyncio
async def test_persist_episode_list_assigns_unconflicted_tmdb_id() -> None:
    engine, session_factory = await _make_session()
    async with session_factory() as db:
        db.add(MediaItem(id="show-1", media_type="tv", title="Some Show", tmdb_id="100"))
        db.add(
            EpisodeItem(
                id="ep-s1",
                show_media_item_id="show-1",
                season_number=1,
                episode_number=1,
            )
        )
        await db.commit()

        await _persist_episode_list_for_media_item(
            db,
            (await db.execute(select(MediaItem).where(MediaItem.id == "show-1"))).scalars().one(),
            "tmdb",
            "100",
            1,
            [_tmdb_episode(1, "123456", "Pilot", "2020-01-01")],
        )
        await db.commit()

        ep = (await db.execute(select(EpisodeItem).where(EpisodeItem.id == "ep-s1"))).scalars().one()
        assert ep.tmdb_id == "123456"
    await engine.dispose()


@pytest.mark.asyncio
async def test_fail_scheduled_job_recovers_from_poisoned_session() -> None:
    """A failed flush leaves the session unusable; fail_scheduled_job must roll
    back and still persist the retry schedule."""
    engine, session_factory = await _make_session()
    async with session_factory() as db:
        job = ScheduledJob(
            name="metadata_backfill",
            next_run_at=NOW - timedelta(hours=1),
            lease_until=NOW + timedelta(hours=2),
            lease_owner="worker-1",
        )
        db.add(job)
        db.add(User(id="user-1", username="taken", password_hash="x"))
        await db.commit()
        job_name = job.name

        # Poison the session: duplicate username violates the unique constraint.
        db.add(User(id="user-2", username="taken", password_hash="y"))
        with pytest.raises(Exception):
            await db.flush()

        await fail_scheduled_job(db, job_name, timedelta(minutes=10), NOW)

        refreshed = await db.get(ScheduledJob, job_name)
        assert refreshed is not None
        assert refreshed.next_run_at == NOW + timedelta(minutes=10)
        assert refreshed.lease_until is None
        assert refreshed.lease_owner is None

    # The retry schedule must be committed, not just pending.
    async with session_factory() as db:
        persisted = await db.get(ScheduledJob, job_name)
        assert persisted is not None
        assert persisted.lease_until is None
        assert persisted.next_run_at.replace(tzinfo=timezone.utc) == NOW + timedelta(minutes=10)
    await engine.dispose()
