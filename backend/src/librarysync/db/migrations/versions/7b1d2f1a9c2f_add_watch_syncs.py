"""add watch syncs

Revision ID: 7b1d2f1a9c2f
Revises: 4f3e2d1c0b9a
Create Date: 2026-01-03 12:30:00.000000

"""

import sqlalchemy as sa
from alembic import op

revision = "7b1d2f1a9c2f"
down_revision = "4f3e2d1c0b9a"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "watch_syncs",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("user_id", sa.String(length=36), nullable=False),
        sa.Column("watched_item_id", sa.String(length=36), nullable=False),
        sa.Column("provider", sa.String(length=32), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("is_rewatch", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("external_id", sa.String(length=255), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("last_synced_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["watched_item_id"], ["watched_items.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "watched_item_id",
            "provider",
            name="uq_watch_syncs_watched_provider",
        ),
    )
    op.create_index(op.f("ix_watch_syncs_user_id"), "watch_syncs", ["user_id"])
    op.create_index(
        op.f("ix_watch_syncs_watched_item_id"),
        "watch_syncs",
        ["watched_item_id"],
    )
    op.create_index(op.f("ix_watch_syncs_provider"), "watch_syncs", ["provider"])
    op.create_index(op.f("ix_watch_syncs_status"), "watch_syncs", ["status"])
    op.alter_column("watch_syncs", "is_rewatch", server_default=None)


def downgrade() -> None:
    op.drop_index(op.f("ix_watch_syncs_status"), table_name="watch_syncs")
    op.drop_index(op.f("ix_watch_syncs_provider"), table_name="watch_syncs")
    op.drop_index(op.f("ix_watch_syncs_watched_item_id"), table_name="watch_syncs")
    op.drop_index(op.f("ix_watch_syncs_user_id"), table_name="watch_syncs")
    op.drop_table("watch_syncs")
