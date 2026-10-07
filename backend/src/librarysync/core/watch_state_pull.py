"""Complete snapshots and cheap version-gated Watch State pulls."""

from __future__ import annotations

import copy
import hashlib
from datetime import datetime, timezone
from typing import Any

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from librarysync.core.next_episode import find_next_episodes_bulk
from librarysync.core.stremio_addon import resolve_meta_id
from librarysync.core.watch_state_events import utc
from librarysync.db.models import (
    EpisodeItem,
    MediaItem,
    User,
    WatchedItem,
    WatchlistItem,
    WatchStateCatalogRevision,
    WatchStateEntry,
    WatchStateRevision,
    WatchStateSnapshot,
)


def _video_id(meta_id: str, episode: EpisodeItem) -> str:
    raw = episode.raw if isinstance(episode.raw, dict) else {}
    state = raw.get("watch_state", {})
    if isinstance(state, dict) and state.get("metaId") == meta_id and state.get("videoId"):
        return str(state["videoId"])
    if meta_id.startswith("kitsu:"):
        return f"{meta_id}:{episode.episode_number}"
    return f"{meta_id}:{episode.season_number}:{episode.episode_number}"


async def _build_complete_snapshot(db: AsyncSession, user_id: str) -> dict[str, Any]:
    state_rows = list(
        (
            await db.execute(
                select(WatchStateEntry, MediaItem)
                .outerjoin(MediaItem, WatchStateEntry.media_item_id == MediaItem.id)
                .where(
                    WatchStateEntry.user_id == user_id,
                    WatchStateEntry.category != "playback",
                )
            )
        ).all()
    )
    entries = [entry for entry, _ in state_rows]
    state_media = {entry.id: media for entry, media in state_rows}
    watched_entries = {row.episode_item_id or row.media_item_id: row for row in entries if row.category == "watched"}
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
    ratings = []
    explicit_ratings = {
        (row.media_item_id, row.episode_item_id, row.scope) for row in entries if row.category == "rating"
    }
    legacy_targets = set()
    for watched, movie, episode, series in result.all():
        media = movie or series
        meta_id = resolve_meta_id(media) if media else None
        if not meta_id or (not episode and media.media_type != "movie"):
            continue
        state = watched_entries.get(episode.id if episode else media.id)
        if state and state.payload["event"] == "unplayed" and utc(watched.watched_at) <= utc(state.occurred_at):
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
            video_id = state.video_id if state and state.video_id else _video_id(meta_id, episode)
            episodes.add(video_id)
            for alias in _aliases(media):
                counts.setdefault(alias, set()).add(video_id)
            shows.setdefault(media.id, (meta_id, at))
            row.update(
                videoId=video_id,
                season=state.season if state else episode.season_number,
                episode=state.episode if state else episode.episode_number,
            )
        else:
            movies.add(meta_id)
        recent.setdefault(video_id, row)

        rating_target = (media.id, episode.id if episode else None, "episode" if episode else "movie")
        if rating_target not in legacy_targets and rating_target not in explicit_ratings:
            legacy_targets.add(rating_target)
            if watched.rating is not None:
                rated = {"type": row["type"], "metaId": meta_id, "rating": watched.rating * 2, "at": at}
                if episode:
                    rated["videoId"] = video_id
                    rated["season"] = row["season"]
                    rated["episode"] = row["episode"]
                ratings.append(rated)

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
        "nextUp": next_up,
    }
    for entry in entries:
        body = entry.payload
        if entry.category == "watched" and entry.episode_item_id is None and entry.scope == "episode":
            if body["event"] != "unplayed":
                episodes.add(entry.video_id)
                aliases = _aliases(state_media[entry.id]) if state_media[entry.id] else {entry.meta_id}
                for alias in aliases:
                    counts.setdefault(alias, set()).add(entry.video_id)
        elif entry.category == "watched" and entry.media_item_id is None and entry.scope == "movie":
            if body["event"] != "unplayed":
                movies.add(entry.meta_id)
    watched_state["movies"] = sorted(movies)
    watched_state["episodes"] = sorted(episodes)
    watched_state["counts"] = {key: {"watched": len(value), "total": 0} for key, value in sorted(counts.items())}
    watchlist_rows = list(
        (
            await db.execute(
                select(WatchlistItem, MediaItem)
                .join(
                    MediaItem,
                    MediaItem.id == WatchlistItem.media_item_id,
                )
                .where(WatchlistItem.user_id == user_id)
            )
        ).all()
    )
    dropped = set()
    watchlist = []
    for item, media in watchlist_rows:
        meta_id = resolve_meta_id(media)
        if not meta_id:
            continue
        if item.status == "dropped":
            dropped.update(_aliases(media))
        elif item.status not in {"removed", "hidden"}:
            watchlist.append(
                {
                    "type": "movie" if media.media_type == "movie" else "series",
                    "metaId": meta_id,
                    "at": int(utc(item.created_at).timestamp()),
                }
            )
    watched_state["dropped"] = sorted(dropped)
    watched_state["nextUp"] = [row for row in next_up if row["metaId"] not in dropped]
    for entry in entries:
        if entry.category != "rating" or entry.payload["event"] == "unrated":
            continue
        row = {
            "type": entry.media_type,
            "metaId": entry.meta_id,
            "rating": entry.payload["rating"],
            "at": int(utc(entry.occurred_at).timestamp()),
        }
        if entry.scope == "episode":
            row["videoId"] = entry.video_id
            row["season"] = entry.season
            row["episode"] = entry.episode
        elif entry.scope == "season":
            row["season"] = entry.season
        ratings.append(row)
    ratings.sort(
        key=lambda row: (
            row["type"],
            row["metaId"],
            row["season"] if row.get("season") is not None else -1,
            row.get("videoId", ""),
        )
    )
    watchlist.sort(key=lambda row: (-row["at"], row["metaId"]))
    return {"items": list(recent.values())[:100], "watched": watched_state, "watchlist": watchlist, "ratings": ratings}


