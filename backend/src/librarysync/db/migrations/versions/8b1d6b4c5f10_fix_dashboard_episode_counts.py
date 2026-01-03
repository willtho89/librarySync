"""fix dashboard episode counts

Revision ID: 8b1d6b4c5f10
Revises: 5a8c9d2e3f4b
Create Date: 2026-01-05 00:00:00.000000

"""
from alembic import op


revision = "8b1d6b4c5f10"
down_revision = "5a8c9d2e3f4b"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE OR REPLACE VIEW user_watch_stats AS
        SELECT
            w.user_id,
            COUNT(DISTINCT CASE WHEN m.media_type = 'movie' THEN w.id END) as movies_watched,
            COUNT(DISTINCT CASE WHEN w.episode_item_id IS NOT NULL THEN w.id END) as episodes_watched,
            COUNT(
                DISTINCT CASE
                    WHEN w.episode_item_id IS NOT NULL THEN e.show_media_item_id
                    WHEN m.media_type = 'tv' THEN m.id
                END
            ) as shows_watched,
            COUNT(DISTINCT CASE WHEN w.rating IS NOT NULL THEN w.id END) as items_rated,
            AVG(CASE WHEN w.rating IS NOT NULL THEN w.rating END) as avg_rating,
            MIN(w.watched_at) as first_watch_date,
            MAX(w.watched_at) as last_watch_date,
            COUNT(DISTINCT DATE(w.watched_at AT TIME ZONE 'UTC')) as total_watch_days
        FROM watched_items w
        LEFT JOIN media_items m ON w.media_item_id = m.id
        LEFT JOIN episode_items e ON w.episode_item_id = e.id
        GROUP BY w.user_id
        """
    )

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


def downgrade() -> None:
    op.execute(
        """
        CREATE OR REPLACE VIEW user_watch_stats AS
        SELECT
            w.user_id,
            COUNT(DISTINCT CASE WHEN m.media_type = 'movie' THEN w.id END) as movies_watched,
            COUNT(DISTINCT CASE WHEN m.media_type = 'tv' THEN w.id END) as episodes_watched,
            COUNT(DISTINCT CASE WHEN m.media_type = 'tv' THEN e.show_media_item_id END) as shows_watched,
            COUNT(DISTINCT CASE WHEN w.rating IS NOT NULL THEN w.id END) as items_rated,
            AVG(CASE WHEN w.rating IS NOT NULL THEN w.rating END) as avg_rating,
            MIN(w.watched_at) as first_watch_date,
            MAX(w.watched_at) as last_watch_date,
            COUNT(DISTINCT DATE(w.watched_at AT TIME ZONE 'UTC')) as total_watch_days
        FROM watched_items w
        LEFT JOIN media_items m ON w.media_item_id = m.id
        LEFT JOIN episode_items e ON w.episode_item_id = e.id
        GROUP BY w.user_id
        """
    )

    op.execute(
        """
        CREATE OR REPLACE VIEW user_daily_activity AS
        SELECT
            w.user_id,
            DATE(w.watched_at AT TIME ZONE 'UTC') as watch_date,
            COUNT(DISTINCT CASE WHEN m.media_type = 'movie' THEN w.id END) as movies_count,
            COUNT(DISTINCT CASE WHEN m.media_type = 'tv' THEN w.id END) as episodes_count
        FROM watched_items w
        LEFT JOIN media_items m ON w.media_item_id = m.id
        WHERE w.watched_at >= NOW() - INTERVAL '90 days'
        GROUP BY w.user_id, DATE(w.watched_at AT TIME ZONE 'UTC')
        ORDER BY watch_date DESC
        """
    )
