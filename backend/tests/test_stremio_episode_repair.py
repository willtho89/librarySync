"""Stremio bitfield repair logic, run on SQLite (the migration itself is PostgreSQL-only)."""

import asyncio
import importlib.util
from datetime import datetime, timezone
from pathlib import Path

import pytest
from librarysync.db.models import Base, OutboxJob, WatchedItem, WatchEvent, WatchSync
from librarysync.jobs.merge_history import merge_history_for_user
from sqlalchemy import create_engine, delete, insert, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from test_stremio_episode_repair_migration_postgres import _seed

MIGRATION = (
    Path(__file__).parents[1]
    / "src/librarysync/db/migrations/versions/fb3b3e7932d5_repair_stremio_bitfield_episodes.py"
)


def _load_migration():
    spec = importlib.util.spec_from_file_location("stremio_repair_migration", MIGRATION)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def database(tmp_path):
    path = tmp_path / "repair.db"
    engine = create_engine(f"sqlite:///{path}")
    Base.metadata.create_all(engine)
    with engine.begin() as connection:
        _seed(connection)
    yield engine, f"sqlite+aiosqlite:///{path}"
    engine.dispose()


def _repair(engine) -> None:
    with engine.begin() as connection:
        _load_migration().repair(connection)


def _merge(async_url: str) -> int:
    async def _run() -> int:
        async_engine = create_async_engine(async_url)
        factory = async_sessionmaker(async_engine, autoflush=False, expire_on_commit=False)
        async with factory() as db:
            merged = await merge_history_for_user(db, "u")
        await async_engine.dispose()
        return merged

    return asyncio.run(_run())


def _state(engine):
    with engine.connect() as connection:
        watches = connection.execute(
            select(WatchedItem.id, WatchedItem.episode_item_id).where(WatchedItem.source == "stremio")
        ).all()
        syncs = connection.execute(select(WatchSync.watched_item_id, WatchSync.provider, WatchSync.external_id)).all()
        events = dict(connection.execute(select(WatchEvent.id, WatchEvent.episode_item_id)).all())
        jobs = connection.execute(
            select(OutboxJob.payload).where(OutboxJob.job_type == "new_item_added", OutboxJob.status == "pending")
        ).scalars()
        return watches, syncs, events, sorted(job["watched_item_id"] for job in jobs)


def test_unmerged_watches_move_to_the_episode_their_import_named(database):
    engine, _ = database
    _repair(engine)

    watches, syncs, events, jobs = _state(engine)
    assert dict(watches) == {"w1": "ep1", "w2": "ep2", "w3": "ep3", "w-lone": "other-ep2"}
    assert events == {"event-w1": "ep1", "event-w2": "ep2", "event-w3": "ep3", "event-w-lone": "other-ep2"}
    assert ("w1", "trakt", None) not in syncs
    assert jobs == ["w1", "w2"]


def test_watches_already_merged_by_history_merge_are_recreated(database):
    engine, async_url = database
    assert _merge(async_url) == 2

    _repair(engine)

    watches, syncs, events, jobs = _state(engine)
    by_episode = {}
    for watched_id, episode_id in watches:
        by_episode.setdefault(episode_id, []).append(watched_id)
    assert sorted(by_episode) == ["ep1", "ep2", "ep3", "other-ep2"]
    assert all(len(ids) == 1 for ids in by_episode.values())
    [survivor] = by_episode["ep3"]
    stremio_ids = {(watched_id, external_id) for watched_id, provider, external_id in syncs if provider == "stremio"}
    assert (survivor, "tt1:1:3") in stremio_ids
    assert (by_episode["ep1"][0], "tt1:1:1") in stremio_ids
    assert (by_episode["ep2"][0], "tt1:1:2") in stremio_ids
    # The survivor's downstream deliveries were for this episode and stay; new watches are delivered.
    assert any(watched_id == survivor and provider == "trakt" for watched_id, provider, _ in syncs)
    assert jobs == sorted([by_episode["ep1"][0], by_episode["ep2"][0]])
    assert events == {"event-w1": "ep1", "event-w2": "ep2", "event-w3": "ep3", "event-w-lone": "other-ep2"}


def test_repair_is_idempotent(database):
    engine, async_url = database
    _merge(async_url)
    _repair(engine)
    first = _state(engine)
    _repair(engine)
    assert _state(engine) == first


SEEDED_AT = datetime(2026, 1, 10, 20, 0, tzinfo=timezone.utc)


def _delete_watch(engine, watched_id: str, linked_episode: str, previous_watched_at=SEEDED_AT, deleted_at=None) -> None:
    """What the history routes do: delete the watch and record an audit event against its linked episode."""
    with engine.begin() as connection:
        connection.execute(delete(WatchSync).where(WatchSync.watched_item_id == watched_id))
        connection.execute(delete(WatchedItem).where(WatchedItem.id == watched_id))
        connection.execute(
            insert(WatchEvent).values(
                id=f"deleted-{watched_id}",
                user_id="u",
                episode_item_id=linked_episode,
                event_type="manual_watched_deleted",
                occurred_at=deleted_at or datetime.now(timezone.utc),
                raw={"watched_id": watched_id, "previous_watched_at": previous_watched_at.isoformat()},
            )
        )


def _episodes(engine) -> list[str]:
    watches, _, _, _ = _state(engine)
    return sorted(episode_id for _, episode_id in watches)


def test_deleted_unmerged_watch_is_not_recreated_on_its_real_episode(database):
    engine, _ = database
    _delete_watch(engine, "w1", "ep3")

    _repair(engine)

    watches, _, events, jobs = _state(engine)
    assert dict(watches) == {"w2": "ep2", "w3": "ep3", "w-lone": "other-ep2"}
    assert jobs == ["w2"]
    # The event still moves, so a later import does not bring the deleted watch back.
    assert events["event-w1"] == "ep1"


def test_deleted_merge_survivor_is_not_recreated(database):
    engine, async_url = database
    _merge(async_url)
    [survivor] = [watched_id for watched_id, episode_id in _state(engine)[0] if episode_id == "ep3"]
    _delete_watch(engine, survivor, "ep3")

    _repair(engine)

    assert _episodes(engine) == ["other-ep2"]
    assert _state(engine)[3] == []


def test_older_deletion_of_the_episode_does_not_block_the_repair(database):
    engine, async_url = database
    with engine.begin() as connection:
        connection.execute(
            insert(WatchEvent).values(
                id="deleted-2025",
                user_id="u",
                episode_item_id="ep2",
                event_type="manual_watched_deleted",
                occurred_at=datetime(2025, 5, 1, tzinfo=timezone.utc),
                raw={"watched_id": "old", "previous_watched_at": "2025-04-30T20:00:00+00:00"},
            )
        )
    _merge(async_url)

    _repair(engine)

    assert _episodes(engine) == ["ep1", "ep2", "ep3", "other-ep2"]
