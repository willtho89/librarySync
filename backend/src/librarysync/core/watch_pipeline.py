from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable, ClassVar

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from librarysync.config import settings
from librarysync.connectors.services.letterboxd import has_required_letterboxd_fields
from librarysync.connectors.services.simkl import has_required_simkl_fields
from librarysync.connectors.services.stremio import has_required_stremio_fields
from librarysync.connectors.services.trakt import has_required_trakt_fields
from librarysync.core.integrations import load_integration_with_secrets
from librarysync.core.metadata_enrichment import enrich_watched_metadata
from librarysync.db.models import (
    EpisodeItem,
    MediaItem,
    OutboxJob,
    WatchedItem,
    WatchSync,
)

SUCCESS_STATUSES = {
    "succeeded",
    "assumed_tracked",
    "synced_from_trakt",
    "synced_from_letterboxd",
    "synced_from_simkl",
    "synced_from_stremio",
}
ACTIVE_OUTBOX_STATUSES = {"pending", "failed_retryable", "in_progress"}


def is_synced_status(status: str | None) -> bool:
    if not status:
        return False
    if status in SUCCESS_STATUSES:
        return True
    return status.startswith("synced_from_")


def _build_outbox_dedupe_key(
    user_id: str,
    provider: str,
    job_type: str,
    payload: dict[str, object],
) -> str | None:
    watch_sync_id = payload.get("watch_sync_id")
    if watch_sync_id:
        return f"{user_id}:{provider}:{job_type}:{watch_sync_id}"
    watched_item_id = payload.get("watched_item_id")
    if watched_item_id:
        return f"{user_id}:{provider}:{job_type}:{watched_item_id}"
    return None


async def enqueue_outbox_job(
    db: AsyncSession,
    *,
    user_id: str,
    target_provider: str,
    job_type: str,
    payload: dict[str, object],
    status: str = "pending",
) -> OutboxJob:
    dedupe_key = _build_outbox_dedupe_key(user_id, target_provider, job_type, payload)
    now = datetime.now(timezone.utc)
    if dedupe_key:
        result = await db.execute(
            select(OutboxJob).where(
                OutboxJob.dedupe_key == dedupe_key,
                OutboxJob.status.in_(ACTIVE_OUTBOX_STATUSES),
            )
        )
        existing = result.scalars().first()
        if existing:
            if existing.status != "in_progress":
                existing.payload = payload
                existing.status = status
                existing.run_after = None
                existing.last_error = None
                existing.updated_at = now
            return existing
    job = OutboxJob(
        user_id=user_id,
        target_provider=target_provider,
        job_type=job_type,
        payload=payload,
        status=status,
        dedupe_key=dedupe_key,
    )
    db.add(job)
    return job


class SyncStrategy(ABC):
    provider: ClassVar[str]

    @abstractmethod
    async def enqueue_new(
        self,
        db: AsyncSession,
        watched: WatchedItem,
        media_item: MediaItem | None,
        episode_item: EpisodeItem | None,
        is_rewatch: bool,
        force: bool = False,
    ) -> None:
        raise NotImplementedError

    async def enqueue_update(
        self,
        db: AsyncSession,
        watched: WatchedItem,
        media_item: MediaItem | None,
        episode_item: EpisodeItem | None,
        watched_at_updated: bool,
        rating_updated: bool,
    ) -> None:
        return None

    async def enqueue_delete(
        self,
        db: AsyncSession,
        watched: WatchedItem,
        media_item: MediaItem | None,
        episode_item: EpisodeItem | None,
    ) -> None:
        return None


class SyncStrategyRegistry:
    def __init__(self, strategies: list[SyncStrategy]) -> None:
        self._strategies = {strategy.provider: strategy for strategy in strategies}

    def get(self, provider: str) -> SyncStrategy | None:
        return self._strategies.get(provider)

    def list(self) -> list[SyncStrategy]:
        return list(self._strategies.values())


