"""drop poll interval

Revision ID: 8f3b1f0a3e1b
Revises: c5a6d4b0b2f1
Create Date: 2026-01-06 10:00:00.000000

"""

import sqlalchemy as sa
from alembic import op

revision = "8f3b1f0a3e1b"
down_revision = "c5a6d4b0b2f1"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_column("users", "poll_interval_seconds")


def downgrade() -> None:
    op.add_column(
        "users",
        sa.Column("poll_interval_seconds", sa.Integer(), nullable=True),
    )
