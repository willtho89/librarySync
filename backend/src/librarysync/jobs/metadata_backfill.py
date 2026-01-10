import logging
import random
from datetime import datetime, timedelta, timezone

from sqlalchemy import and_, or_, select, union_all
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
    WatchedItem,
)
from librarysync.db.session import SessionLocal, init_session_factory

logger = logging.getLogger(__name__)

METADATA_BACKFILL_JOB = "metadata_backfill"
METADATA_BACKFILL_FORCE_JOB = "metadata_backfill_force"
METADATA_BACKFILL_INTERVAL = timedelta(hours=9)
METADATA_BACKFILL_FORCE_INTERVAL = timedelta(days=3650)
METADATA_BACKFILL_LEASE = timedelta(hours=2)
METADATA_BACKFILL_RETRY_DELAY = timedelta(minutes=10)
METADATA_BACKFILL_BATCH_SIZE = 200
METADATA_BACKFILL_EPISODE_REFRESH_DELTA = timedelta(days=3)
METADATA_BACKFILL_METADATA_REFRESH_DELTA = timedelta(days=3)
METADATA_BACKFILL_SAMPLE_RATE = 0.10


async def process_metadata_backfill_once() -> int:
    init_session_factory()
    async with SessionLocal() as db:
        job = await claim_scheduled_job(
            db,
            METADATA_BACKFILL_FORCE_JOB,
            METADATA_BACKFILL_FORCE_INTERVAL,
            METADATA_BACKFILL_LEASE,
        )
        force_refresh = bool(job)
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
            await run_metadata_backfill(
                db,
                job,
                force_metadata_refresh=force_refresh,
                force_episode_refresh=force_refresh,
            )
        except Exception:
            logger.exception("Metadata backfill failed")
            await release_scheduled_job(db, job, METADATA_BACKFILL_RETRY_DELAY)
            return 0
        interval = (
            METADATA_BACKFILL_FORCE_INTERVAL
            if force_refresh
            else METADATA_BACKFILL_INTERVAL
        )
        await complete_scheduled_job(db, job, interval)
    return 1


async def run_metadata_backfill(
    db: AsyncSession,
    job: ScheduledJob,
    batch_size: int = METADATA_BACKFILL_BATCH_SIZE,
    *,
    force_metadata_refresh: bool = False,
    force_episode_refresh: bool = False,
) -> None:
    logger.info("Starting metadata backfill")
    now = datetime.now(timezone.utc)
    tmdb_required = await _is_tmdb_required(db)
    provider_overrides = await _load_background_provider_overrides(db)
    use_overrides_only = True
    episode_refresh_cutoff = now - METADATA_BACKFILL_EPISODE_REFRESH_DELTA
    metadata_refresh_delta = METADATA_BACKFILL_METADATA_REFRESH_DELTA
    background_user_id = _select_background_user_id(provider_overrides)

    await _backfill_media_items(
        db,
        background_user_id,
        batch_size,
        job,
        now=now,
        tmdb_required=tmdb_required,
        episode_refresh_cutoff=episode_refresh_cutoff,
        metadata_refresh_delta=metadata_refresh_delta,
        force_metadata_refresh=force_metadata_refresh,
        force_episode_refresh=force_episode_refresh,
        provider_overrides=provider_overrides,
        use_overrides_only=use_overrides_only,
    )

    logger.info("Finished metadata backfill")


async def _backfill_media_items(
    db: AsyncSession,
    user_id: str,
    batch_size: int,
    job: ScheduledJob,
    *,
    now: datetime,
    tmdb_required: bool,
    episode_refresh_cutoff: datetime,
    metadata_refresh_delta: timedelta,
    force_metadata_refresh: bool,
    force_episode_refresh: bool,
    provider_overrides: dict[str, MetadataProvider],
    use_overrides_only: bool,
) -> None:
    direct_ids = select(WatchedItem.media_item_id.label("media_item_id")).where(
        WatchedItem.media_item_id.is_not(None),
    )
    episode_ids = (
        select(EpisodeItem.show_media_item_id.label("media_item_id"))
        .join(WatchedItem, WatchedItem.episode_item_id == EpisodeItem.id)
    )
    union_ids = union_all(direct_ids, episode_ids).subquery()

    missing_predicate = _missing_metadata_predicate(tmdb_required)
    episode_predicate = _episode_refresh_predicate(tmdb_required, episode_refresh_cutoff)
    candidate_predicate = (
        or_(missing_predicate, episode_predicate)
        if episode_predicate is not None
        else missing_predicate
    )

    tmdb_override = provider_overrides.get("tmdb")
    offset = 0
    while True:
        result = await db.execute(
            select(MediaItem.id)
            .join(union_ids, union_ids.c.media_item_id == MediaItem.id)
            .where(
                MediaItem.media_type.in_(["movie", "tv", "anime"]),
                candidate_predicate,
            )
            .distinct()
            .order_by(MediaItem.id)
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
            if provider_overrides and _should_refresh_media_metadata(media_item, tmdb_required):
                if force_metadata_refresh or _should_sample_media_item(
                    media_item,
                    now,
                    metadata_refresh_delta,
                    METADATA_BACKFILL_SAMPLE_RATE,
                ):
                    await enrich_watched_metadata(
                        db,
                        user_id,
                        media_item,
                        None,
                        provider_overrides=provider_overrides,
                        use_overrides_only=use_overrides_only,
                    )
                    media_item.metadata_refreshed_at = now
            if _should_refresh_episode_list(
                media_item,
                episode_refresh_cutoff,
                tmdb_required=tmdb_required,
                force_episode_refresh=force_episode_refresh,
            ):
                if tmdb_override is None:
                    continue
                await backfill_show_episodes(
                    db,
                    user_id,
                    media_item,
                    provider_override=tmdb_override,
                )

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
    for name in ("tmdb", "tvdb", "imdb", "kitsu", "myanimelist", "anilist"):
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


def _missing_metadata_predicate(tmdb_required: bool):
    predicates = [
        MediaItem.imdb_id.is_(None),
        MediaItem.tvdb_id.is_(None),
        MediaItem.poster_url.is_(None),
    ]
    if tmdb_required:
        predicates.append(MediaItem.tmdb_id.is_(None))
    return or_(*predicates)


def _episode_refresh_predicate(tmdb_required: bool, cutoff: datetime):
    if not tmdb_required:
        return None
    return and_(
        MediaItem.media_type.in_(["tv", "anime"]),
        or_(MediaItem.updated_at.is_(None), MediaItem.updated_at < cutoff),
    )


def _select_background_user_id(provider_overrides: dict[str, MetadataProvider]) -> str:
    for provider in provider_overrides.values():
        return provider.context.user_id
    return "system"


def _should_sample_media_item(
    media_item: MediaItem,
    now: datetime,
    refresh_delta: timedelta,
    sample_rate: float,
) -> bool:
    last_refresh = media_item.metadata_refreshed_at or media_item.created_at or now
    age = now - last_refresh if now >= last_refresh else timedelta(0)
    if refresh_delta.total_seconds() <= 0:
        weight = 1.0
    else:
        over_delta = age - refresh_delta if age > refresh_delta else timedelta(0)
        weight = 1.0 + (over_delta.total_seconds() / refresh_delta.total_seconds())
    probability = min(1.0, sample_rate * weight)
    return random.random() < probability