class SyncCoordinator:
    def __init__(self, registry: SyncStrategyRegistry) -> None:
        self._registry = registry

    async def enqueue_new(
        self,
        db: AsyncSession,
        watched: WatchedItem,
        media_item: MediaItem | None,
        episode_item: EpisodeItem | None,
        is_rewatch: bool,
        force: bool = False,
    ) -> None:
        for strategy in self._registry.list():
            await strategy.enqueue_new(
                db, watched, media_item, episode_item, is_rewatch, force=force
            )

    async def enqueue_update(
        self,
        provider: str,
        db: AsyncSession,
        watched: WatchedItem,
        media_item: MediaItem | None,
        episode_item: EpisodeItem | None,
        watched_at_updated: bool,
        rating_updated: bool,
    ) -> None:
        strategy = self._registry.get(provider)
        if not strategy:
            return
        await strategy.enqueue_update(
            db, watched, media_item, episode_item, watched_at_updated, rating_updated
        )

    async def enqueue_update_all(
        self,
        db: AsyncSession,
        watched: WatchedItem,
        media_item: MediaItem | None,
        episode_item: EpisodeItem | None,
        watched_at_updated: bool,
        rating_updated: bool,
    ) -> None:
        for strategy in self._registry.list():
            await strategy.enqueue_update(
                db, watched, media_item, episode_item, watched_at_updated, rating_updated
            )

    async def enqueue_delete(
        self,
        provider: str,
        db: AsyncSession,
        watched: WatchedItem,
        media_item: MediaItem | None,
        episode_item: EpisodeItem | None,
    ) -> None:
        strategy = self._registry.get(provider)
        if not strategy:
            return
        await strategy.enqueue_delete(db, watched, media_item, episode_item)

    async def enqueue_delete_all(
        self,
        db: AsyncSession,
        watched: WatchedItem,
        media_item: MediaItem | None,
        episode_item: EpisodeItem | None,
    ) -> None:
        for strategy in self._registry.list():
            await strategy.enqueue_delete(db, watched, media_item, episode_item)


async def enqueue_new_item_job(
    db: AsyncSession,
    user_id: str,
    watched_item_id: str,
    is_rewatch: bool | None = None,
    source: str | None = None,
) -> OutboxJob:
    payload: dict[str, object] = {"watched_item_id": watched_item_id}
    if is_rewatch is not None:
        payload["is_rewatch"] = bool(is_rewatch)
    if source:
        payload["source"] = source
    return await enqueue_outbox_job(
        db,
        user_id=user_id,
        target_provider="internal",
        job_type="new_item_added",
        payload=payload,
        status="pending",
    )


async def process_new_item_job(db: AsyncSession, job: OutboxJob) -> None:
    payload = job.payload or {}
    watched_id = payload.get("watched_item_id")
    if not watched_id:
        raise ValueError("new_item_added requires watched_item_id")
    watched = await db.get(WatchedItem, str(watched_id))
    if not watched:
        raise ValueError("watched item not found")
    media_item = None
    episode_item = None
    if watched.media_item_id:
        media_item = await db.get(MediaItem, watched.media_item_id)
    if watched.episode_item_id:
        episode_item = await db.get(EpisodeItem, watched.episode_item_id)
        if episode_item and not media_item:
            media_item = await db.get(MediaItem, episode_item.show_media_item_id)
    if not media_item and not episode_item:
        raise ValueError("watched item missing media references")
    is_rewatch = bool(payload.get("is_rewatch"))

    await enrich_watched_metadata(db, watched.user_id, media_item, episode_item)
    await _sync_to_integrations(db, watched, media_item, episode_item, is_rewatch)


async def _sync_to_integrations(
    db: AsyncSession,
    watched: WatchedItem,
    media_item: MediaItem | None,
    episode_item: EpisodeItem | None,
    is_rewatch: bool,
) -> None:
    await SYNC_COORDINATOR.enqueue_new(
        db, watched, media_item, episode_item, is_rewatch
    )


