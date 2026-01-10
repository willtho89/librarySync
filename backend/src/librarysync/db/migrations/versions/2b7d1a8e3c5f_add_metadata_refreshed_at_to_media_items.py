"""add metadata_refreshed_at to media_items

Revision ID: 2b7d1a8e3c5f
Revises: 4a1b2c3d4e5f
Create Date: 2026-01-12 12:00:00.000000

"""
from alembic import op
import sqlalchemy as sa


revision = "2b7d1a8e3c5f"
down_revision = "4a1b2c3d4e5f"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "media_items",
        sa.Column("metadata_refreshed_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("media_items", "metadata_refreshed_at")
