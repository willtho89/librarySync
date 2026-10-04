"""Independent ratings fan out through the existing per-provider outbox."""

import math
from datetime import timezone

from sqlalchemy import select

from librarysync.core.watch_pipeline import collect_external_ids, enqueue_outbox_job
from librarysync.db.models import Integration

RATING_SCOPES = {
    "trakt": {"movie", "series", "season", "episode"},
    "simkl": {"movie", "series"},
    "publicmetadb": {"movie", "series", "episode"},
    "anilist": {"series"},
}


async def enqueue_rating_delivery(db, entry, media, episode):
    integrations = list(
        (
            await db.scalars(
                select(Integration).where(
                    Integration.user_id == entry.user_id,
                    Integration.status == "connected",
                    Integration.provider.in_([*RATING_SCOPES, "stremio", "letterboxd"]),
                )
            )
        ).all()
    )
    ids = collect_external_ids(media.imdb_id, media.tmdb_id, media.tvdb_id) if media else {}
    clear = entry.payload["event"] == "unrated"
    value = entry.payload.get("rating")
    for integration in integrations:
        provider = integration.provider
        supported = entry.scope in RATING_SCOPES.get(provider, set())
        # Explicit conversion at the boundary: integer ten-point providers round halves up.
        rounded = max(1, min(10, math.floor(value + 0.5))) if value is not None else None
        payload = {
            "state_entry_id": entry.id,
            "rating_scope": entry.scope,
            "media_item_id": media.id if media else None,
            "media_type": "movie" if entry.scope == "movie" else "tv",
            "rating": rounded / 2 if rounded is not None else None,
            "season_number": episode.season_number if episode else entry.season,
            "episode_number": episode.episode_number if episode else entry.episode,
            "movie_ids" if entry.scope == "movie" else "show_ids": ids,
            "tmdb_id": media.tmdb_id if media else None,
            "anilist_id": media.anilist_id if media else None,
            "protocol_at": int(entry.occurred_at.replace(tzinfo=timezone.utc).timestamp()),
            "protocol_event": entry.payload["event"],
            "protocol_rating": value,
        }
        if episode:
            payload["episode_ids"] = collect_external_ids(episode.imdb_id, episode.tmdb_id, episode.tvdb_id)
        if provider == "anilist":
            supported = supported and bool(payload["anilist_id"])
        if entry.scope == "episode" and episode is None:
            supported = False
        job = await enqueue_outbox_job(
            db,
            user_id=entry.user_id,
            target_provider=provider,
            job_type="remove_rating" if clear else "push_rating",
            payload=payload,
            status="pending" if supported else "failed_permanent",
        )
        if not supported:
            job.last_error = "Rating scope or canonical mapping is unsupported by this provider"
