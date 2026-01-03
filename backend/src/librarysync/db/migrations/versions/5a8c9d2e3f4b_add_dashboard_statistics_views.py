"""add dashboard statistics views

Revision ID: 5a8c9d2e3f4b
Revises: 4d2b0f6a7c1e
Create Date: 2026-01-03 00:00:00.000000

"""
from alembic import op


revision = "5a8c9d2e3f4b"
down_revision = "4d2b0f6a7c1e"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Create view for user watch statistics
    op.execute("""
        CREATE VIEW user_watch_stats AS
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
    """)

    # Create view for user activity over time (last 90 days)
    op.execute("""
        CREATE VIEW user_daily_activity AS
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
    """)

    # Create view for user ratings distribution
    op.execute("""
        CREATE VIEW user_rating_distribution AS
        SELECT
            w.user_id,
            FLOOR(w.rating * 2) / 2 as rating_bucket,
            COUNT(*) as count
        FROM watched_items w
        WHERE w.rating IS NOT NULL
        GROUP BY w.user_id, FLOOR(w.rating * 2) / 2
        ORDER BY rating_bucket
    """)

    # Create view for system-wide statistics
    op.execute("""
        CREATE VIEW system_stats AS
        SELECT
            (SELECT COUNT(*) FROM media_items) as total_media_items,
            (SELECT COUNT(*) FROM episode_items) as total_episode_items,
            (SELECT COUNT(*) FROM integrations) as total_integrations,
            (SELECT COUNT(*) FROM watch_syncs) as total_sync_events,
            (SELECT COUNT(*) FROM users) as total_users,
            (SELECT COUNT(*) FROM watched_items) as total_watched_items,
            (SELECT COUNT(DISTINCT user_id) FROM watched_items) as active_users
    """)

    # Create view for user integration summary
    op.execute("""
        CREATE VIEW user_integration_summary AS
        SELECT
            i.user_id,
            COUNT(*) as integration_count,
            COUNT(CASE WHEN i.status = 'configured' THEN 1 END) as configured_count,
            array_agg(DISTINCT i.provider ORDER BY i.provider) as providers
        FROM integrations i
        WHERE i.provider NOT IN ('_system_')
        GROUP BY i.user_id
    """)


def downgrade() -> None:
    op.execute("DROP VIEW IF EXISTS user_integration_summary")
    op.execute("DROP VIEW IF EXISTS system_stats")
    op.execute("DROP VIEW IF EXISTS user_rating_distribution")
    op.execute("DROP VIEW IF EXISTS user_daily_activity")
    op.execute("DROP VIEW IF EXISTS user_watch_stats")
