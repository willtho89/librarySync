"""Complete Watch State v2 and retire proxy inference."""

import sqlalchemy as sa
from alembic import op

from librarysync.db.watch_state_triggers import install_watch_state_triggers, remove_watch_state_triggers

revision = "8e9f0a1b2c3d"
down_revision = "7d8e9f0a1b2c"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "watch_state_receipts",
        sa.Column("id", sa.String(length=36), primary_key=True, nullable=False),
        sa.Column(
            "addon_id",
            sa.String(length=36),
            sa.ForeignKey("stremio_addon_configs.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("viewer", sa.String(length=32), nullable=False),
        sa.Column("user_id", sa.String(length=36), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("entry_key", sa.String(length=64), nullable=False),
        sa.Column("event", sa.String(length=32), nullable=False),
        sa.Column("media_type", sa.String(length=16), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("outcomes", sa.JSON(), nullable=True),
        sa.Column("duplicates", sa.Integer(), nullable=False),
        sa.Column("received_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("processed_at", sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint("addon_id", "viewer", "entry_key", name="uq_watch_state_receipt_event"),
    )
    op.create_index("ix_watch_state_receipts_user_id", "watch_state_receipts", ["user_id"], unique=False)
    op.create_index(
        "ix_watch_state_receipt_user_received", "watch_state_receipts", ["user_id", "received_at"], unique=False
    )
    op.create_index("ix_watch_state_receipts_status", "watch_state_receipts", ["status"], unique=False)
    op.create_table(
        "watch_state_entries",
        sa.Column("id", sa.String(length=36), primary_key=True, nullable=False),
        sa.Column("user_id", sa.String(length=36), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("category", sa.String(length=16), nullable=False),
        sa.Column("target_key", sa.String(length=64), nullable=False),
        sa.Column("media_type", sa.String(length=16), nullable=False),
        sa.Column("scope", sa.String(length=16), nullable=False),
        sa.Column(
            "media_item_id", sa.String(length=36), sa.ForeignKey("media_items.id", ondelete="SET NULL"), nullable=True
        ),
        sa.Column(
            "episode_item_id",
            sa.String(length=36),
            sa.ForeignKey("episode_items.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("meta_id", sa.String(length=255), nullable=False),
        sa.Column("video_id", sa.String(length=255), nullable=True),
        sa.Column("season", sa.Integer(), nullable=True),
        sa.Column("episode", sa.Integer(), nullable=True),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.UniqueConstraint("user_id", "category", "target_key", name="uq_watch_state_entry_target"),
    )
    op.create_index(
        "ix_watch_state_entry_user_category_time",
        "watch_state_entries",
        ["user_id", "category", "occurred_at"],
        unique=False,
    )
    op.create_index("ix_watch_state_entries_episode_item_id", "watch_state_entries", ["episode_item_id"], unique=False)
    op.create_index("ix_watch_state_entries_media_item_id", "watch_state_entries", ["media_item_id"], unique=False)
    op.create_table(
        "watch_state_viewers",
        sa.Column("id", sa.String(length=36), primary_key=True, nullable=False),
        sa.Column(
            "addon_id",
            sa.String(length=36),
            sa.ForeignKey("stremio_addon_configs.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("viewer", sa.String(length=32), nullable=False),
        sa.Column("user_id", sa.String(length=36), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=True),
        sa.Column("invitation_hash", sa.String(length=64), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint("addon_id", "viewer", name="uq_watch_state_viewer"),
        sa.UniqueConstraint("invitation_hash", name=None),
    )
    op.create_table(
        "watch_state_revisions",
        sa.Column(
            "user_id",
            sa.String(length=36),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            primary_key=True,
            nullable=False,
        ),
        sa.Column("revision", sa.BigInteger(), nullable=False, server_default="0"),
    )
    op.create_table(
        "watch_state_catalog_revision",
        sa.Column("id", sa.Integer(), primary_key=True, nullable=False),
        sa.Column("revision", sa.BigInteger(), nullable=False, server_default="0"),
    )
    op.create_table(
        "watch_state_snapshots",
        sa.Column(
            "user_id",
            sa.String(length=36),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            primary_key=True,
            nullable=False,
        ),
        sa.Column("version", sa.String(length=128), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("built_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_pulled_at", sa.DateTime(timezone=True), nullable=True),
    )
    install_watch_state_triggers(op.get_bind())
    _retire_proxy()


def _retire_proxy() -> None:
    connection = op.get_bind()
    integrations = sa.table(
        "integrations", sa.column("id"), sa.column("provider"), sa.column("status"), sa.column("config", sa.JSON)
    )
    secrets = sa.table("integration_secrets", sa.column("integration_id"))
    ids = sa.select(integrations.c.id).where(integrations.c.provider == "aiostreams")
    connection.execute(sa.delete(secrets).where(secrets.c.integration_id.in_(ids)))
    connection.execute(
        sa.update(integrations).where(integrations.c.provider == "aiostreams").values(status="retired", config={})
    )
    for row in connection.execute(sa.select(integrations.c.id, integrations.c.config)).all():
        config = dict(row.config or {})
        for prefix in ("quick_import", "import_all"):
            queue = config.get(f"{prefix}_queue")
            if not isinstance(queue, list):
                continue
            index = int(config.get(f"{prefix}_index", 0) or 0)
            config[f"{prefix}_queue"] = [p for p in queue if p != "aiostreams"]
            config[f"{prefix}_index"] = sum(p != "aiostreams" for p in queue[:index])
        order = config.get("import_queue_order")
        if isinstance(order, list):
            config["import_queue_order"] = [p for p in order if p != "aiostreams"]
        if config != row.config:
            connection.execute(sa.update(integrations).where(integrations.c.id == row.id).values(config=config))


def downgrade() -> None:
    # History is untouched. Retired proxy credentials cannot be restored.
    remove_watch_state_triggers(op.get_bind())
    op.drop_table("watch_state_snapshots")
    op.drop_table("watch_state_catalog_revision")
    op.drop_table("watch_state_revisions")
    op.drop_table("watch_state_viewers")
    op.drop_table("watch_state_entries")
    op.drop_table("watch_state_receipts")
