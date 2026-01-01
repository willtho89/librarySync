"""drop integration import schedule columns

Revision ID: 4d2b0f6a7c1e
Revises: f551ff23ab37
Create Date: 2025-01-10 20:12:00.000000

"""
from alembic import op
import sqlalchemy as sa


revision = "4d2b0f6a7c1e"
down_revision = "f551ff23ab37"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_index("ix_integrations_next_import_at", table_name="integrations")
    op.drop_index("ix_integrations_import_lease_until", table_name="integrations")
    op.drop_column("integrations", "next_import_at")
    op.drop_column("integrations", "import_lease_until")
    op.drop_column("integrations", "import_lease_owner")


def downgrade() -> None:
    op.add_column(
        "integrations",
        sa.Column("next_import_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "integrations",
        sa.Column("import_lease_until", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "integrations",
        sa.Column("import_lease_owner", sa.String(length=128), nullable=True),
    )
    op.create_index(
        "ix_integrations_import_lease_until",
        "integrations",
        ["import_lease_until"],
        unique=False,
    )
    op.create_index(
        "ix_integrations_next_import_at",
        "integrations",
        ["next_import_at"],
        unique=False,
    )