class LetterboxdSyncStrategy(SyncStrategy):
    provider = "letterboxd"

    async def enqueue_new(
        self,
        db: AsyncSession,
        watched: WatchedItem,
        media_item: MediaItem | None,
        episode_item: EpisodeItem | None,
        is_rewatch: bool,
        force: bool = False,
    ) -> None:
        if episode_item:
            return
        if not media_item or media_item.media_type != "movie":
            return
        if not media_item.imdb_id and not media_item.tmdb_id:
            return
        integration, secret_data = await load_integration_with_secrets(
            db, watched.user_id, "letterboxd"
        )
        if not integration or not secret_data:
            return
        if not has_required_letterboxd_fields(secret_data):
            return
        watch_sync = await _get_watch_sync(db, watched.id, "letterboxd")
        if watch_sync and is_synced_status(watch_sync.status) and not force:
            return
        if watch_sync and watch_sync.status in {"pending", "in_progress"} and not force:
            return
        if not watch_sync:
            watch_sync = WatchSync(
                user_id=watched.user_id,
                watched_item_id=watched.id,
                provider="letterboxd",
                status="pending",
                is_rewatch=is_rewatch,
            )
            db.add(watch_sync)
            await db.flush()
        else:
            watch_sync.status = "pending"
            watch_sync.last_error = None

        imdb_id = media_item.imdb_id.lower() if media_item.imdb_id else None
        tmdb_id = media_item.tmdb_id if media_item.tmdb_id else None
        await enqueue_outbox_job(
            db,
            user_id=watched.user_id,
            target_provider="letterboxd",
            job_type="push_watched",
            payload={
                "watch_sync_id": watch_sync.id,
                "watched_item_id": watched.id,
                "media_item_id": media_item.id,
                "imdb_id": imdb_id,
                "tmdb_id": tmdb_id,
                "watched_at": watched.watched_at.isoformat(),
                "is_rewatch": is_rewatch,
                "rating": watched.rating,
            },
            status="pending",
        )

    async def enqueue_update(
        self,
        db: AsyncSession,
        watched: WatchedItem,
        media_item: MediaItem | None,
        episode_item: EpisodeItem | None,
        watched_at_updated: bool,
        rating_updated: bool,
    ) -> None:
        if episode_item:
            return
        if not media_item or media_item.media_type != "movie":
            return
        integration, secret_data = await load_integration_with_secrets(
            db, watched.user_id, "letterboxd"
        )
        if not integration or not secret_data:
            return
        if not has_required_letterboxd_fields(secret_data):
            return
        result = await db.execute(
            select(WatchSync).where(
                WatchSync.watched_item_id == watched.id,
                WatchSync.provider == "letterboxd",
            )
        )
        watch_sync = result.scalars().first()
        if not watch_sync or not watch_sync.external_id:
            return

        payload: dict[str, object] = {
            "entry_id": watch_sync.external_id,
            "watched_item_id": watched.id,
            "watch_sync_id": watch_sync.id,
        }
        if watched_at_updated:
            payload["watched_at"] = watched.watched_at.isoformat()
        if rating_updated and watched.rating is not None:
            payload["rating"] = watched.rating

        watch_sync.status = "pending"
        watch_sync.last_error = None

        await enqueue_outbox_job(
            db,
            user_id=watched.user_id,
            target_provider="letterboxd",
            job_type="update_log_entry",
            payload=payload,
            status="pending",
        )

    async def enqueue_delete(
        self,
        db: AsyncSession,
        watched: WatchedItem,
        media_item: MediaItem | None,
        episode_item: EpisodeItem | None,
    ) -> None:
        if episode_item:
            return
        if not media_item or media_item.media_type != "movie":
            return
        integration, secret_data = await load_integration_with_secrets(
            db, watched.user_id, "letterboxd"
        )
        if not integration or not secret_data:
            return
        if not has_required_letterboxd_fields(secret_data):
            return
        result = await db.execute(
            select(WatchSync).where(
                WatchSync.watched_item_id == watched.id,
                WatchSync.provider == "letterboxd",
            )
        )
        watch_sync = result.scalars().first()
        if not watch_sync or not watch_sync.external_id:
            return
        payload = {
            "entry_id": watch_sync.external_id,
            "watched_item_id": watched.id,
        }
        await enqueue_outbox_job(
            db,
            user_id=watched.user_id,
            target_provider="letterboxd",
            job_type="delete_log_entry",
            payload=payload,
            status="pending",
        )


