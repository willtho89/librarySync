"""drop stremio addon key columns

Revision ID: d2f6a1b7c3e9
Revises: c9a6b5d7e8f1
Create Date: 2026-02-01 00:00:00.000000
"""

from alembic import op
import sqlalchemy as sa


revision = "d2f6a1b7c3e9"
down_revision = "c9a6b5d7e8f1"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_index(
        "ix_stremio_addon_configs_addon_key_hash",
        table_name="stremio_addon_configs",
    )
    op.drop_constraint(
        "uq_stremio_addon_configs_addon_key_hash",
        "stremio_addon_configs",
        type_="unique",
    )
    op.drop_column("stremio_addon_configs", "addon_key_last_rotated_at")
    op.drop_column("stremio_addon_configs", "addon_key_hash")


def downgrade() -> None:
    op.add_column(
        "stremio_addon_configs",
        sa.Column("addon_key_hash", sa.String(length=64), nullable=False, server_default=""),
    )
    op.add_column(
        "stremio_addon_configs",
        sa.Column(
            "addon_key_last_rotated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
    )
    op.alter_column("stremio_addon_configs", "addon_key_hash", server_default=None)
    op.create_unique_constraint(
        "uq_stremio_addon_configs_addon_key_hash",
        "stremio_addon_configs",
        ["addon_key_hash"],
    )
    op.create_index(
        "ix_stremio_addon_configs_addon_key_hash",
        "stremio_addon_configs",
        ["addon_key_hash"],
    )
