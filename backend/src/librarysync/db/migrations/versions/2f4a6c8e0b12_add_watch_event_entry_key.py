"""add watch event entry key

Revision ID: 2f4a6c8e0b12
Revises: 1c2d3e4f5a6b
Create Date: 2026-01-13 00:00:00.000000

"""

from alembic import op
import sqlalchemy as sa


revision = "2f4a6c8e0b12"
down_revision = "1c2d3e4f5a6b"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "watch_events",
        sa.Column("entry_key", sa.String(length=255), nullable=True),
    )
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        op.execute(
            sa.text(
                """
                UPDATE watch_events
                SET entry_key = raw->>'entry_key'
                WHERE entry_key IS NULL AND raw->>'entry_key' IS NOT NULL
                """
            )
        )
    elif bind.dialect.name == "sqlite":
        op.execute(
            sa.text(
                """
                UPDATE watch_events
                SET entry_key = json_extract(raw, '$.entry_key')
                WHERE entry_key IS NULL
                  AND json_extract(raw, '$.entry_key') IS NOT NULL
                """
            )
        )
    else:
        op.execute(
            sa.text(
                """
                UPDATE watch_events
                SET entry_key = JSON_UNQUOTE(JSON_EXTRACT(raw, '$.entry_key'))
                WHERE entry_key IS NULL
                  AND JSON_EXTRACT(raw, '$.entry_key') IS NOT NULL
                """
            )
        )
    op.execute(
        sa.text(
            """
            DELETE FROM watch_events
            WHERE id IN (
                SELECT id FROM (
                    SELECT id,
                           ROW_NUMBER() OVER (
                               PARTITION BY user_id, event_type, entry_key
                               ORDER BY created_at ASC, id ASC
                           ) AS row_number
                    FROM watch_events
                    WHERE entry_key IS NOT NULL
                ) dedupe
                WHERE row_number > 1
            )
            """
        )
    )
    op.create_unique_constraint(
        "uq_watch_events_user_event_entry_key",
        "watch_events",
        ["user_id", "event_type", "entry_key"],
    )


def downgrade() -> None:
    op.drop_constraint(
        "uq_watch_events_user_event_entry_key",
        "watch_events",
        type_="unique",
    )
    op.drop_column("watch_events", "entry_key")
