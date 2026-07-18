from __future__ import annotations

from typing import Any, Callable

from sqlalchemy.ext.asyncio import AsyncSession

from librarysync.connectors.services.letterboxd import has_required_letterboxd_fields
from librarysync.connectors.services.publicmetadb import has_required_publicmetadb_fields
from librarysync.connectors.services.simkl import has_required_simkl_fields
from librarysync.connectors.services.trakt import has_required_trakt_fields
from librarysync.core.integrations import load_integration_with_secrets
from librarysync.core.publicmetadb import is_publicmetadb_sync_enabled
from librarysync.core.watch_pipeline import collect_external_ids, enqueue_outbox_job
from librarysync.core.watchlist_sources import ensure_personal_watchlist_source
from librarysync.db.models import MediaItem, WatchlistItem


async def enqueue_personal_watchlist_sync(
    db: AsyncSession,
    watchlist_item: WatchlistItem,
    media_item: MediaItem | None,
) -> None:
    if not media_item:
        return
    await _enqueue_trakt_watchlist(db, watchlist_item, media_item)
    await _enqueue_simkl_watchlist(db, watchlist_item, media_item)
    await _enqueue_publicmetadb_watchlist(db, watchlist_item, media_item)
    await _enqueue_letterboxd_watchlist(db, watchlist_item, media_item)


async def enqueue_personal_watchlist_removal(
    db: AsyncSession,
    watchlist_item: WatchlistItem,
    media_item: MediaItem | None,
) -> None:
    if not media_item:
        return
    await _enqueue_trakt_watchlist_removal(db, watchlist_item, media_item)
    await _enqueue_simkl_watchlist_removal(db, watchlist_item, media_item)
    await _enqueue_publicmetadb_watchlist_removal(db, watchlist_item, media_item)
    await _enqueue_letterboxd_watchlist_removal(db, watchlist_item, media_item)


WatchlistPayloadBuilder = Callable[[WatchlistItem, MediaItem], dict[str, Any] | None]


async def _enqueue_watchlist_job(
    db: AsyncSession,
    watchlist_item: WatchlistItem,
    media_item: MediaItem,
    *,
    provider: str,
    source_name: str,
    job_type: str,
    required_fields: Callable[[dict[str, object]], bool],
    build_payload: WatchlistPayloadBuilder,
    sync_enabled: Callable[[dict[str, object]], bool] | None = None,
) -> None:
    integration, secret_data = await load_integration_with_secrets(
        db, watchlist_item.user_id, provider
    )
    if not integration or integration.status == "disconnected" or not secret_data:
        return
    if not required_fields(secret_data):
        return
    if sync_enabled and not sync_enabled(dict(integration.config or {})):
        return

    source = await ensure_personal_watchlist_source(
        db,
        user_id=watchlist_item.user_id,
        provider=provider,
        name=source_name,
    )
    if not source.is_enabled:
        return

    payload = build_payload(watchlist_item, media_item)
    if not payload:
        return

    await enqueue_outbox_job(
        db,
        user_id=watchlist_item.user_id,
        target_provider=provider,
        job_type=job_type,
        payload=payload,
        status="pending",
    )


def _base_watchlist_payload(
    watchlist_item: WatchlistItem,
    media_item: MediaItem,
) -> dict[str, Any]:
    return {
        "watchlist_item_id": watchlist_item.id,
        "media_item_id": media_item.id,
        "media_type": watchlist_item.type,
        "imdb_id": media_item.imdb_id,
        "tmdb_id": media_item.tmdb_id,
        "tvdb_id": media_item.tvdb_id,
    }


def _build_trakt_payload(
    watchlist_item: WatchlistItem,
    media_item: MediaItem,
) -> dict[str, Any] | None:
    ids = collect_external_ids(media_item.imdb_id, media_item.tmdb_id, media_item.tvdb_id)
    if not ids:
        return None
    payload = _base_watchlist_payload(watchlist_item, media_item)
    if watchlist_item.type in {"movie", "anime"}:
        payload["movie_ids"] = ids
    elif watchlist_item.type == "tv":
        payload["show_ids"] = ids
    else:
        return None
    return payload


def _build_simkl_payload(
    watchlist_item: WatchlistItem,
    media_item: MediaItem,
) -> dict[str, Any] | None:
    ids = collect_external_ids(media_item.imdb_id, media_item.tmdb_id, media_item.tvdb_id)
    raw = media_item.raw if isinstance(media_item.raw, dict) else {}
    simkl_id = raw.get("simkl_id")
    if simkl_id:
        ids["simkl"] = simkl_id
    if not ids:
        return None
    payload = _base_watchlist_payload(watchlist_item, media_item)
    payload["simkl_id"] = simkl_id
    if watchlist_item.type == "movie":
        payload["movie_ids"] = ids
    elif watchlist_item.type in {"tv", "anime"}:
        payload["show_ids"] = ids
    else:
        return None
    return payload


