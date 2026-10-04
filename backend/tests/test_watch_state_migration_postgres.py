"""Upgrade a real pre-Watch-State PostgreSQL schema, including retired credentials."""

import os
import uuid
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest
from alembic import command
from alembic.config import Config
from librarysync.db import session
from librarysync.db.models import Integration, IntegrationSecret, MediaItem, User, WatchedItem
from sqlalchemy import create_engine, insert, select, text

DATABASE = os.environ.get("WATCH_STATE_TEST_DATABASE_URL")


@pytest.mark.skipif(not DATABASE, reason="Isolated PostgreSQL test URL required")
def test_postgres_upgrade_and_downgrade_preserve_imported_history(monkeypatch):
    schema = "watch_state_migration_" + uuid.uuid4().hex
    admin = create_engine(DATABASE)
    with admin.begin() as connection:
        connection.execute(text(f'CREATE SCHEMA "{schema}"'))
    url = DATABASE + "?options=-csearch_path%3D" + schema
    monkeypatch.setattr(session, "settings", SimpleNamespace(database_url=url))
    cfg = Config()
    cfg.set_main_option("script_location", str(Path(__file__).parents[1] / "src/librarysync/db/migrations"))
    engine = create_engine(url)
    try:
        command.upgrade(cfg, "7d8e9f0a1b2c")
        with engine.begin() as connection:
            connection.execute(insert(User).values(id="u", username="u", password_hash="test"))
            connection.execute(insert(MediaItem).values(id="m", media_type="movie", title="Kept"))
            connection.execute(
                insert(WatchedItem).values(
                    id="w",
                    user_id="u",
                    media_item_id="m",
                    watched_at=datetime.now(timezone.utc),
                    source="aiostreams",
                )
            )
            connection.execute(insert(Integration).values(id="proxy", user_id="u", provider="aiostreams"))
            connection.execute(insert(IntegrationSecret).values(integration_id="proxy", secret_data="encrypted"))
        command.upgrade(cfg, "head")
        with engine.connect() as connection:
            assert connection.scalar(select(WatchedItem.id)) == "w"
            assert connection.scalar(select(IntegrationSecret.id)) is None
            assert connection.scalar(select(Integration.status)) == "retired"
        command.downgrade(cfg, "7d8e9f0a1b2c")
        with engine.connect() as connection:
            assert connection.scalar(select(WatchedItem.id)) == "w"
    finally:
        engine.dispose()
        with admin.begin() as connection:
            connection.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
        admin.dispose()
