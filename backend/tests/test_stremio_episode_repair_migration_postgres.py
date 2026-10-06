"""Repair migration for Stremio bitfield imports that linked every episode to the last one."""

import os
import uuid
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest
from alembic import command
from alembic.config import Config
from librarysync.db import session
from librarysync.db.models import EpisodeItem, MediaItem, User, WatchedItem, WatchEvent, WatchSync
from sqlalchemy import create_engine, insert, text

DATABASE = os.environ.get("WATCH_STATE_TEST_DATABASE_URL")
BEFORE = "8e9f0a1b2c3d"
REPAIR = "fb3b3e7932d5"


def _seed(connection) -> None:
    watched_at = datetime(2026, 1, 10, 20, 0, tzinfo=timezone.utc)
    connection.execute(insert(User).values(id="u", username="u", password_hash="x"))
    connection.execute(insert(MediaItem).values(id="show", media_type="tv", title="Show"))
    connection.execute(insert(MediaItem).values(id="other", media_type="tv", title="Other"))
    episodes = [("ep1", "show", 1), ("ep2", "show", 2), ("ep3", "show", 3), ("other-ep2", "other", 2)]
    for episode_id, show_id, number in episodes:
        connection.execute(
            insert(EpisodeItem).values(
                id=episode_id, show_media_item_id=show_id, season_number=1, episode_number=number
            )
        )
    # Buggy import: three watches all linked to ep3, each Stremio sync carrying the real video id.
    # A lone watch whose numbering differs has no buggy siblings and must not be touched.
    rows = [
        ("w1", "ep3", "tt1:1:1"),
        ("w2", "ep3", "tt1:1:2"),
        ("w3", "ep3", "tt1:1:3"),
        ("w-lone", "other-ep2", "tt2:1:1"),
    ]
    for watched_id, episode_id, video_id in rows:
        connection.execute(
            insert(WatchedItem).values(
                id=watched_id, user_id="u", episode_item_id=episode_id, watched_at=watched_at, source="stremio"
            )
        )
        connection.execute(
            insert(WatchSync).values(
                id=f"sync-{watched_id}",
                user_id="u",
                watched_item_id=watched_id,
                provider="stremio",
                status="synced_from_stremio",
                external_id=video_id,
            )
        )
        connection.execute(
            insert(WatchEvent).values(
                id=f"event-{watched_id}",
                user_id="u",
                episode_item_id=episode_id,
                event_type="stremio_imported",
                entry_key=f"key-{watched_id}",
                occurred_at=watched_at,
                raw={"video_id": video_id},
            )
        )
    connection.execute(
        insert(WatchSync).values(id="trakt-w1", user_id="u", watched_item_id="w1", provider="trakt", status="synced")
    )


@pytest.mark.skipif(not DATABASE, reason="Isolated PostgreSQL test URL required")
def test_repair_moves_misattributed_stremio_episode_watches(monkeypatch):
    schema = "stremio_repair_" + uuid.uuid4().hex
    admin = create_engine(DATABASE)
    with admin.begin() as connection:
        connection.execute(text(f'CREATE SCHEMA "{schema}"'))
    url = DATABASE + "?options=-csearch_path%3D" + schema
    monkeypatch.setattr(session, "settings", SimpleNamespace(database_url=url))
    cfg = Config()
    cfg.set_main_option("script_location", str(Path(__file__).parents[1] / "src/librarysync/db/migrations"))
    engine = create_engine(url)
    try:
        command.upgrade(cfg, BEFORE)
        with engine.begin() as connection:
            _seed(connection)
        command.upgrade(cfg, REPAIR)
        with engine.connect() as connection:
            links = dict(connection.execute(text("SELECT id, episode_item_id FROM watched_items")).all())
            assert links == {"w1": "ep1", "w2": "ep2", "w3": "ep3", "w-lone": "other-ep2"}
            events = dict(connection.execute(text("SELECT id, episode_item_id FROM watch_events")).all())
            assert events == {
                "event-w1": "ep1",
                "event-w2": "ep2",
                "event-w3": "ep3",
                "event-w-lone": "other-ep2",
            }
            providers = (
                connection.execute(
                    text("SELECT provider FROM watch_syncs WHERE watched_item_id = 'w1' ORDER BY provider")
                )
                .scalars()
                .all()
            )
            assert providers == ["stremio"]
            jobs = (
                connection.execute(
                    text(
                        "SELECT payload ->> 'watched_item_id' FROM outbox "
                        "WHERE job_type = 'new_item_added' AND status = 'pending' ORDER BY 1"
                    )
                )
                .scalars()
                .all()
            )
            assert jobs == ["w1", "w2"]
    finally:
        engine.dispose()
        with admin.begin() as connection:
            connection.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
        admin.dispose()