def _build_letterboxd_payload(
    watchlist_item: WatchlistItem,
    media_item: MediaItem,
) -> dict[str, Any] | None:
    if watchlist_item.type not in {"movie", "anime"}:
        return None
    raw = media_item.raw if isinstance(media_item.raw, dict) else {}
    payload = _base_watchlist_payload(watchlist_item, media_item)
    payload["letterboxd_film_id"] = raw.get("letterboxd_film_id")
    if not payload["imdb_id"] and not payload["tmdb_id"] and not payload["letterboxd_film_id"]:
        return None
    return payload


def _build_publicmetadb_payload(
    watchlist_item: WatchlistItem,
    media_item: MediaItem,
) -> dict[str, Any] | None:
    tmdb_id = media_item.tmdb_id
    if not tmdb_id:
        return None
    payload = _base_watchlist_payload(watchlist_item, media_item)
    payload["tmdb_id"] = tmdb_id
    if watchlist_item.type == "tv":
        payload["media_type"] = "tv"
    elif watchlist_item.type in {"movie", "anime"}:
        payload["media_type"] = "movie"
    else:
        return None
    return payload


async def _enqueue_trakt_watchlist(
    db: AsyncSession,
    watchlist_item: WatchlistItem,
    media_item: MediaItem,
) -> None:
    await _enqueue_watchlist_job(
        db,
        watchlist_item,
        media_item,
        provider="trakt",
        source_name="Trakt watchlist",
        job_type="push_watchlist",
        required_fields=has_required_trakt_fields,
        build_payload=_build_trakt_payload,
    )


async def _enqueue_trakt_watchlist_removal(
    db: AsyncSession,
    watchlist_item: WatchlistItem,
    media_item: MediaItem,
) -> None:
    await _enqueue_watchlist_job(
        db,
        watchlist_item,
        media_item,
        provider="trakt",
        source_name="Trakt watchlist",
        job_type="remove_watchlist",
        required_fields=has_required_trakt_fields,
        build_payload=_build_trakt_payload,
    )


async def _enqueue_simkl_watchlist(
    db: AsyncSession,
    watchlist_item: WatchlistItem,
    media_item: MediaItem,
) -> None:
    await _enqueue_watchlist_job(
        db,
        watchlist_item,
        media_item,
        provider="simkl",
        source_name="SIMKL watchlist",
        job_type="push_watchlist",
        required_fields=has_required_simkl_fields,
        build_payload=_build_simkl_payload,
    )


async def _enqueue_simkl_watchlist_removal(
    db: AsyncSession,
    watchlist_item: WatchlistItem,
    media_item: MediaItem,
) -> None:
    await _enqueue_watchlist_job(
        db,
        watchlist_item,
        media_item,
        provider="simkl",
        source_name="SIMKL watchlist",
        job_type="remove_watchlist",
        required_fields=has_required_simkl_fields,
        build_payload=_build_simkl_payload,
    )


async def _enqueue_publicmetadb_watchlist(
    db: AsyncSession,
    watchlist_item: WatchlistItem,
    media_item: MediaItem,
) -> None:
    await _enqueue_watchlist_job(
        db,
        watchlist_item,
        media_item,
        provider="publicmetadb",
        source_name="PublicMetaDB watchlist",
        job_type="push_watchlist",
        required_fields=has_required_publicmetadb_fields,
        build_payload=_build_publicmetadb_payload,
        sync_enabled=is_publicmetadb_sync_enabled,
    )


async def _enqueue_publicmetadb_watchlist_removal(
    db: AsyncSession,
    watchlist_item: WatchlistItem,
    media_item: MediaItem,
) -> None:
    await _enqueue_watchlist_job(
        db,
        watchlist_item,
        media_item,
        provider="publicmetadb",
        source_name="PublicMetaDB watchlist",
        job_type="remove_watchlist",
        required_fields=has_required_publicmetadb_fields,
        build_payload=_build_publicmetadb_payload,
        sync_enabled=is_publicmetadb_sync_enabled,
    )


async def _enqueue_letterboxd_watchlist(
    db: AsyncSession,
    watchlist_item: WatchlistItem,
    media_item: MediaItem,
) -> None:
    await _enqueue_watchlist_job(
        db,
        watchlist_item,
        media_item,
        provider="letterboxd",
        source_name="Letterboxd watchlist",
        job_type="push_watchlist",
        required_fields=has_required_letterboxd_fields,
        build_payload=_build_letterboxd_payload,
    )


async def _enqueue_letterboxd_watchlist_removal(
    db: AsyncSession,
    watchlist_item: WatchlistItem,
    media_item: MediaItem,
) -> None:
    await _enqueue_watchlist_job(
        db,
        watchlist_item,
        media_item,
        provider="letterboxd",
        source_name="Letterboxd watchlist",
        job_type="remove_watchlist",
        required_fields=has_required_letterboxd_fields,
        build_payload=_build_letterboxd_payload,
    )
