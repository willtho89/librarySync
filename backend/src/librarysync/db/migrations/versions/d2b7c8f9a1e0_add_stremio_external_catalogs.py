"""add stremio external catalogs

Revision ID: d2b7c8f9a1e0
Revises: a1d4e6f8b9c0
Create Date: 2026-04-17 14:00:00.000000

"""

from alembic import op
import sqlalchemy as sa


revision = "d2b7c8f9a1e0"
down_revision = "a1d4e6f8b9c0"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "stremio_external_catalogs",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("user_id", sa.String(length=36), nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("slug", sa.String(length=64), nullable=False),
        sa.Column("addon_name", sa.String(length=255), nullable=True),
        sa.Column("manifest_url", sa.String(length=500), nullable=False),
        sa.Column("source_catalog_id", sa.String(length=255), nullable=False),
        sa.Column("source_catalog_type", sa.String(length=32), nullable=False),
        sa.Column("media_type", sa.String(length=32), nullable=False, server_default="movie"),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("filters", sa.JSON(), nullable=True),
        sa.Column("order_by", sa.String(length=32), nullable=False, server_default="source"),
        sa.Column("order_dir", sa.String(length=8), nullable=False, server_default="asc"),
        sa.Column("page_size", sa.Integer(), nullable=False, server_default="30"),
        sa.Column("show_in_home", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("last_refreshed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_refresh_error", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("user_id", "slug", name="uq_stremio_external_catalogs_user_slug"),
    )
    op.create_index(
        op.f("ix_stremio_external_catalogs_user_id"),
        "stremio_external_catalogs",
        ["user_id"],
    )

    op.create_table(
        "stremio_external_catalog_items",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("catalog_id", sa.String(length=36), nullable=False),
        sa.Column("media_item_id", sa.String(length=36), nullable=True),
        sa.Column("stremio_id", sa.String(length=255), nullable=False),
        sa.Column("stremio_type", sa.String(length=32), nullable=False),
        sa.Column("title", sa.String(length=255), nullable=True),
        sa.Column("year", sa.Integer(), nullable=True),
        sa.Column("poster_url", sa.String(length=500), nullable=True),
        sa.Column("imdb_id", sa.String(length=32), nullable=True),
        sa.Column("position", sa.Integer(), nullable=False, server_default="0"),
        sa.Column(
            "fetched_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.ForeignKeyConstraint(
            ["catalog_id"], ["stremio_external_catalogs.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(["media_item_id"], ["media_items.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "catalog_id",
            "position",
            name="uq_stremio_external_catalog_items_catalog_position",
        ),
        sa.UniqueConstraint(
            "catalog_id",
            "stremio_id",
            name="uq_stremio_external_catalog_items_catalog_stremio",
        ),
    )
    op.create_index(
        "ix_stremio_external_catalog_items_catalog_position",
        "stremio_external_catalog_items",
        ["catalog_id", "position"],
    )
    op.create_index(
        op.f("ix_stremio_external_catalog_items_catalog_id"),
        "stremio_external_catalog_items",
        ["catalog_id"],
    )
    op.create_index(
        op.f("ix_stremio_external_catalog_items_media_item_id"),
        "stremio_external_catalog_items",
        ["media_item_id"],
    )
    op.create_index(
        op.f("ix_stremio_external_catalog_items_imdb_id"),
        "stremio_external_catalog_items",
        ["imdb_id"],
    )


def downgrade() -> None:
    op.drop_index(
        op.f("ix_stremio_external_catalog_items_imdb_id"),
        table_name="stremio_external_catalog_items",
    )
    op.drop_index(
        op.f("ix_stremio_external_catalog_items_media_item_id"),
        table_name="stremio_external_catalog_items",
    )
    op.drop_index(
        op.f("ix_stremio_external_catalog_items_catalog_id"),
        table_name="stremio_external_catalog_items",
    )
    op.drop_index(
        "ix_stremio_external_catalog_items_catalog_position",
        table_name="stremio_external_catalog_items",
    )
    op.drop_table("stremio_external_catalog_items")
    op.drop_index(
        op.f("ix_stremio_external_catalogs_user_id"),
        table_name="stremio_external_catalogs",
    )
    op.drop_table("stremio_external_catalogs")
