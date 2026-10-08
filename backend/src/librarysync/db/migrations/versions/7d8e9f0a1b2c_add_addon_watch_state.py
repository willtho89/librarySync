"""Add opt-in AIOStreams watch state support."""

import sqlalchemy as sa
from alembic import op

revision = "7d8e9f0a1b2c"
down_revision = "b3d4e5f6a7b8"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "stremio_addon_configs",
        sa.Column("watch_state_enabled", sa.Boolean(), nullable=False, server_default=sa.false()),
    )


def downgrade() -> None:
    op.drop_column("stremio_addon_configs", "watch_state_enabled")