@dataclass(frozen=True)
class HistorySyncConfig:
    provider: str
    client_id_attr: str
    client_secret_attr: str
    has_required_fields: Callable[[dict[str, Any]], bool]
    build_payload: Callable[
        [MediaItem, EpisodeItem | None, datetime, float | None], dict[str, object] | None
    ]
    include_history_id_on_delete: bool = False


class HistorySyncStrategy(SyncStrategy):
    def __init__(self, config: HistorySyncConfig) -> None:
        self._config = config
        self.provider = config.provider

    def _settings_ready(self) -> bool:
        client_id = getattr(settings, self._config.client_id_attr, None)
        client_secret = getattr(settings, self._config.client_secret_attr, None)
        return bool(client_id and client_secret)

    async def _has_integration(self, db: AsyncSession, user_id: str) -> bool:
        integration, secret_data = await load_integration_with_secrets(
            db, user_id, self.provider
        )
        if not integration or not secret_data:
            return False
        return self._config.has_required_fields(secret_data)

    async def enqueue_new(
        self,
        db: AsyncSession,
        watched: WatchedItem,
        media_item: MediaItem | None,
        episode_item: EpisodeItem | None,
        is_rewatch: bool,
        force: bool = False,
    ) -> None:
        if not media_item:
            return
        if not self._settings_ready():
            return
        payload = self._config.build_payload(
            media_item,
            episode_item,
            watched.watched_at,
            watched.rating,
        )
        if not payload:
            return
        if not await self._has_integration(db, watched.user_id):
            return

        watch_sync = await _get_watch_sync(db, watched.id, self.provider)
        if watch_sync and is_synced_status(watch_sync.status) and not force:
            return
        if watch_sync and watch_sync.status in {"pending", "in_progress"} and not force:
            return

        same_day_duplicate = await _has_same_day_watch(
            db,
            watched.user_id,
            media_item.id if not episode_item else None,
            episode_item.id if episode_item else None,
            watched.watched_at,
            watched.id,
        )
        now = datetime.now(timezone.utc)
        watch_status = "pending"
        if same_day_duplicate and watched.rating is None:
            watch_status = "assumed_tracked"

        if not watch_sync:
            watch_sync = WatchSync(
                user_id=watched.user_id,
                watched_item_id=watched.id,
                provider=self.provider,
                status=watch_status,
                is_rewatch=is_rewatch,
            )
            if watch_status == "assumed_tracked":
                watch_sync.last_synced_at = now
            db.add(watch_sync)
            await db.flush()
        else:
            watch_sync.status = watch_status
            watch_sync.last_error = None
            if watch_status == "assumed_tracked":
                watch_sync.last_synced_at = now

        payload["watch_sync_id"] = watch_sync.id
        payload["watched_item_id"] = watched.id
        if watch_status != "assumed_tracked" and not same_day_duplicate:
            await enqueue_outbox_job(
                db,
                user_id=watched.user_id,
                target_provider=self.provider,
                job_type="push_watched",
                payload=payload,
                status="pending",
            )
        if watched.rating is not None:
            rating_payload = dict(payload)
            rating_payload["rating"] = watched.rating
            await enqueue_outbox_job(
                db,
                user_id=watched.user_id,
                target_provider=self.provider,
                job_type="push_rating",
                payload=rating_payload,
                status="pending",
            )

    async def enqueue_update(
        self,
        db: AsyncSession,
        watched: WatchedItem,
        media_item: MediaItem | None,
        episode_item: EpisodeItem | None,
        watched_at_updated: bool,
        rating_updated: bool,
    ) -> None:
        if not media_item:
            return
        if not self._settings_ready():
            return
        if not await self._has_integration(db, watched.user_id):
            return
        result = await db.execute(
            select(WatchSync).where(
                WatchSync.watched_item_id == watched.id, WatchSync.provider == self.provider
            )
        )
        watch_sync = result.scalars().first()
        if not watch_sync:
            return
        payload = self._config.build_payload(
            media_item,
            episode_item,
            watched.watched_at,
            watched.rating,
        )
        if not payload:
            return
        payload["watch_sync_id"] = watch_sync.id
        payload["watched_item_id"] = watched.id
        if watch_sync.external_id:
            payload["history_id"] = watch_sync.external_id
        if watched_at_updated:
            payload["watched_at"] = watched.watched_at.isoformat()
        if rating_updated and watched.rating is not None:
            payload["rating"] = watched.rating

        watch_sync.status = "pending"
        watch_sync.last_error = None
        db.add(watch_sync)

        if watched_at_updated:
            await enqueue_outbox_job(
                db,
                user_id=watched.user_id,
                target_provider=self.provider,
                job_type="update_history",
                payload=payload,
                status="pending",
            )
        if rating_updated and watched.rating is not None:
            rating_payload = dict(payload)
            rating_payload["rating"] = watched.rating
            await enqueue_outbox_job(
                db,
                user_id=watched.user_id,
                target_provider=self.provider,
                job_type="push_rating",
                payload=rating_payload,
                status="pending",
            )

    async def enqueue_delete(
        self,
        db: AsyncSession,
        watched: WatchedItem,
        media_item: MediaItem | None,
        episode_item: EpisodeItem | None,
    ) -> None:
        if not media_item:
            return
        if not self._settings_ready():
            return
        if not await self._has_integration(db, watched.user_id):
            return
        payload = self._config.build_payload(
            media_item,
            episode_item,
            watched.watched_at,
            watched.rating,
        )
        if not payload:
            return
        payload.pop("watched_at", None)
        payload.pop("rating", None)
        if self._config.include_history_id_on_delete:
            watch_sync = await _get_watch_sync(db, watched.id, self.provider)
            if watch_sync and watch_sync.external_id:
                payload["history_id"] = watch_sync.external_id
        payload["watched_item_id"] = watched.id
        await enqueue_outbox_job(
            db,
            user_id=watched.user_id,
            target_provider=self.provider,
            job_type="remove_history",
            payload=payload,
            status="pending",
        )


