"""Make outbox dedupe keys unique only among waiting jobs.

An in-flight job may now have one waiting successor with the same key so
updates queued during delivery are not dropped. Finished jobs never hold keys.
"""

import sqlalchemy as sa
from alembic import op

revision = "58fe69244040"
down_revision = "fb3b3e7932d5"
branch_labels = None
depends_on = None

WAITING = "status IN ('pending', 'failed_retryable')"


def upgrade() -> None:
    op.execute(
        "UPDATE outbox SET dedupe_key = NULL "
        "WHERE dedupe_key IS NOT NULL AND status NOT IN ('pending', 'failed_retryable', 'in_progress')"
    )
    op.drop_constraint("uq_outbox_dedupe_key", "outbox", type_="unique")
    op.create_index(
        "uq_outbox_dedupe_key_waiting",
        "outbox",
        ["dedupe_key"],
        unique=True,
        postgresql_where=sa.text(WAITING),
        sqlite_where=sa.text(WAITING),
    )


def downgrade() -> None:
    op.drop_index("uq_outbox_dedupe_key_waiting", table_name="outbox")
    op.execute(
        "UPDATE outbox SET dedupe_key = NULL "
        "WHERE dedupe_key IS NOT NULL AND status NOT IN ('pending', 'failed_retryable', 'in_progress')"
    )
    # An in-flight job and its waiting successor share a key; keep it on the successor.
    op.execute(
        """
        UPDATE outbox SET dedupe_key = NULL
        WHERE status = 'in_progress'
            AND dedupe_key IN (
                SELECT dedupe_key FROM outbox
                WHERE status IN ('pending', 'failed_retryable') AND dedupe_key IS NOT NULL
            )
        """
    )
    op.create_unique_constraint("uq_outbox_dedupe_key", "outbox", ["dedupe_key"])
