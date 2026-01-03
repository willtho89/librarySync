from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from librarysync.api.deps import get_current_user, get_db
from librarysync.config import settings
from librarysync.db.models import User

router = APIRouter(
    prefix="/api/dashboard",
    tags=["dashboard"],
    dependencies=[Depends(get_current_user)],
)


@router.get(
    "/stats",
    summary="Get dashboard statistics",
    description="Return comprehensive statistics for the current user's dashboard.",
)
async def get_dashboard_stats(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """
    Get dashboard statistics including:
    - Watch statistics (movies/shows watched, ratings)
    - Activity over time (last 90 days)
    - Rating distribution
    - Integration summary
    - System statistics
    """
    
    # Check if dashboard stats are enabled
    if not settings.enable_dashboard_stats:
        raise HTTPException(
            status_code=403,
            detail="Dashboard statistics are disabled by the administrator"
        )

    # Get user watch statistics
    watch_stats_query = text("""
        SELECT
            movies_watched,
            episodes_watched,
            shows_watched,
            items_rated,
            avg_rating,
            first_watch_date,
            last_watch_date,
            total_watch_days
        FROM user_watch_stats
        WHERE user_id = :user_id
    """)
    watch_stats_result = await db.execute(
        watch_stats_query, {"user_id": current_user.id}
    )
    watch_stats_row = watch_stats_result.fetchone()

    if watch_stats_row:
        watch_stats = {
            "movies_watched": watch_stats_row.movies_watched or 0,
            "episodes_watched": watch_stats_row.episodes_watched or 0,
            "shows_watched": watch_stats_row.shows_watched or 0,
            "items_rated": watch_stats_row.items_rated or 0,
            "avg_rating": float(watch_stats_row.avg_rating) if watch_stats_row.avg_rating else 0,
            "first_watch_date": watch_stats_row.first_watch_date,
            "last_watch_date": watch_stats_row.last_watch_date,
            "total_watch_days": watch_stats_row.total_watch_days or 0,
        }
    else:
        watch_stats = {
            "movies_watched": 0,
            "episodes_watched": 0,
            "shows_watched": 0,
            "items_rated": 0,
            "avg_rating": 0,
            "first_watch_date": None,
            "last_watch_date": None,
            "total_watch_days": 0,
        }

    # Get daily activity (last 90 days)
    daily_activity_query = text("""
        SELECT
            watch_date,
            movies_count,
            episodes_count
        FROM user_daily_activity
        WHERE user_id = :user_id
        ORDER BY watch_date ASC
    """)
    daily_activity_result = await db.execute(
        daily_activity_query, {"user_id": current_user.id}
    )
    daily_activity = [
        {
            "date": row.watch_date.isoformat(),
            "movies": row.movies_count or 0,
            "episodes": row.episodes_count or 0,
        }
        for row in daily_activity_result.fetchall()
    ]

    # Get rating distribution
    rating_dist_query = text("""
        SELECT
            rating_bucket,
            count
        FROM user_rating_distribution
        WHERE user_id = :user_id
        ORDER BY rating_bucket
    """)
    rating_dist_result = await db.execute(
        rating_dist_query, {"user_id": current_user.id}
    )
    rating_distribution = [
        {"rating": float(row.rating_bucket), "count": row.count}
        for row in rating_dist_result.fetchall()
    ]

    # Get integration summary
    integration_query = text("""
        SELECT
            integration_count,
            configured_count,
            providers
        FROM user_integration_summary
        WHERE user_id = :user_id
    """)
    integration_result = await db.execute(
        integration_query, {"user_id": current_user.id}
    )
    integration_row = integration_result.fetchone()

    if integration_row:
        integration_summary = {
            "total_integrations": integration_row.integration_count or 0,
            "configured_integrations": integration_row.configured_count or 0,
            "providers": integration_row.providers or [],
        }
    else:
        integration_summary = {
            "total_integrations": 0,
            "configured_integrations": 0,
            "providers": [],
        }

    # Get system statistics
    system_stats_query = text("""
        SELECT
            total_media_items,
            total_episode_items,
            total_integrations,
            total_sync_events,
            total_users,
            total_watched_items,
            active_users
        FROM system_stats
    """)
    system_stats_result = await db.execute(system_stats_query)
    system_stats_row = system_stats_result.fetchone()

    if system_stats_row:
        system_stats = {
            "total_media_items": system_stats_row.total_media_items or 0,
            "total_episode_items": system_stats_row.total_episode_items or 0,
            "total_integrations": system_stats_row.total_integrations or 0,
            "total_sync_events": system_stats_row.total_sync_events or 0,
            "total_users": system_stats_row.total_users or 0,
            "total_watched_items": system_stats_row.total_watched_items or 0,
            "active_users": system_stats_row.active_users or 0,
        }
    else:
        system_stats = {
            "total_media_items": 0,
            "total_episode_items": 0,
            "total_integrations": 0,
            "total_sync_events": 0,
            "total_users": 0,
            "total_watched_items": 0,
            "active_users": 0,
        }

    # Calculate activity statistics
    now = datetime.now(timezone.utc)
    seven_days_ago = now - timedelta(days=7)
    thirty_days_ago = now - timedelta(days=30)

    # Activity in last 7 days
    recent_activity_query = text("""
        SELECT
            COUNT(*) as total_watches
        FROM watched_items
        WHERE user_id = :user_id
        AND watched_at >= :since
    """)
    
    last_7_days_result = await db.execute(
        recent_activity_query, 
        {"user_id": current_user.id, "since": seven_days_ago}
    )
    last_7_days_count = last_7_days_result.scalar() or 0

    last_30_days_result = await db.execute(
        recent_activity_query,
        {"user_id": current_user.id, "since": thirty_days_ago}
    )
    last_30_days_count = last_30_days_result.scalar() or 0

    # Get overall daily activity (last 90 days) excluding current user for comparison
    overall_daily_activity_query = text("""
        SELECT
            DATE(w.watched_at AT TIME ZONE 'UTC') as watch_date,
            COUNT(DISTINCT CASE WHEN m.media_type = 'movie' THEN w.id END) as movies_count,
            COUNT(DISTINCT CASE WHEN w.episode_item_id IS NOT NULL THEN w.id END) as episodes_count
        FROM watched_items w
        LEFT JOIN media_items m ON w.media_item_id = m.id
        WHERE w.watched_at >= NOW() - INTERVAL '90 days'
        AND w.user_id != :user_id
        GROUP BY DATE(w.watched_at AT TIME ZONE 'UTC')
        ORDER BY watch_date ASC
    """)
    overall_daily_activity_result = await db.execute(
        overall_daily_activity_query, {"user_id": current_user.id}
    )
    overall_daily_activity = [
        {
            "date": row.watch_date.isoformat(),
            "movies": row.movies_count or 0,
            "episodes": row.episodes_count or 0,
        }
        for row in overall_daily_activity_result.fetchall()
    ]

    # Get overall rating distribution for comparison excluding current user
    overall_rating_dist_query = text("""
        SELECT
            FLOOR(w.rating * 2) / 2 as rating_bucket,
            COUNT(*) as count
        FROM watched_items w
        WHERE w.rating IS NOT NULL
        AND w.user_id != :user_id
        GROUP BY FLOOR(w.rating * 2) / 2
        ORDER BY rating_bucket
    """)
    overall_rating_dist_result = await db.execute(
        overall_rating_dist_query, {"user_id": current_user.id}
    )
    overall_rating_distribution = [
        {"rating": float(row.rating_bucket), "count": row.count}
        for row in overall_rating_dist_result.fetchall()
    ]

    return {
        "user_stats": watch_stats,
        "daily_activity": daily_activity,
        "rating_distribution": rating_distribution,
        "integration_summary": integration_summary,
        "system_stats": system_stats,
        "activity_summary": {
            "last_7_days": last_7_days_count,
            "last_30_days": last_30_days_count,
        },
        "overall_daily_activity": overall_daily_activity,
        "overall_rating_distribution": overall_rating_distribution,
    }