class TraktSyncStrategy(HistorySyncStrategy):
    def __init__(self) -> None:
        super().__init__(
            HistorySyncConfig(
                provider="trakt",
                client_id_attr="trakt_client_id",
                client_secret_attr="trakt_client_secret",
                has_required_fields=has_required_trakt_fields,
                build_payload=build_trakt_payload,
                include_history_id_on_delete=True,
            )
        )


class SimklSyncStrategy(HistorySyncStrategy):
    def __init__(self) -> None:
        super().__init__(
            HistorySyncConfig(
                provider="simkl",
                client_id_attr="simkl_client_id",
                client_secret_attr="simkl_client_secret",
                has_required_fields=has_required_simkl_fields,
                build_payload=build_simkl_payload,
            )
        )


class StremioSyncStrategy(SyncStrategy):
    provider = "stremio"

    async def enqueue_new(
        self,
        db: AsyncSession,
        watched: WatchedItem,
        media_item: MediaItem | None,
        episode_item: EpisodeItem | None,
        is_rewatch: bool,
        force: bool = False,
    ) -> None:
        if not media_item:
            return
        integration, secret_data = await load_integration_with_secrets(
            db, watched.user_id, "stremio"
        )
        if not integration or not secret_data:
            return
        if not has_required_stremio_fields(secret_data):
            return
        payload = build_stremio_payload(media_item, episode_item, watched.watched_at)
        if not payload:
            return

        watch_sync = await _get_watch_sync(db, watched.id, "stremio")
        if watch_sync and is_synced_status(watch_sync.status) and not force:
            return
        if watch_sync and watch_sync.status in {"pending", "in_progress"} and not force:
            return

        if not watch_sync:
            watch_sync = WatchSync(
                user_id=watched.user_id,
                watched_item_id=watched.id,
                provider="stremio",
                status="pending",
                is_rewatch=is_rewatch,
            )
            db.add(watch_sync)
            await db.flush()
        else:
            watch_sync.status = "pending"
            watch_sync.last_error = None

        payload["watch_sync_id"] = watch_sync.id
        payload["watched_item_id"] = watched.id
        await enqueue_outbox_job(
            db,
            user_id=watched.user_id,
            target_provider="stremio",
            job_type="push_watched",
            payload=payload,
            status="pending",
        )

    async def enqueue_delete(
        self,
        db: AsyncSession,
        watched: WatchedItem,
        media_item: MediaItem | None,
        episode_item: EpisodeItem | None,
    ) -> None:
        if not media_item:
            return
        integration, secret_data = await load_integration_with_secrets(
            db, watched.user_id, "stremio"
        )
        if not integration or not secret_data:
            return
        if not has_required_stremio_fields(secret_data):
            return
        payload = build_stremio_payload(media_item, episode_item, watched.watched_at)
        if not payload:
            return
        payload.pop("watched_at", None)
        payload["watched_item_id"] = watched.id
        await enqueue_outbox_job(
            db,
            user_id=watched.user_id,
            target_provider="stremio",
            job_type="remove_watched",
            payload=payload,
            status="pending",
        )


