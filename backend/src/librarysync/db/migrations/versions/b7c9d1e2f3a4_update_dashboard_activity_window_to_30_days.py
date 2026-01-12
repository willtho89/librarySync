"""update dashboard activity window to 30 days

Revision ID: b7c9d1e2f3a4
Revises: 2b7d1a8e3c5f
Create Date: 2026-01-07 00:00:00.000000

"""
from alembic import op


revision = "b7c9d1e2f3a4"
down_revision = "2b7d1a8e3c5f"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE OR REPLACE VIEW user_daily_activity AS
        SELECT
            w.user_id,
            DATE(w.watched_at AT TIME ZONE 'UTC') as watch_date,
            COUNT(DISTINCT CASE WHEN m.media_type = 'movie' THEN w.id END) as movies_count,
            COUNT(DISTINCT CASE WHEN w.episode_item_id IS NOT NULL THEN w.id END) as episodes_count
        FROM watched_items w
        LEFT JOIN media_items m ON w.media_item_id = m.id
        WHERE w.watched_at >= NOW() - INTERVAL '30 days'
        GROUP BY w.user_id, DATE(w.watched_at AT TIME ZONE 'UTC')
        ORDER BY watch_date DESC
        """
    )


def downgrade() -> None:
    op.execute(
        """
        CREATE OR REPLACE VIEW user_daily_activity AS
        SELECT
            w.user_id,
            DATE(w.watched_at AT TIME ZONE 'UTC') as watch_date,
            COUNT(DISTINCT CASE WHEN m.media_type = 'movie' THEN w.id END) as movies_count,
            COUNT(DISTINCT CASE WHEN w.episode_item_id IS NOT NULL THEN w.id END) as episodes_count
        FROM watched_items w
        LEFT JOIN media_items m ON w.media_item_id = m.id
        WHERE w.watched_at >= NOW() - INTERVAL '90 days'
        GROUP BY w.user_id, DATE(w.watched_at AT TIME ZONE 'UTC')
        ORDER BY watch_date DESC
        """
    )
