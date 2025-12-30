"""drop completion threshold

Revision ID: 2c6f2c2f1b7a
Revises: 8f3b1f0a3e1b
Create Date: 2026-01-06 10:30:00.000000

"""

import sqlalchemy as sa
from alembic import op

revision = "2c6f2c2f1b7a"
down_revision = "8f3b1f0a3e1b"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_column("users", "completion_threshold_percent")


def downgrade() -> None:
    op.add_column(
        "users",
        sa.Column("completion_threshold_percent", sa.Float(), nullable=True),
    )
