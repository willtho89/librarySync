from importlib import import_module
from pathlib import Path

from alembic.migration import MigrationContext
from alembic.operations import Operations
from librarysync.core.import_all import DEFAULT_IMPORT_QUEUE_ORDER, IMPORT_ALL_PRIORITY
from librarysync.db.models import Base, Integration, IntegrationSecret, MediaItem, User, WatchedItem
from librarysync.jobs.imports import IMPORT_ALL_REGISTRY, QUICK_IMPORT_REGISTRY
from sqlalchemy import MetaData, create_engine, insert, select


def test_proxy_importer_and_configuration_are_removed():
    root = Path(__file__).parents[1] / "src/librarysync"
    assert not (root / "connectors/services/aiostreams_proxy.py").exists()
    assert not (root / "jobs/aiostreams_import.py").exists()
    assert "aiostreams" not in DEFAULT_IMPORT_QUEUE_ORDER
    assert "aiostreams" not in IMPORT_ALL_PRIORITY
    assert QUICK_IMPORT_REGISTRY.get("aiostreams") is None
    assert IMPORT_ALL_REGISTRY.get("aiostreams") is None
    assert "aiostreams-form" not in (root / "templates/settings/providers.html").read_text()
    assert "/api/integrations/aiostreams" not in (root / "static/page-settings.js").read_text()


def test_migration_retires_proxy_preserves_history_and_rebases_queues():
    migration = import_module("librarysync.db.migrations.versions.8e9f0a1b2c3d_complete_watch_state")
    metadata = MetaData()
    for table in Base.metadata.sorted_tables:
        if not table.name.startswith("watch_state_"):
            table.to_metadata(metadata)
    engine = create_engine("sqlite:///:memory:")
    with engine.begin() as connection:
        metadata.create_all(connection)
        connection.execute(insert(User).values(id="u", username="u", password_hash="unused"))
        connection.execute(insert(MediaItem).values(id="m", media_type="movie", title="Kept"))
        from datetime import datetime, timezone

        connection.execute(
            insert(WatchedItem).values(
                id="w",
                user_id="u",
                media_item_id="m",
                source="aiostreams",
                watched_at=datetime.now(timezone.utc),
            )
        )
        connection.execute(
            insert(Integration).values(id="proxy", user_id="u", provider="aiostreams", status="connected")
        )
        connection.execute(insert(IntegrationSecret).values(integration_id="proxy", secret_data="encrypted"))
        connection.execute(
            insert(Integration).values(
                id="system",
                user_id="u",
                provider="system",
                config={
                    "quick_import_queue": ["trakt", "aiostreams", "simkl"],
                    "quick_import_index": 2,
                    "import_all_queue": ["aiostreams", "trakt"],
                    "import_all_index": 1,
                    "import_queue_order": ["aiostreams", "trakt"],
                },
            )
        )
        with Operations.context(MigrationContext.configure(connection)):
            migration.upgrade()
            assert connection.scalar(select(WatchedItem.id)) == "w"
            assert connection.scalar(select(IntegrationSecret.id)) is None
            assert connection.scalar(select(Integration.status).where(Integration.id == "proxy")) == "retired"
            config = connection.scalar(select(Integration.config).where(Integration.id == "system"))
            assert config["quick_import_queue"] == ["trakt", "simkl"] and config["quick_import_index"] == 1
            assert config["import_all_queue"] == ["trakt"] and config["import_all_index"] == 0
            migration.downgrade()
            assert connection.scalar(select(WatchedItem.id)) == "w"
    engine.dispose()
