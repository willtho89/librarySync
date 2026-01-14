"""add runtime_in_seconds genres overview to media_items

Revision ID: f5e85859e1da
Revises: b7c9d1e2f3a4
Create Date: 2026-01-14 12:54:42.060009

"""

from alembic import op
import sqlalchemy as sa


revision = "f5e85859e1da"
down_revision = "b7c9d1e2f3a4"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("media_items", sa.Column("runtime_in_seconds", sa.Integer(), nullable=True))
    op.add_column("media_items", sa.Column("genres", sa.JSON(), nullable=True))
    op.add_column("media_items", sa.Column("overview", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("media_items", "overview")
    op.drop_column("media_items", "genres")
    op.drop_column("media_items", "runtime_in_seconds")
