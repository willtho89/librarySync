"""add stremio addon tables

Revision ID: c9a6b5d7e8f1
Revises: 34ca9376da38
Create Date: 2026-02-01 00:00:00.000000
"""

from alembic import op
import sqlalchemy as sa


revision = "c9a6b5d7e8f1"
down_revision = "34ca9376da38"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "stremio_addon_configs",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("user_id", sa.String(length=36), nullable=False),
        sa.Column("is_enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("default_catalogs", sa.JSON(), nullable=True),
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
        sa.UniqueConstraint("user_id", name="uq_stremio_addon_configs_user_id"),
    )
    op.create_index(
        op.f("ix_stremio_addon_configs_user_id"),
        "stremio_addon_configs",
        ["user_id"],
    )

    op.create_table(
        "stremio_custom_catalogs",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("user_id", sa.String(length=36), nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("slug", sa.String(length=64), nullable=False),
        sa.Column("media_type", sa.String(length=32), nullable=False, server_default="movie"),
        sa.Column("order_by", sa.String(length=32), nullable=False, server_default="manual"),
        sa.Column("order_dir", sa.String(length=8), nullable=False, server_default="asc"),
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
        sa.UniqueConstraint("user_id", "slug", name="uq_stremio_custom_catalogs_user_slug"),
    )
    op.create_index(
        op.f("ix_stremio_custom_catalogs_user_id"),
        "stremio_custom_catalogs",
        ["user_id"],
    )

    op.create_table(
        "stremio_custom_catalog_items",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("catalog_id", sa.String(length=36), nullable=False),
        sa.Column("media_item_id", sa.String(length=36), nullable=False),
        sa.Column("position", sa.Integer(), nullable=False, server_default="0"),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.ForeignKeyConstraint(
            ["catalog_id"], ["stremio_custom_catalogs.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["media_item_id"], ["media_items.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "catalog_id",
            "media_item_id",
            name="uq_stremio_custom_catalog_items_catalog_media",
        ),
    )
    op.create_index(
        "ix_stremio_custom_catalog_items_catalog_position",
        "stremio_custom_catalog_items",
        ["catalog_id", "position"],
    )
    op.create_index(
        op.f("ix_stremio_custom_catalog_items_catalog_id"),
        "stremio_custom_catalog_items",
        ["catalog_id"],
    )
    op.create_index(
        op.f("ix_stremio_custom_catalog_items_media_item_id"),
        "stremio_custom_catalog_items",
        ["media_item_id"],
    )


def downgrade() -> None:
    op.drop_index(op.f("ix_stremio_custom_catalog_items_media_item_id"), table_name="stremio_custom_catalog_items")
    op.drop_index(op.f("ix_stremio_custom_catalog_items_catalog_id"), table_name="stremio_custom_catalog_items")
    op.drop_index(
        "ix_stremio_custom_catalog_items_catalog_position",
        table_name="stremio_custom_catalog_items",
    )
    op.drop_table("stremio_custom_catalog_items")
    op.drop_index(
        op.f("ix_stremio_custom_catalogs_user_id"), table_name="stremio_custom_catalogs"
    )
    op.drop_table("stremio_custom_catalogs")
    op.drop_index(
        op.f("ix_stremio_addon_configs_user_id"), table_name="stremio_addon_configs"
    )
    op.drop_table("stremio_addon_configs")