async def _get_watch_sync(
    db: AsyncSession, watched_id: str, provider: str
) -> WatchSync | None:
    result = await db.execute(
        select(WatchSync).where(
            WatchSync.watched_item_id == watched_id,
            WatchSync.provider == provider,
        )
    )
    return result.scalars().first()


async def _has_same_day_watch(
    db: AsyncSession,
    user_id: str,
    media_item_id: str | None,
    episode_item_id: str | None,
    watched_at: datetime,
    exclude_watched_id: str | None = None,
) -> bool:
    if not media_item_id and not episode_item_id:
        return False
    target_date = watched_at.date()
    query = select(WatchedItem.id).where(
        WatchedItem.user_id == user_id,
        func.date(WatchedItem.watched_at) == target_date,
    )
    if media_item_id:
        query = query.where(WatchedItem.media_item_id == media_item_id)
    if episode_item_id:
        query = query.where(WatchedItem.episode_item_id == episode_item_id)
    if exclude_watched_id:
        query = query.where(WatchedItem.id != exclude_watched_id)
    query = query.limit(1)
    result = await db.execute(query)
    return result.scalars().first() is not None


def collect_external_ids(
    imdb_id: str | None, tmdb_id: str | None, tvdb_id: str | None
) -> dict[str, object]:
    ids: dict[str, object] = {}
    if imdb_id:
        ids["imdb"] = imdb_id.lower()
    if tmdb_id:
        ids["tmdb"] = tmdb_id
    if tvdb_id:
        ids["tvdb"] = tvdb_id
    return ids


def build_history_payload(
    media_item: MediaItem,
    episode_item: EpisodeItem | None,
    watched_at: datetime,
    rating: float | None,
    id_builder: Callable[[str | None, str | None, str | None], dict[str, object]],
) -> dict[str, object] | None:
    if episode_item:
        show_ids = id_builder(media_item.imdb_id, media_item.tmdb_id, media_item.tvdb_id)
        episode_ids = id_builder(
            episode_item.imdb_id, episode_item.tmdb_id, episode_item.tvdb_id
        )
        if not show_ids and not episode_ids:
            return None
        payload: dict[str, object] = {
            "media_type": "tv",
            "season_number": episode_item.season_number,
            "episode_number": episode_item.episode_number,
            "watched_at": watched_at.isoformat(),
        }
        if show_ids:
            payload["show_ids"] = show_ids
        if episode_ids:
            payload["episode_ids"] = episode_ids
        if rating is not None:
            payload["rating"] = rating
        return payload

    if media_item.media_type != "movie":
        return None
    movie_ids = id_builder(media_item.imdb_id, media_item.tmdb_id, media_item.tvdb_id)
    if not movie_ids:
        return None
    payload: dict[str, object] = {
        "media_type": "movie",
        "movie_ids": movie_ids,
        "watched_at": watched_at.isoformat(),
    }
    if rating is not None:
        payload["rating"] = rating
    return payload


def build_trakt_payload(
    media_item: MediaItem,
    episode_item: EpisodeItem | None,
    watched_at: datetime,
    rating: float | None,
) -> dict[str, object] | None:
    return build_history_payload(
        media_item,
        episode_item,
        watched_at,
        rating,
        collect_external_ids,
    )


def build_simkl_payload(
    media_item: MediaItem,
    episode_item: EpisodeItem | None,
    watched_at: datetime,
    rating: float | None,
) -> dict[str, object] | None:
    return build_history_payload(
        media_item,
        episode_item,
        watched_at,
        rating,
        collect_external_ids,
    )


