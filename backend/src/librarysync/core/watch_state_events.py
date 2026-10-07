"""Durable ingestion and local Watch State transitions. No network calls here."""

from __future__ import annotations

import hashlib
from datetime import datetime, timezone

from fastapi import HTTPException
from sqlalchemy import delete, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from librarysync.core.watch_pipeline import SYNC_COORDINATOR, enqueue_new_item_job, enqueue_watchlist_update_job
from librarysync.core.watch_state_identity import EVENT_TYPE, _media_ids, _resolve_media
from librarysync.core.watchlist_sources import ensure_watchlist_source, upsert_watchlist_source_item
from librarysync.core.watchlist_sync import enqueue_personal_watchlist_removal, enqueue_personal_watchlist_sync
from librarysync.db.models import (
    EpisodeItem,
    MediaItem,
    WatchedItem,
    WatchEvent,
    WatchlistItem,
    WatchlistSourceItem,
    WatchStateEntry,
    WatchStateReceipt,
)

WATCH_EVENTS = {"stop", "played", "unplayed"}


def utc(value: datetime) -> datetime:
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


def digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


async def receive_watch_state(
    db: AsyncSession,
    addon_id: str,
    viewer: str,
    user_id: str,
    media_type: str,
    payload: dict,
) -> WatchStateReceipt:
    _media_ids(payload)  # Reject internally contradictory ids before accepting a receipt.
    # AIOStreams keys playback by position and can coalesce the start. The
    # immutable event timestamp distinguishes sessions; retries retain it.
    key = payload["id"]
    if payload["event"] in {"start", "pause", "stop"}:
        key += "|" + str(payload["at"])
    receipt = await db.scalar(
        select(WatchStateReceipt).where(
            WatchStateReceipt.addon_id == addon_id,
            WatchStateReceipt.viewer == viewer,
            WatchStateReceipt.entry_key == digest(key),
        )
    )
    if receipt:
        receipt.duplicates += 1
        return receipt
    receipt = WatchStateReceipt(
        addon_id=addon_id,
        viewer=viewer,
        user_id=user_id,
        entry_key=digest(key),
        event=payload["event"],
        media_type=media_type,
        payload=payload,
    )
    db.add(receipt)
    await db.flush()
    if not payload.get("videos"):
        await process_receipt(db, receipt, strict=True)
    return receipt


async def process_receipt(db: AsyncSession, receipt: WatchStateReceipt, *, strict: bool = False) -> None:
    payload = receipt.payload
    videos = payload.get("videos")
    previous = {row["videoId"]: row for row in receipt.outcomes or []}
    outcomes = []
    for video in videos or [None]:
        body = {**payload, **video, "scope": "episode"} if video else payload
        video_id = body.get("videoId") or body["metaId"]
        mapping = payload.get("_mappings", {}).get(video_id)
        if mapping:
            body = {**body, **mapping}
        if previous.get(video_id, {}).get("status") in {"applied", "superseded"}:
            outcomes.append(previous[video_id])
            continue
        try:
            async with db.begin_nested():
                status = await apply_event(db, receipt.user_id, receipt.media_type, body)
                await db.flush()
            outcome = {"videoId": video_id, "status": status}
            if status == "unresolved":
                outcome["reason"] = "Original state retained; map the absolute episode before downstream delivery"
            outcomes.append(outcome)
        except HTTPException as exc:
            if strict and not videos and exc.status_code in {409, 422}:
                raise
            outcomes.append({"videoId": video_id, "status": "unresolved", "reason": str(exc.detail)})
        except IntegrityError:
            # A concurrent user may have created the same catalog item. The inbox survives;
            # retry reuses the committed item instead of acknowledging lost history.
            outcomes.append({"videoId": video_id, "status": "pending", "reason": "Concurrent catalog update"})
    receipt.outcomes = outcomes
    statuses = {row["status"] for row in outcomes}
    if "pending" in statuses:
        receipt.status = "pending"
    elif "unresolved" in statuses:
        receipt.status = "unresolved"
    elif statuses == {"superseded"}:
        receipt.status = "superseded"
    else:
        receipt.status = "applied"
    receipt.error = next((row.get("reason") for row in outcomes if row.get("reason")), None)
    receipt.processed_at = datetime.now(timezone.utc)


