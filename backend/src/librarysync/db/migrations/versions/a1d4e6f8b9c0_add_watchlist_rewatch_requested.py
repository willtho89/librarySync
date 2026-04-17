"""add watchlist rewatch requested flag

Revision ID: a1d4e6f8b9c0
Revises: c9a6b5d7e8f1
Create Date: 2026-04-17 12:00:00.000000

"""

from alembic import op
import sqlalchemy as sa


revision = "a1d4e6f8b9c0"
down_revision = "c9a6b5d7e8f1"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "watchlist_items",
        sa.Column("rewatch_requested", sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    op.add_column(
        "watchlist_items",
        sa.Column("rewatch_requested_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.alter_column("watchlist_items", "rewatch_requested", server_default=None)


def downgrade() -> None:
    op.drop_column("watchlist_items", "rewatch_requested_at")
    op.drop_column("watchlist_items", "rewatch_requested")
