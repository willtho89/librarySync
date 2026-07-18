"""backfill watchlist from show history

Revision ID: b3d4e5f6a7b8
Revises: e4f6a8b1c2d3
Create Date: 2026-07-18 12:00:00.000000

"""

from datetime import datetime, timezone
from uuid import NAMESPACE_URL, uuid5

import sqlalchemy as sa
from alembic import op

revision = "b3d4e5f6a7b8"
down_revision = "e4f6a8b1c2d3"
branch_labels = None
depends_on = None


_SHOW_HISTORY_SELECT = sa.text(
    """
SELECT DISTINCT user_id, media_item_id, media_type
FROM (
    SELECT
        w.user_id,
        m.id AS media_item_id,
        m.media_type AS media_type
    FROM watched_items w
    JOIN media_items m ON m.id = w.media_item_id
    WHERE m.media_type IN ('tv', 'anime')

    UNION

    SELECT
        w.user_id,
        e.show_media_item_id AS media_item_id,
        m.media_type AS media_type
    FROM watched_items w
    JOIN episode_items e ON e.id = w.episode_item_id
    JOIN media_items m ON m.id = e.show_media_item_id
    WHERE m.media_type IN ('tv', 'anime')
)
"""
)

_INSERT_WATCHLIST_ITEM = sa.text(
    """
INSERT INTO watchlist_items (
    id,
    user_id,
    media_item_id,
    type,
    status,
    source,
    rewatch_requested,
    rewatch_requested_at,
    created_at,
    updated_at
)
VALUES (
    :id,
    :user_id,
    :media_item_id,
    :media_type,
    'added',
    'auto_from_history',
    false,
    NULL,
    :now,
    :now
)
ON CONFLICT (user_id, media_item_id) DO NOTHING
"""
)

_DELETE_WATCHLIST_ITEM = sa.text(
    """
DELETE FROM watchlist_items
WHERE source = 'auto_from_history'
  AND id = :id
"""
)


def _migration_id(user_id: str, media_item_id: str) -> str:
    return str(uuid5(NAMESPACE_URL, f"librarysync:auto_from_history:{user_id}:{media_item_id}"))


def _history_rows(bind) -> list[dict[str, str]]:
    rows = bind.execute(_SHOW_HISTORY_SELECT).mappings().all()
    return [
        {
            "id": _migration_id(row["user_id"], row["media_item_id"]),
            "user_id": row["user_id"],
            "media_item_id": row["media_item_id"],
            "media_type": row["media_type"],
        }
        for row in rows
    ]


def upgrade() -> None:
    bind = op.get_bind()
    rows = _history_rows(bind)
    if not rows:
        return
    now = datetime.now(timezone.utc)
    bind.execute(_INSERT_WATCHLIST_ITEM, [{**row, "now": now} for row in rows])


def downgrade() -> None:
    bind = op.get_bind()
    rows = _history_rows(bind)
    if not rows:
        return
    bind.execute(_DELETE_WATCHLIST_ITEM, [{"id": row["id"]} for row in rows])
