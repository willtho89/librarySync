from __future__ import annotations

from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from librarysync.connectors.services.letterboxd import has_required_letterboxd_fields
from librarysync.connectors.services.trakt import has_required_trakt_fields
from librarysync.core.integrations import load_integration_with_secrets
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
    await _enqueue_letterboxd_watchlist(db, watchlist_item, media_item)


async def enqueue_personal_watchlist_removal(
    db: AsyncSession,
    watchlist_item: WatchlistItem,
    media_item: MediaItem | None,
) -> None:
    if not media_item:
        return
    await _enqueue_trakt_watchlist_removal(db, watchlist_item, media_item)
    await _enqueue_letterboxd_watchlist_removal(db, watchlist_item, media_item)


async def _enqueue_trakt_watchlist(
    db: AsyncSession,
    watchlist_item: WatchlistItem,
    media_item: MediaItem,
) -> None:
    integration, secret_data = await load_integration_with_secrets(
        db, watchlist_item.user_id, "trakt"
    )
    if not integration or integration.status == "disconnected" or not secret_data:
        return
    if not has_required_trakt_fields(secret_data):
        return

    source = await ensure_personal_watchlist_source(
        db,
        user_id=watchlist_item.user_id,
        provider="trakt",
        name="Trakt watchlist",
    )
    if not source.is_enabled:
        return

    ids = collect_external_ids(media_item.imdb_id, media_item.tmdb_id, media_item.tvdb_id)
    if not ids:
        return

    payload: dict[str, Any] = {
        "watchlist_item_id": watchlist_item.id,
        "media_item_id": media_item.id,
        "media_type": watchlist_item.type,
        "imdb_id": media_item.imdb_id,
        "tmdb_id": media_item.tmdb_id,
        "tvdb_id": media_item.tvdb_id,
    }
    if watchlist_item.type in {"movie", "anime"}:
        payload["movie_ids"] = ids
    elif watchlist_item.type == "tv":
        payload["show_ids"] = ids
    else:
        return

    await enqueue_outbox_job(
        db,
        user_id=watchlist_item.user_id,
        target_provider="trakt",
        job_type="push_watchlist",
        payload=payload,
        status="pending",
    )


async def _enqueue_trakt_watchlist_removal(
    db: AsyncSession,
    watchlist_item: WatchlistItem,
    media_item: MediaItem,
) -> None:
    integration, secret_data = await load_integration_with_secrets(
        db, watchlist_item.user_id, "trakt"
    )
    if not integration or integration.status == "disconnected" or not secret_data:
        return
    if not has_required_trakt_fields(secret_data):
        return

    source = await ensure_personal_watchlist_source(
        db,
        user_id=watchlist_item.user_id,
        provider="trakt",
        name="Trakt watchlist",
    )
    if not source.is_enabled:
        return

    ids = collect_external_ids(media_item.imdb_id, media_item.tmdb_id, media_item.tvdb_id)
    if not ids:
        return

    payload: dict[str, Any] = {
        "watchlist_item_id": watchlist_item.id,
        "media_item_id": media_item.id,
        "media_type": watchlist_item.type,
        "imdb_id": media_item.imdb_id,
        "tmdb_id": media_item.tmdb_id,
        "tvdb_id": media_item.tvdb_id,
    }
    if watchlist_item.type in {"movie", "anime"}:
        payload["movie_ids"] = ids
    elif watchlist_item.type == "tv":
        payload["show_ids"] = ids
    else:
        return

    await enqueue_outbox_job(
        db,
        user_id=watchlist_item.user_id,
        target_provider="trakt",
        job_type="remove_watchlist",
        payload=payload,
        status="pending",
    )


async def _enqueue_letterboxd_watchlist(
    db: AsyncSession,
    watchlist_item: WatchlistItem,
    media_item: MediaItem,
) -> None:
    if watchlist_item.type not in {"movie", "anime"}:
        return

    integration, secret_data = await load_integration_with_secrets(
        db, watchlist_item.user_id, "letterboxd"
    )
    if not integration or integration.status == "disconnected" or not secret_data:
        return
    if not has_required_letterboxd_fields(secret_data):
        return

    source = await ensure_personal_watchlist_source(
        db,
        user_id=watchlist_item.user_id,
        provider="letterboxd",
        name="Letterboxd watchlist",
    )
    if not source.is_enabled:
        return

    raw = media_item.raw if isinstance(media_item.raw, dict) else {}
    payload: dict[str, Any] = {
        "watchlist_item_id": watchlist_item.id,
        "media_item_id": media_item.id,
        "media_type": watchlist_item.type,
        "imdb_id": media_item.imdb_id,
        "tmdb_id": media_item.tmdb_id,
        "letterboxd_film_id": raw.get("letterboxd_film_id"),
    }
    if not payload["imdb_id"] and not payload["tmdb_id"] and not payload["letterboxd_film_id"]:
        return

    await enqueue_outbox_job(
        db,
        user_id=watchlist_item.user_id,
        target_provider="letterboxd",
        job_type="push_watchlist",
        payload=payload,
        status="pending",
    )


async def _enqueue_letterboxd_watchlist_removal(
    db: AsyncSession,
    watchlist_item: WatchlistItem,
    media_item: MediaItem,
) -> None:
    if watchlist_item.type not in {"movie", "anime"}:
        return

    integration, secret_data = await load_integration_with_secrets(
        db, watchlist_item.user_id, "letterboxd"
    )
    if not integration or integration.status == "disconnected" or not secret_data:
        return
    if not has_required_letterboxd_fields(secret_data):
        return

    source = await ensure_personal_watchlist_source(
        db,
        user_id=watchlist_item.user_id,
        provider="letterboxd",
        name="Letterboxd watchlist",
    )
    if not source.is_enabled:
        return

    raw = media_item.raw if isinstance(media_item.raw, dict) else {}
    payload: dict[str, Any] = {
        "watchlist_item_id": watchlist_item.id,
        "media_item_id": media_item.id,
        "media_type": watchlist_item.type,
        "imdb_id": media_item.imdb_id,
        "tmdb_id": media_item.tmdb_id,
        "letterboxd_film_id": raw.get("letterboxd_film_id"),
    }
    if not payload["imdb_id"] and not payload["tmdb_id"] and not payload["letterboxd_film_id"]:
        return

    await enqueue_outbox_job(
        db,
        user_id=watchlist_item.user_id,
        target_provider="letterboxd",
        job_type="remove_watchlist",
        payload=payload,
        status="pending",
    )
