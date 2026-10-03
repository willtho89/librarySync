"""AIOStreams v2 watch history. Transactions are owned by the addon routes."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from typing import Any

from fastapi import HTTPException
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from librarysync.core.next_episode import find_next_episodes_bulk
from librarysync.core.stremio_addon import resolve_meta_id
from librarysync.core.watch_pipeline import (
    SYNC_COORDINATOR,
    enqueue_new_item_job,
    enqueue_watchlist_update_job,
)
from librarysync.core.watchlist import apply_media_id_update
from librarysync.db.models import EpisodeItem, MediaItem, WatchedItem, WatchEvent

EVENT_TYPE = "aiostreams_watch_state"
ID_FIELDS = {
    "imdb": "imdb_id",
    "tmdb": "tmdb_id",
    "tvdb": "tvdb_id",
    "tvmaze": "tvmaze_id",
    "kitsu": "kitsu_id",
    "mal": "myanimelist_id",
    "anilist": "anilist_id",
}


def _media_ids(payload: dict) -> dict[str, str]:
    ids = {
        field: str(payload["ids"][provider]).strip()
        for provider, field in ID_FIELDS.items()
        if payload.get("ids", {}).get(provider)
    }
    meta_id = payload["metaId"]
    meta_field = None
    meta_value = None
    if meta_id.startswith("tt"):
        meta_field, meta_value = "imdb_id", meta_id
    elif ":" in meta_id:
        prefix, value = meta_id.split(":", 1)
        if prefix in ID_FIELDS and value.isdigit():
            meta_field, meta_value = ID_FIELDS[prefix], value
    if meta_field:
        if len(meta_value) > 32:
            raise HTTPException(422, "Provider identifier exceeds 32 characters")
        if ids.get(meta_field, meta_value) != meta_value:
            raise HTTPException(409, "Watch event identifiers do not match the meta id")
        ids[meta_field] = meta_value
    return ids


async def _resolve_media(db: AsyncSession, media_type: str, payload: dict) -> MediaItem | None:
    ids = _media_ids(payload)
    clauses = [getattr(MediaItem, field) == value for field, value in ids.items()]
    clauses.extend(
        [
            MediaItem.raw["stremio_id"].as_string() == payload["metaId"],
            MediaItem.raw["stremio"]["id"].as_string() == payload["metaId"],
            MediaItem.raw["stremio"]["_id"].as_string() == payload["metaId"],
        ]
    )
    types = ["movie"] if media_type == "movie" else ["tv", "anime", "series"]
    result = await db.execute(select(MediaItem).where(MediaItem.media_type.in_(types), or_(*clauses)))
    matches = list(result.scalars().all())
    if len(matches) > 1:
        raise HTTPException(409, "Watch event identifiers refer to different titles")
    if matches:
        media = matches[0]
        # Keep identifiers learned from the client available to downstream sync.
        for field, value in ids.items():
            await apply_media_id_update(db, media, field, value)
        return media
    if payload["event"] == "unplayed":
        return None
    media = MediaItem(
        media_type="movie" if media_type == "movie" else "tv",
        title=payload["metaId"],
        raw={"stremio_id": payload["metaId"], "source": EVENT_TYPE},
        **ids,
    )
    db.add(media)
    await db.flush()
    return media


async def record_watch_state(db: AsyncSession, user_id: str, media_type: str, payload: dict) -> None:
    # The event id is stable across retries, including clients reporting one stop twice.
    entry_key = hashlib.sha256(payload["id"].encode()).hexdigest()
    existing = await db.scalar(
        select(WatchEvent.id).where(
            WatchEvent.user_id == user_id,
            WatchEvent.event_type == EVENT_TYPE,
            WatchEvent.entry_key == entry_key,
        )
    )
    if existing or (payload["event"] == "stop" and not payload.get("played", False)):
        return

    at = datetime.fromtimestamp(payload["at"], timezone.utc)
    media = await _resolve_media(db, media_type, payload)
    if media is None:
        return
    episode = None
    if media_type == "series":
        episode = await db.scalar(
            select(EpisodeItem).where(
                EpisodeItem.show_media_item_id == media.id,
                EpisodeItem.season_number == payload["season"],
                EpisodeItem.episode_number == payload["episode"],
            )
        )
        if episode is None:
            if payload["event"] == "unplayed":
                return
            episode = EpisodeItem(
                show_media_item_id=media.id,
                season_number=payload["season"],
                episode_number=payload["episode"],
            )
            db.add(episode)
            await db.flush()

    target = WatchedItem.episode_item_id == episode.id if episode else WatchedItem.media_item_id == media.id
    event_target = WatchEvent.episode_item_id == episode.id if episode else WatchEvent.media_item_id == media.id
    latest = await db.scalar(
        select(WatchEvent.occurred_at)
        .where(
            WatchEvent.user_id == user_id,
            WatchEvent.event_type == EVENT_TYPE,
            event_target,
        )
        .order_by(WatchEvent.occurred_at.desc())
        .limit(1)
    )
    db.add(
        WatchEvent(
            user_id=user_id,
            media_item_id=None if episode else media.id,
            episode_item_id=episode.id if episode else None,
            event_type=EVENT_TYPE,
            entry_key=entry_key,
            occurred_at=at,
            raw=payload,
        )
    )
    # Delayed deliveries must not undo a newer mark from the same tracker.
    if latest and latest.replace(tzinfo=timezone.utc) > at:
        return

    if episode:
        episode.raw = {
            **(episode.raw or {}),
            "watch_state": {"metaId": payload["metaId"], "videoId": payload["videoId"]},
        }

    result = await db.execute(select(WatchedItem).where(WatchedItem.user_id == user_id, target))
    watches = list(result.scalars().all())
    if payload["event"] == "unplayed":
        for watched in watches:
            if watched.watched_at.replace(tzinfo=timezone.utc) > at:
                continue
            await SYNC_COORDINATOR.enqueue_delete_all(db, watched, media, episode)
            await db.delete(watched)
        await enqueue_watchlist_update_job(db, user_id, media.id)
    else:
        # Mark-played sets a flag; it does not create a rewatch on an already watched title.
        if watches and (
            payload["event"] == "played"
            or any(watched.watched_at.replace(tzinfo=timezone.utc) == at for watched in watches)
        ):
            return
        watched = WatchedItem(
            user_id=user_id,
            media_item_id=None if episode else media.id,
            episode_item_id=episode.id if episode else None,
            watched_at=at,
            source="aiostreams",
        )
        db.add(watched)
        await db.flush()
        await enqueue_new_item_job(db, user_id, watched.id, is_rewatch=bool(watches), source="aiostreams")


def _video_id(meta_id: str, episode: EpisodeItem) -> str:
    raw = episode.raw if isinstance(episode.raw, dict) else {}
    state = raw.get("watch_state", {})
    if isinstance(state, dict) and state.get("metaId") == meta_id and state.get("videoId"):
        return str(state["videoId"])
    if meta_id.startswith("kitsu:"):
        return f"{meta_id}:{episode.episode_number}"
    return f"{meta_id}:{episode.season_number}:{episode.episode_number}"


async def build_watch_state(db: AsyncSession, user_id: str, since: str | None) -> dict[str, Any]:
    show = aliased(MediaItem)
    result = await db.execute(
        select(WatchedItem, MediaItem, EpisodeItem, show)
        .outerjoin(MediaItem, WatchedItem.media_item_id == MediaItem.id)
        .outerjoin(EpisodeItem, WatchedItem.episode_item_id == EpisodeItem.id)
        .outerjoin(show, EpisodeItem.show_media_item_id == show.id)
        .where(WatchedItem.user_id == user_id)
        .order_by(WatchedItem.watched_at.desc(), WatchedItem.id)
    )
    movies: set[str] = set()
    episodes: set[str] = set()
    counts: dict[str, set[str]] = {}
    recent: dict[str, dict] = {}
    shows: dict[str, tuple[str, int]] = {}
    for watched, movie, episode, series in result.all():
        media = movie or series
        meta_id = resolve_meta_id(media) if media else None
        if not meta_id or (not episode and media.media_type != "movie"):
            continue
        at = int(watched.watched_at.replace(tzinfo=timezone.utc).timestamp())
        row = {
            "type": "series" if episode else "movie",
            "metaId": meta_id,
            "videoId": meta_id,
            "played": True,
            "at": at,
        }
        video_id = meta_id
        if episode:
            video_id = _video_id(meta_id, episode)
            episodes.add(video_id)
            counts.setdefault(meta_id, set()).add(video_id)
            shows.setdefault(media.id, (meta_id, at))
            row.update(videoId=video_id, season=episode.season_number, episode=episode.episode_number)
        else:
            movies.add(meta_id)
        recent.setdefault(video_id, row)

    next_episodes = await find_next_episodes_bulk(db, user_id, list(shows))
    next_up = []
    for show_id, episode in next_episodes.items():
        meta_id, at = shows[show_id]
        next_up.append(
            {
                "type": "series",
                "metaId": meta_id,
                "videoId": _video_id(meta_id, episode),
                "season": episode.season_number,
                "episode": episode.episode_number,
                "at": at,
            }
        )
    watched_state = {
        "movies": sorted(movies),
        "episodes": sorted(episodes),
        "counts": {key: {"watched": len(value), "total": 0} for key, value in sorted(counts.items())},
        "nextUp": next_up,
    }
    items = list(recent.values())[:100]
    version = hashlib.sha256(
        json.dumps(
            {"items": items, "watched": watched_state},
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
    ).hexdigest()
    response = {"version": version, "items": items}
    if since != version:
        response["watched"] = watched_state
    return response