def _aliases(media: MediaItem) -> set[str]:
    ids = {resolve_meta_id(media)}
    if media.imdb_id:
        ids.add(media.imdb_id)
    for prefix, field in (
        ("tmdb", "tmdb_id"),
        ("tvdb", "tvdb_id"),
        ("kitsu", "kitsu_id"),
        ("mal", "myanimelist_id"),
        ("anilist", "anilist_id"),
    ):
        value = getattr(media, field)
        if value:
            ids.add(f"{prefix}:{value}")
    return ids - {None}


async def _version(db: AsyncSession, user_id: str) -> str:
    revision = await db.scalar(select(WatchStateRevision.revision).where(WatchStateRevision.user_id == user_id)) or 0
    catalog = await db.scalar(select(WatchStateCatalogRevision.revision).where(WatchStateCatalogRevision.id == 1)) or 0
    # Date-based episode availability changes even when no row is written.
    today = datetime.now(timezone.utc).date().isoformat()
    return hashlib.sha256(f"v2|{user_id}|{revision}|{catalog}|{today}".encode()).hexdigest()


async def build_watch_state(
    db: AsyncSession, user_id: str, since: str | None, *, mark_pulled: bool = True
) -> dict[str, Any]:
    await db.flush()
    version = await _version(db, user_id)
    snapshot = await db.scalar(
        select(WatchStateSnapshot)
        .where(WatchStateSnapshot.user_id == user_id)
        .execution_options(populate_existing=True)
    )
    if snapshot is None or snapshot.version != version:
        # Cross-process serialization of snapshot builders. Provider imports can still write,
        # so check the revision around the complete read rather than publishing a partial set.
        await db.scalar(select(User.id).where(User.id == user_id).with_for_update())
        # Another process may have populated the cache while this reader waited.
        snapshot = await db.scalar(
            select(WatchStateSnapshot)
            .where(WatchStateSnapshot.user_id == user_id)
            .execution_options(populate_existing=True)
        )
        for _ in range(3):
            version = await _version(db, user_id)
            payload = await _build_complete_snapshot(db, user_id)
            if await _version(db, user_id) == version:
                break
        else:
            raise HTTPException(503, "Watch state changed while building its snapshot")
        if snapshot is None:
            snapshot = WatchStateSnapshot(user_id=user_id)
            db.add(snapshot)
        snapshot.version = version
        snapshot.payload = payload
        snapshot.built_at = datetime.now(timezone.utc)
        await db.flush()
    payload = copy.deepcopy(snapshot.payload)
    items = {row["videoId"]: row for row in payload["items"]}
    progress = list(
        (
            await db.scalars(
                select(WatchStateEntry)
                .where(
                    WatchStateEntry.user_id == user_id,
                    WatchStateEntry.category == "playback",
                )
                .order_by(WatchStateEntry.occurred_at.desc(), WatchStateEntry.id)
                .limit(100)
            )
        ).all()
    )
    for entry in progress:
        body = entry.payload
        video_id = entry.video_id or entry.meta_id
        items.pop(video_id, None)
        if body["event"] in {"start", "unplayed"}:
            continue
        played = body["event"] == "played" or (body["event"] == "stop" and body.get("played", False))
        position = body.get("positionMs")
        if not played and not position:
            continue
        row = {
            "type": entry.media_type,
            "metaId": entry.meta_id,
            "videoId": video_id,
            "played": played,
            "at": int(utc(entry.occurred_at).timestamp()),
        }
        if entry.scope == "episode":
            row.update(season=entry.season, episode=entry.episode)
        if position is not None:
            row["positionMs"] = position
        duration = body.get("durationMs")
        if duration:
            row["durationMs"] = duration
            if position is not None:
                row["progressPercent"] = min(100, round(position / duration * 100, 4))
        items[video_id] = row
    response = {"version": version, "items": sorted(items.values(), key=lambda r: (-r["at"], r["videoId"]))[:100]}
    if since != version:
        response.update({key: payload[key] for key in ("watched", "watchlist", "ratings")})
    if mark_pulled:
        snapshot.last_pulled_at = datetime.now(timezone.utc)
    return response