async def _episode(db: AsyncSession, media: MediaItem | None, body: dict) -> EpisodeItem | None:
    if body.get("_canonical_episode_item_id"):
        episode = await db.get(EpisodeItem, body["_canonical_episode_item_id"])
        if not episode or not media or episode.show_media_item_id != media.id:
            raise HTTPException(409, "Episode mapping no longer belongs to this title")
        return episode
    if not media or body["scope"] != "episode" or body.get("season") is None:
        return None
    episode = await db.scalar(
        select(EpisodeItem).where(
            EpisodeItem.show_media_item_id == media.id,
            EpisodeItem.season_number == body["season"],
            EpisodeItem.episode_number == body["episode"],
        )
    )
    if episode is None and body["event"] not in {"unplayed", "unrated"}:
        episode = EpisodeItem(
            show_media_item_id=media.id,
            season_number=body["season"],
            episode_number=body["episode"],
        )
        db.add(episode)
        await db.flush()
    return episode


def target_key(media: MediaItem | None, body: dict) -> str:
    identity = media.id if media else body["metaId"]
    if body["scope"] == "episode":
        identity += f"|{body.get('season')}|{body['episode']}"
        if body.get("season") is None:
            identity += f"|{body['videoId']}"
    elif body["scope"] == "season":
        identity += f"|{body['season']}"
    return digest(f"{body['scope']}|{identity}")


async def _entry(db, user_id, category, key):
    return await db.scalar(
        select(WatchStateEntry).where(
            WatchStateEntry.user_id == user_id,
            WatchStateEntry.category == category,
            WatchStateEntry.target_key == key,
        )
    )


async def _write_entry(db, user_id, category, media, episode, body):
    key = target_key(media, body)
    entry = await _entry(db, user_id, category, key)
    if entry is None:
        # A clear can arrive before the catalog item exists. Preserve its timestamp
        # when the same external target subsequently acquires a canonical id.
        entry = await db.scalar(
            select(WatchStateEntry)
            .where(
                WatchStateEntry.user_id == user_id,
                WatchStateEntry.category == category,
                WatchStateEntry.meta_id == body["metaId"],
                WatchStateEntry.scope == body["scope"],
                WatchStateEntry.season == body.get("season"),
                WatchStateEntry.episode == body.get("episode"),
            )
            .order_by(WatchStateEntry.occurred_at.desc())
            .limit(1)
        )
    at = datetime.fromtimestamp(body["at"], timezone.utc)
    if entry and utc(entry.occurred_at) > at:
        return None
    if entry is None:
        entry = WatchStateEntry(user_id=user_id, category=category, target_key=key)
        db.add(entry)
    entry.target_key = key
    entry.media_type = "movie" if body["scope"] == "movie" else "series"
    entry.scope = body["scope"]
    entry.media_item_id = media.id if media else None
    entry.episode_item_id = episode.id if episode else None
    entry.meta_id = body["metaId"]
    entry.video_id = body.get("videoId")
    entry.season = body.get("season")
    entry.episode = body.get("episode")
    entry.occurred_at = at
    entry.payload = body
    await db.flush()
    return entry


async def apply_event(db: AsyncSession, user_id: str, media_type: str, body: dict) -> str:
    if body["event"] == "stop" and not body.get("played") and body.get("positionMs") is None:
        return "applied"
    if body["scope"] == "episode" and not body.get("_canonical_episode_item_id"):
        known = await db.scalar(
            select(WatchStateEntry)
            .where(
                WatchStateEntry.user_id == user_id,
                WatchStateEntry.scope == "episode",
                WatchStateEntry.meta_id == body["metaId"],
                WatchStateEntry.video_id == body.get("videoId"),
                WatchStateEntry.payload["_canonical_episode_item_id"].as_string().is_not(None),
            )
            .order_by(WatchStateEntry.occurred_at.desc())
            .limit(1)
        )
        if known:
            body = {
                **body,
                "_canonical_episode_item_id": known.payload["_canonical_episode_item_id"],
                "_canonical_media_item_id": known.payload["_canonical_media_item_id"],
            }
    media = await _resolve_media(db, media_type, body)
    episode = await _episode(db, media, body)
    name = body["event"]
    at = datetime.fromtimestamp(body["at"], timezone.utc)
    if name in {"watchlisted", "unwatchlisted"}:
        category = "watchlist"
    elif name in {"dropped", "undropped"}:
        category = "drop"
    elif name in {"rated", "unrated"}:
        category = "rating"
    elif name in {"played", "unplayed"} or (name == "stop" and body.get("played")):
        category = "watched"
    else:
        category = "playback"
    entry = await _write_entry(db, user_id, category, media, episode, body)
    if entry is None:
        return "superseded"
    if category == "watched":
        await _write_entry(db, user_id, "playback", media, episode, body)
        await _apply_history(db, user_id, media, episode, body, at)
    elif category in {"watchlist", "drop"} and media:
        await _apply_watchlist(db, user_id, media, body, at)
    elif category == "rating":
        # Provider delivery is independent of watches, including ratings of unwatched titles.
        from librarysync.core.watch_state_ratings import enqueue_rating_delivery

        await enqueue_rating_delivery(db, entry, media, episode)
    resumes_show = name in {"start", "played"} or (name == "stop" and body.get("played"))
    if media_type == "series" and resumes_show:
        show_body = {**body, "scope": "series", "event": "undropped", "videoId": None, "season": None, "episode": None}
        dropped = await _entry(db, user_id, "drop", target_key(media, show_body))
        if dropped and dropped.payload["event"] == "dropped" and utc(dropped.occurred_at) <= at:
            await _write_entry(db, user_id, "drop", media, None, show_body)
            if media:
                await _apply_watchlist(db, user_id, media, show_body, at)
    if body["scope"] == "episode" and episode is None and name in {"played", "stop", "rated"}:
        # State round-trips under its original ids. Canonical downstream delivery awaits a real mapping.
        if body.get("season") is None:
            return "unresolved"
    return "applied"


