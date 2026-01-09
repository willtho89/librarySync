import logging
from datetime import timedelta

from sqlalchemy import select, union_all
from sqlalchemy.ext.asyncio import AsyncSession

from librarysync.core.metadata_enrichment import enrich_watched_metadata
from librarysync.core.scheduler import (
    claim_scheduled_job,
    complete_scheduled_job,
    release_scheduled_job,
)
from librarysync.db.models import EpisodeItem, MediaItem, User, WatchedItem
from librarysync.db.session import SessionLocal, init_session_factory

logger = logging.getLogger(__name__)

METADATA_BACKFILL_JOB = "metadata_backfill"
METADATA_BACKFILL_INTERVAL = timedelta(days=3650)
METADATA_BACKFILL_LEASE = timedelta(hours=2)
METADATA_BACKFILL_RETRY_DELAY = timedelta(minutes=10)
METADATA_BACKFILL_BATCH_SIZE = 200


async def process_metadata_backfill_once() -> int:
    init_session_factory()
    async with SessionLocal() as db:
        job = await claim_scheduled_job(
            db,
            METADATA_BACKFILL_JOB,
            METADATA_BACKFILL_INTERVAL,
            METADATA_BACKFILL_LEASE,
        )
        if not job:
            return 0
        try:
            await run_metadata_backfill(db)
        except Exception:
            logger.exception("Metadata backfill failed")
            await release_scheduled_job(db, job, METADATA_BACKFILL_RETRY_DELAY)
            return 0
        await complete_scheduled_job(db, job, METADATA_BACKFILL_INTERVAL)
    return 1


async def run_metadata_backfill(
    db: AsyncSession, batch_size: int = METADATA_BACKFILL_BATCH_SIZE
) -> None:
    logger.info("Starting metadata backfill")

    result = await db.execute(select(User.id))
    user_ids = result.scalars().all()

    for user_id in user_ids:
        await _backfill_user_media(db, user_id, batch_size)

    logger.info("Finished metadata backfill")


async def _backfill_user_media(db: AsyncSession, user_id: str, batch_size: int) -> None:
    direct_ids = select(WatchedItem.media_item_id.label("media_item_id")).where(
        WatchedItem.user_id == user_id,
        WatchedItem.media_item_id.is_not(None),
    )
    episode_ids = (
        select(EpisodeItem.show_media_item_id.label("media_item_id"))
        .join(WatchedItem, WatchedItem.episode_item_id == EpisodeItem.id)
        .where(WatchedItem.user_id == user_id)
    )
    union_ids = union_all(direct_ids, episode_ids).subquery()

    offset = 0
    while True:
        result = await db.execute(
            select(union_ids.c.media_item_id)
            .distinct()
            .order_by(union_ids.c.media_item_id)
            .offset(offset)
            .limit(batch_size)
        )
        media_ids = [row[0] for row in result.all() if row[0]]
        if not media_ids:
            break

        items_result = await db.execute(
            select(MediaItem).where(
                MediaItem.id.in_(media_ids),
                MediaItem.media_type.in_(["movie", "tv"]),
            )
        )
        media_items = items_result.scalars().all()

        for media_item in media_items:
            await enrich_watched_metadata(db, user_id, media_item, None)

        await db.commit()
        offset += batch_size
