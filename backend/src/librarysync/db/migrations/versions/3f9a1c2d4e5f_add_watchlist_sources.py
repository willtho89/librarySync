"""add watchlist sources

Revision ID: 3f9a1c2d4e5f
Revises: 7e38c9ecf58a
Create Date: 2025-01-12 00:00:00.000000
"""

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = "3f9a1c2d4e5f"
down_revision = "7e38c9ecf58a"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "watchlist_sources",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("user_id", sa.String(length=36), nullable=False),
        sa.Column("provider", sa.String(length=32), nullable=False),
        sa.Column("source_type", sa.String(length=32), nullable=False),
        sa.Column("external_id", sa.String(length=255), nullable=False),
        sa.Column("url", sa.String(length=500), nullable=True),
        sa.Column("name", sa.String(length=255), nullable=True),
        sa.Column("last_synced_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.UniqueConstraint(
            "user_id",
            "provider",
            "source_type",
            "external_id",
            name="uq_watchlist_sources_user_provider_type_external",
        ),
    )
    op.create_index(
        "ix_watchlist_sources_user_provider",
        "watchlist_sources",
        ["user_id", "provider"],
        unique=False,
    )
    op.create_index(
        op.f("ix_watchlist_sources_provider"),
        "watchlist_sources",
        ["provider"],
        unique=False,
    )
    op.create_index(
        op.f("ix_watchlist_sources_user_id"),
        "watchlist_sources",
        ["user_id"],
        unique=False,
    )

    op.create_table(
        "watchlist_source_items",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("source_id", sa.String(length=36), nullable=False),
        sa.Column("watchlist_item_id", sa.String(length=36), nullable=False),
        sa.Column("user_id", sa.String(length=36), nullable=False),
        sa.Column("media_item_id", sa.String(length=36), nullable=False),
        sa.Column("external_item_id", sa.String(length=255), nullable=True),
        sa.Column("added_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(
            ["source_id"], ["watchlist_sources.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["watchlist_item_id"], ["watchlist_items.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["media_item_id"], ["media_items.id"], ondelete="CASCADE"),
        sa.UniqueConstraint(
            "source_id",
            "watchlist_item_id",
            name="uq_watchlist_source_items_source_item",
        ),
    )
    op.create_index(
        "ix_watchlist_source_items_user_media",
        "watchlist_source_items",
        ["user_id", "media_item_id"],
        unique=False,
    )
    op.create_index(
        "ix_watchlist_source_items_source_seen",
        "watchlist_source_items",
        ["source_id", "last_seen_at"],
        unique=False,
    )
    op.create_index(
        op.f("ix_watchlist_source_items_source_id"),
        "watchlist_source_items",
        ["source_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_watchlist_source_items_watchlist_item_id"),
        "watchlist_source_items",
        ["watchlist_item_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_watchlist_source_items_user_id"),
        "watchlist_source_items",
        ["user_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_watchlist_source_items_media_item_id"),
        "watchlist_source_items",
        ["media_item_id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(op.f("ix_watchlist_source_items_media_item_id"), table_name="watchlist_source_items")
    op.drop_index(op.f("ix_watchlist_source_items_user_id"), table_name="watchlist_source_items")
    op.drop_index(op.f("ix_watchlist_source_items_watchlist_item_id"), table_name="watchlist_source_items")
    op.drop_index(op.f("ix_watchlist_source_items_source_id"), table_name="watchlist_source_items")
    op.drop_index("ix_watchlist_source_items_source_seen", table_name="watchlist_source_items")
    op.drop_index("ix_watchlist_source_items_user_media", table_name="watchlist_source_items")
    op.drop_table("watchlist_source_items")

    op.drop_index(op.f("ix_watchlist_sources_user_id"), table_name="watchlist_sources")
    op.drop_index(op.f("ix_watchlist_sources_provider"), table_name="watchlist_sources")
    op.drop_index("ix_watchlist_sources_user_provider", table_name="watchlist_sources")
    op.drop_table("watchlist_sources")