async def _apply_history(db, user_id, media, episode, body, at):
    if media is None or (body["scope"] == "episode" and episode is None):
        return
    target = WatchedItem.episode_item_id == episode.id if episode else WatchedItem.media_item_id == media.id
    watches = list((await db.scalars(select(WatchedItem).where(WatchedItem.user_id == user_id, target))).all())
    # Audit completed watches and clears using the established history event model.
    audit_key = digest(body["id"] + (body.get("videoId") or "") + "|" + str(body["at"]))
    audit_exists = await db.scalar(
        select(WatchEvent.id).where(
            WatchEvent.user_id == user_id,
            WatchEvent.event_type == EVENT_TYPE,
            WatchEvent.entry_key == audit_key,
        )
    )
    if not audit_exists:
        db.add(
            WatchEvent(
                user_id=user_id,
                media_item_id=None if episode else media.id,
                episode_item_id=episode.id if episode else None,
                event_type=EVENT_TYPE,
                entry_key=audit_key,
                occurred_at=at,
                raw=body,
            )
        )
    if body["event"] == "unplayed":
        for watched in watches:
            if utc(watched.watched_at) <= at:
                await SYNC_COORDINATOR.enqueue_delete_all(db, watched, media, episode)
                await db.delete(watched)
        await enqueue_watchlist_update_job(db, user_id, media.id)
    elif not watches or (body["event"] != "played" and not any(utc(w.watched_at) == at for w in watches)):
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


async def _apply_watchlist(db, user_id, media, body, at):
    name = body["event"]
    item = await db.scalar(
        select(WatchlistItem).where(
            WatchlistItem.user_id == user_id,
            WatchlistItem.media_item_id == media.id,
        )
    )
    if item is None:
        if name in {"unwatchlisted", "undropped"}:
            return
        item = WatchlistItem(
            user_id=user_id,
            media_item_id=media.id,
            type=media.media_type,
            source="aiostreams",
            status="added",
            created_at=at,
            updated_at=at,
        )
        db.add(item)
        await db.flush()
    source_id = "dropped" if name in {"dropped", "undropped"} else "watchlist"
    source = await ensure_watchlist_source(db, user_id, "aiostreams", "personal", source_id, name="AIOStreams")
    if name == "watchlisted":
        await upsert_watchlist_source_item(db, source, item, external_item_id=body["metaId"], now=at)
        if item.status == "removed":
            item.status = "added"
        await enqueue_personal_watchlist_sync(db, item, media)
    elif name == "unwatchlisted":
        link = await db.scalar(
            select(WatchlistSourceItem).where(
                WatchlistSourceItem.source_id == source.id,
                WatchlistSourceItem.watchlist_item_id == item.id,
            )
        )
        if link:
            await db.delete(link)
            await db.flush()
        remaining = await db.scalar(
            select(WatchlistSourceItem.id).where(WatchlistSourceItem.watchlist_item_id == item.id)
        )
        if not remaining and item.source != "manual":
            item.status = "removed"
            await enqueue_personal_watchlist_removal(db, item, media)
    elif name == "dropped":
        await upsert_watchlist_source_item(db, source, item, external_item_id=body["metaId"], now=at)
        item.status = "dropped"
        await enqueue_personal_watchlist_removal(db, item, media)
    else:
        await db.execute(
            delete(WatchlistSourceItem).where(
                WatchlistSourceItem.source_id == source.id,
                WatchlistSourceItem.watchlist_item_id == item.id,
            )
        )
        if item.status == "dropped":
            item.status = "in_progress"
        await enqueue_personal_watchlist_sync(db, item, media, unhide_dropped=True)
    item.updated_at = at
