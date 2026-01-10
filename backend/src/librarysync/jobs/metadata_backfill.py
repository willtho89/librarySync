import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy import select, union_all
from sqlalchemy.ext.asyncio import AsyncSession

from librarysync.connectors.metadata.base import MetadataProvider
from librarysync.core.metadata_enrichment import enrich_watched_metadata
from librarysync.core.metadata_providers import load_random_provider
from librarysync.core.scheduler import (
    claim_scheduled_job,
    complete_scheduled_job,
    extend_scheduled_job,
    release_scheduled_job,
)
from librarysync.core.watchlist import backfill_show_episodes
from librarysync.db.models import (
    EpisodeItem,
    Integration,
    IntegrationSecret,
    MediaItem,
    ScheduledJob,
    User,
    WatchedItem,
)
from librarysync.db.session import SessionLocal, init_session_factory

logger = logging.getLogger(__name__)

METADATA_BACKFILL_JOB = "metadata_backfill"
METADATA_BACKFILL_FORCE_JOB = "metadata_backfill_force"
METADATA_BACKFILL_INTERVAL = timedelta(days=1)
METADATA_BACKFILL_FORCE_INTERVAL = timedelta(days=3650)
METADATA_BACKFILL_LEASE = timedelta(hours=2)
METADATA_BACKFILL_RETRY_DELAY = timedelta(minutes=10)
METADATA_BACKFILL_BATCH_SIZE = 200
METADATA_BACKFILL_EPISODE_REFRESH_DELTA = timedelta(days=3)


async def process_metadata_backfill_once() -> int:
    init_session_factory()
    async with SessionLocal() as db:
        job = await claim_scheduled_job(
            db,
            METADATA_BACKFILL_FORCE_JOB,
            METADATA_BACKFILL_FORCE_INTERVAL,
            METADATA_BACKFILL_LEASE,
        )
        force_episode_refresh = bool(job)
        if not job:
            job = await claim_scheduled_job(
                db,
                METADATA_BACKFILL_JOB,
                METADATA_BACKFILL_INTERVAL,
                METADATA_BACKFILL_LEASE,
            )
        if not job:
            return 0
        try:
            await run_metadata_backfill(db, job, force_episode_refresh=force_episode_refresh)
        except Exception:
            logger.exception("Metadata backfill failed")
            await release_scheduled_job(db, job, METADATA_BACKFILL_RETRY_DELAY)
            return 0
        interval = (
            METADATA_BACKFILL_FORCE_INTERVAL
            if force_episode_refresh
            else METADATA_BACKFILL_INTERVAL
        )
        await complete_scheduled_job(db, job, interval)
    return 1


async def run_metadata_backfill(
    db: AsyncSession,
    job: ScheduledJob,
    batch_size: int = METADATA_BACKFILL_BATCH_SIZE,
    *,
    force_episode_refresh: bool = False,
) -> None:
    logger.info("Starting metadata backfill")
    tmdb_required = await _is_tmdb_required(db)
    provider_overrides = await _load_background_provider_overrides(db)
    use_overrides_only = bool(provider_overrides)
    episode_refresh_cutoff = datetime.now(timezone.utc) - METADATA_BACKFILL_EPISODE_REFRESH_DELTA

    result = await db.execute(select(User.id))
    user_ids = result.scalars().all()

    for user_id in user_ids:
        await _backfill_user_media(
            db,
            user_id,
            batch_size,
            job,
            tmdb_required=tmdb_required,
            episode_refresh_cutoff=episode_refresh_cutoff,
            force_episode_refresh=force_episode_refresh,
            provider_overrides=provider_overrides,
            use_overrides_only=use_overrides_only,
        )
        await extend_scheduled_job(db, job, METADATA_BACKFILL_LEASE)

    logger.info("Finished metadata backfill")


async def _backfill_user_media(
    db: AsyncSession,
    user_id: str,
    batch_size: int,
    job: ScheduledJob,
    *,
    tmdb_required: bool,
    episode_refresh_cutoff: datetime,
    force_episode_refresh: bool,
    provider_overrides: dict[str, MetadataProvider],
    use_overrides_only: bool,
) -> None:
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
                MediaItem.media_type.in_(["movie", "tv", "anime"]),
            )
        )
        media_items = items_result.scalars().all()

        for media_item in media_items:
            if _should_refresh_media_metadata(media_item, tmdb_required):
                await enrich_watched_metadata(
                    db,
                    user_id,
                    media_item,
                    None,
                    provider_overrides=provider_overrides,
                    use_overrides_only=use_overrides_only,
                )
            if _should_refresh_episode_list(
                media_item,
                episode_refresh_cutoff,
                tmdb_required=tmdb_required,
                force_episode_refresh=force_episode_refresh,
            ):
                await backfill_show_episodes(db, user_id, media_item)

        await db.commit()
        await extend_scheduled_job(db, job, METADATA_BACKFILL_LEASE)
        offset += batch_size


async def _is_tmdb_required(db: AsyncSession) -> bool:
    result = await db.execute(
        select(Integration.id, Integration.config, IntegrationSecret.integration_id)
        .outerjoin(IntegrationSecret, IntegrationSecret.integration_id == Integration.id)
        .where(Integration.provider == "tmdb")
    )
    for integration_id, config, secret_id in result.all():
        if not integration_id or not isinstance(config, dict):
            continue
        if not config.get("enabled"):
            continue
        if secret_id:
            return True
    return False


async def _load_background_provider_overrides(
    db: AsyncSession,
) -> dict[str, MetadataProvider]:
    overrides: dict[str, MetadataProvider] = {}
    for name in ("tmdb", "tvdb", "imdb"):
        provider = await load_random_provider(db, name)
        if provider:
            overrides[name] = provider
    return overrides


def _should_refresh_media_metadata(media_item: MediaItem, tmdb_required: bool) -> bool:
    missing_imdb = not media_item.imdb_id
    missing_tvdb = not media_item.tvdb_id
    missing_tmdb = tmdb_required and not media_item.tmdb_id
    poster_missing = not media_item.poster_url
    return missing_imdb or missing_tvdb or missing_tmdb or poster_missing


def _should_refresh_episode_list(
    media_item: MediaItem,
    cutoff: datetime,
    *,
    tmdb_required: bool,
    force_episode_refresh: bool,
) -> bool:
    if force_episode_refresh:
        return tmdb_required and media_item.media_type in {"tv", "anime"}
    if not tmdb_required:
        return False
    if media_item.media_type not in {"tv", "anime"}:
        return False
    updated_at = media_item.updated_at
    if not updated_at:
        return True
    return updated_at < cutoff
