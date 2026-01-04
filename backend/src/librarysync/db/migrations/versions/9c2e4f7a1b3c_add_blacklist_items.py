"""add blacklist items

Revision ID: 9c2e4f7a1b3c
Revises: 8b1d6b4c5f10
Create Date: 2026-01-12 00:00:00.000000

"""
from alembic import op
import sqlalchemy as sa


revision = "9c2e4f7a1b3c"
down_revision = "8b1d6b4c5f10"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "blacklist_items",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("user_id", sa.String(length=36), nullable=False),
        sa.Column("media_type", sa.String(length=32), nullable=False),
        sa.Column("provider", sa.String(length=32), nullable=False),
        sa.Column("provider_item_id", sa.String(length=64), nullable=False),
        sa.Column("title", sa.String(length=255), nullable=False),
        sa.Column("year", sa.Integer(), nullable=True),
        sa.Column("poster_url", sa.String(length=500), nullable=True),
        sa.Column("imdb_id", sa.String(length=32), nullable=True),
        sa.Column("tmdb_id", sa.String(length=32), nullable=True),
        sa.Column("tvdb_id", sa.String(length=32), nullable=True),
        sa.Column("tvmaze_id", sa.String(length=32), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "user_id",
            "provider",
            "provider_item_id",
            name="uq_blacklist_items_user_provider_item",
        ),
    )
    op.create_index(
        "ix_blacklist_items_user_media_type",
        "blacklist_items",
        ["user_id", "media_type"],
        unique=False,
    )
    op.create_index(
        "ix_blacklist_items_user_imdb_id",
        "blacklist_items",
        ["user_id", "imdb_id"],
        unique=False,
    )
    op.create_index(
        "ix_blacklist_items_user_tmdb_id",
        "blacklist_items",
        ["user_id", "tmdb_id"],
        unique=False,
    )
    op.create_index(
        "ix_blacklist_items_user_tvdb_id",
        "blacklist_items",
        ["user_id", "tvdb_id"],
        unique=False,
    )
    op.create_index(
        "ix_blacklist_items_user_tvmaze_id",
        "blacklist_items",
        ["user_id", "tvmaze_id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_blacklist_items_user_tvmaze_id", table_name="blacklist_items")
    op.drop_index("ix_blacklist_items_user_tvdb_id", table_name="blacklist_items")
    op.drop_index("ix_blacklist_items_user_tmdb_id", table_name="blacklist_items")
    op.drop_index("ix_blacklist_items_user_imdb_id", table_name="blacklist_items")
    op.drop_index("ix_blacklist_items_user_media_type", table_name="blacklist_items")
    op.drop_table("blacklist_items")