SYNC_STRATEGY_REGISTRY = SyncStrategyRegistry(
    [
        LetterboxdSyncStrategy(),
        TraktSyncStrategy(),
        SimklSyncStrategy(),
        StremioSyncStrategy(),
    ]
)
SYNC_COORDINATOR = SyncCoordinator(SYNC_STRATEGY_REGISTRY)


def build_stremio_payload(
    media_item: MediaItem,
    episode_item: EpisodeItem | None,
    watched_at: datetime,
) -> dict[str, object] | None:
    item_id = _extract_stremio_item_id(media_item)
    if not item_id:
        return None
    if episode_item:
        video_id = _extract_stremio_video_id(media_item, episode_item)
        if not video_id:
            return None

    media_type = "series" if episode_item or media_item.media_type == "tv" else "movie"
    payload: dict[str, object] = {
        "item_id": item_id,
        "media_type": media_type,
        "watched_at": watched_at.isoformat(),
    }
    if media_item.title:
        payload["title"] = media_item.title
    if media_item.year is not None:
        payload["year"] = media_item.year
    if media_item.poster_url:
        payload["poster"] = media_item.poster_url

    state = _extract_stremio_state(media_item, episode_item)
    times_watched = _coerce_int(state.get("timesWatched"))
    flagged_watched = _coerce_int(state.get("flaggedWatched"))
    payload["times_watched"] = max(times_watched or 0, 1)
    payload["flagged_watched"] = max(flagged_watched or 0, 1)

    if episode_item:
        payload["video_id"] = video_id
        payload["season_number"] = episode_item.season_number
        payload["episode_number"] = episode_item.episode_number

    return payload


def _extract_stremio_item_id(media_item: MediaItem) -> str | None:
    raw = media_item.raw if isinstance(media_item.raw, dict) else {}
    stremio_id = _coerce_str(raw.get("stremio_id"))
    if stremio_id:
        return stremio_id
    stremio_payload = raw.get("stremio")
    if isinstance(stremio_payload, dict):
        stremio_id = _coerce_str(stremio_payload.get("id") or stremio_payload.get("_id"))
        if stremio_id:
            return stremio_id
    if media_item.imdb_id:
        return media_item.imdb_id
    return None


def _extract_stremio_video_id(
    media_item: MediaItem, episode_item: EpisodeItem
) -> str | None:
    raw = episode_item.raw if isinstance(episode_item.raw, dict) else {}
    stremio_video_id = _coerce_str(raw.get("stremio_video_id"))
    if not stremio_video_id:
        stremio_payload = raw.get("stremio")
        if isinstance(stremio_payload, dict):
            stremio_video_id = _coerce_str(
                stremio_payload.get("video_id") or stremio_payload.get("videoId")
            )
            if not stremio_video_id:
                state = stremio_payload.get("state")
                if isinstance(state, dict):
                    stremio_video_id = _coerce_str(
                        state.get("video_id") or state.get("videoId")
                    )
    if stremio_video_id:
        return stremio_video_id
    if episode_item.imdb_id:
        return episode_item.imdb_id
    if media_item.imdb_id:
        return f"{media_item.imdb_id}:{episode_item.season_number}:{episode_item.episode_number}"
    return None


def _extract_stremio_state(
    media_item: MediaItem, episode_item: EpisodeItem | None
) -> dict[str, object]:
    raw = episode_item.raw if episode_item and isinstance(episode_item.raw, dict) else {}
    if not raw and isinstance(media_item.raw, dict):
        raw = media_item.raw
    stremio_payload = raw.get("stremio")
    if isinstance(stremio_payload, dict):
        state = stremio_payload.get("state")
        if isinstance(state, dict):
            return state
    state = raw.get("state")
    if isinstance(state, dict):
        return state
    return {}


def _coerce_str(value: object) -> str | None:
    if value is None:
        return None
    if isinstance(value, str):
        cleaned = value.strip()
        return cleaned or None
    if isinstance(value, (int, float)):
        return str(value)
    return None


def _coerce_int(value: object) -> int | None:
    if value is None:
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    if isinstance(value, str):
        cleaned = value.strip()
        if cleaned.isdigit():
            return int(cleaned)
    return None
