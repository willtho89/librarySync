"""add anilist_id to media_items

Revision ID: 6e9a1b2c3d4f
Revises: 8b1d6b4c5f10
Create Date: 2026-01-03 23:00:00.000000

"""
from alembic import op
import sqlalchemy as sa


revision = "6e9a1b2c3d4f"
down_revision = "8b1d6b4c5f10"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Add anilist_id column to media_items
    op.add_column(
        "media_items",
        sa.Column("anilist_id", sa.String(length=32), nullable=True)
    )
    
    # Add index for anilist_id
    op.create_index(
        op.f("ix_media_items_anilist_id"),
        "media_items",
        ["anilist_id"],
        unique=False
    )
    
    # Add unique constraint for media_type + anilist_id combination
    op.create_unique_constraint(
        "uq_media_items_anilist_id_type",
        "media_items",
        ["media_type", "anilist_id"]
    )


def downgrade() -> None:
    # Drop unique constraint
    op.drop_constraint(
        "uq_media_items_anilist_id_type",
        "media_items",
        type_="unique"
    )
    
    # Drop index
    op.drop_index(
        op.f("ix_media_items_anilist_id"),
        table_name="media_items"
    )
    
    # Drop column
    op.drop_column("media_items", "anilist_id")
