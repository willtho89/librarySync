"""add external catalog source fields

Revision ID: e4f6a8b1c2d3
Revises: d2b7c8f9a1e0
Create Date: 2026-04-17 16:30:00.000000

"""

from alembic import op
import sqlalchemy as sa


revision = "e4f6a8b1c2d3"
down_revision = "d2b7c8f9a1e0"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "stremio_external_catalogs",
        sa.Column("source_kind", sa.String(length=32), nullable=False, server_default="manifest"),
    )
    op.add_column(
        "stremio_external_catalogs",
        sa.Column("source_provider", sa.String(length=32), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("stremio_external_catalogs", "source_provider")
    op.drop_column("stremio_external_catalogs", "source_kind")
