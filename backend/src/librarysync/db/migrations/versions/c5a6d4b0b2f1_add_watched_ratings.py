"""add watched ratings

Revision ID: c5a6d4b0b2f1
Revises: 7b1d2f1a9c2f
Create Date: 2026-01-06 09:00:00.000000

"""

import sqlalchemy as sa
from alembic import op

revision = "c5a6d4b0b2f1"
down_revision = "7b1d2f1a9c2f"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("watched_items", sa.Column("rating", sa.Float(), nullable=True))
    op.create_check_constraint(
        "ck_watched_items_rating_range",
        "watched_items",
        "rating IS NULL OR (rating >= 0.5 AND rating <= 5.0)",
    )


def downgrade() -> None:
    op.drop_constraint(
        "ck_watched_items_rating_range", "watched_items", type_="check"
    )
    op.drop_column("watched_items", "rating")
