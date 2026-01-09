"""add watchlist source enabled

Revision ID: 4a1b2c3d4e5f
Revises: 3f9a1c2d4e5f
Create Date: 2025-01-01 00:00:00.000000
"""

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = "4a1b2c3d4e5f"
down_revision = "3f9a1c2d4e5f"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "watchlist_sources",
        sa.Column("is_enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
    )
    op.alter_column("watchlist_sources", "is_enabled", server_default=None)


def downgrade() -> None:
    op.drop_column("watchlist_sources", "is_enabled")

