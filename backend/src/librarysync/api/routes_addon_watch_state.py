from __future__ import annotations

import hashlib
import secrets
import uuid
from datetime import datetime, timedelta, timezone
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from pydantic import BaseModel, Field, StrictBool, StrictInt, field_validator, model_validator
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from librarysync.api.deps import get_current_user, get_db
from librarysync.core.addon_watch_state import build_watch_state, receive_watch_state
from librarysync.db.models import (
    EpisodeItem,
    MediaItem,
    OutboxJob,
    StremioAddonConfig,
    User,
    WatchStateEntry,
    WatchStateReceipt,
    WatchStateSnapshot,
    WatchStateViewer,
)

router = APIRouter(tags=["stremio-addon"])
Identifier = Annotated[str, Field(min_length=1, max_length=255)]
EpisodeNumber = Annotated[StrictInt, Field(ge=0, le=2147483647)]
Position = Annotated[StrictInt, Field(ge=0, le=9007199254740991)]


class BulkVideo(BaseModel):
    videoId: Identifier
    season: EpisodeNumber | None
    episode: EpisodeNumber


class WatchStatePush(BaseModel):
    id: Annotated[str, Field(min_length=1, max_length=1024)]
    event: Literal[
        "start",
        "pause",
        "stop",
        "played",
        "unplayed",
        "watchlisted",
        "unwatchlisted",
        "dropped",
        "undropped",
        "rated",
        "unrated",
    ]
    scope: Literal["movie", "episode", "season", "series"] | None = None
    at: Annotated[StrictInt, Field(ge=0, le=253402300799)]
    metaId: Identifier
    videoId: Identifier | None = None
    season: EpisodeNumber | None = None
    episode: EpisodeNumber | None = None
    played: StrictBool = False
    positionMs: Position | None = None
    durationMs: Annotated[StrictInt, Field(gt=0, le=9007199254740991)] | None = None
    rating: Annotated[float, Field(ge=0, le=10, allow_inf_nan=False)] | None = None
    ids: dict[str, Annotated[str, Field(min_length=1, max_length=32)] | StrictInt] = Field(default_factory=dict)
    videos: Annotated[list[BulkVideo], Field(min_length=1, max_length=500)] | None = None
    part: Annotated[StrictInt, Field(ge=1, le=100000)] | None = None
    parts: Annotated[StrictInt, Field(ge=1, le=100000)] | None = None

    @field_validator("rating", mode="before")
    @classmethod
    def numeric_rating(cls, value):
        if isinstance(value, bool) or (value is not None and not isinstance(value, (int, float))):
            raise ValueError("Rating must be a number")
        return value

    @model_validator(mode="after")
    def event_fields(self):
        if self.event == "rated" and self.rating is None:
            raise ValueError("Rated events require a rating")
        if self.videos is not None:
            if self.event not in {"played", "unplayed"} or self.scope not in {"season", "series"}:
                raise ValueError("Only season/series watched marks can contain videos")
            if self.part is None or self.parts is None or self.part > self.parts:
                raise ValueError("Bulk marks require valid part/parts")
            if self.scope == "season" and self.season is None:
                raise ValueError("Season marks require a season")
            if len({v.videoId for v in self.videos}) != len(self.videos):
                raise ValueError("Bulk videos must be unique")
        elif self.part is not None or self.parts is not None:
            raise ValueError("part/parts require videos")
        return self


async def _load_config(
    db: AsyncSession,
    addon_id: str,
    viewer: str | None,
    *,
    lock: bool = False,
) -> tuple[StremioAddonConfig, str]:
    query = (
        select(StremioAddonConfig)
        .join(User, User.id == StremioAddonConfig.user_id)
        .where(StremioAddonConfig.id == addon_id, User.is_active.is_(True))
    )
    if lock:
        # Serialize pushes for this user. The WatchEvent unique key also protects retry deduplication.
        query = query.with_for_update()
    config = await db.scalar(query)
    if not config or not config.is_enabled or not config.watch_state_enabled:
        raise HTTPException(404, "Not found")
    user_id = config.user_id
    if viewer is not None:
        user_id = await db.scalar(
            select(WatchStateViewer.user_id)
            .join(User, User.id == WatchStateViewer.user_id)
            .where(WatchStateViewer.addon_id == addon_id, WatchStateViewer.viewer == viewer, User.is_active.is_(True))
        )
        if user_id is None:
            raise HTTPException(404, "Not found")
    if lock:
        await db.scalar(select(User.id).where(User.id == user_id).with_for_update())
    return config, user_id


@router.get("/stremio-addon/{addon_id}/watch_state/pull.json", include_in_schema=False)
async def pull_watch_state(
    addon_id: str,
    response: Response,
    since: str | None = Query(None, max_length=128),
    viewer: str | None = Query(None),
    db: AsyncSession = Depends(get_db),
) -> dict:
    _config, user_id = await _load_config(db, addon_id, viewer)
    response.headers["Cache-Control"] = "no-store"
    result = await build_watch_state(db, user_id, since)
    await db.commit()
    return result


@router.post("/stremio-addon/{addon_id}/watch_state/push/{media_type}/{item_id}.json", include_in_schema=False)
async def push_watch_state(
    addon_id: str,
    media_type: Literal["movie", "series"],
    item_id: str,
    payload: WatchStatePush,
    viewer: str | None = Query(None),
    db: AsyncSession = Depends(get_db),
) -> Response:
    config, user_id = await _load_config(db, addon_id, viewer, lock=True)
    body = validate_event(media_type, item_id, payload)
    await receive_watch_state(db, config.id, viewer or "", user_id, media_type, body)
    await db.commit()
    return Response(status_code=204, headers={"Cache-Control": "no-store"})


def validate_event(media_type: str, item_id: str, payload: WatchStatePush) -> dict:
    title_event = payload.event in {"watchlisted", "unwatchlisted", "dropped", "undropped"}
    if item_id != (payload.metaId if title_event or payload.videos else payload.videoId or payload.metaId):
        raise HTTPException(422, "Watch event path does not match the video id")
    scope = payload.scope
    if scope is None:
        scope = "movie" if media_type == "movie" else "episode"
        if title_event or (payload.event in {"rated", "unrated"} and not payload.videoId):
            if media_type == "movie":
                scope = "movie"
            elif payload.season is not None:
                scope = "season"
            else:
                scope = "series"
    if media_type == "movie" and (scope != "movie" or payload.videos or payload.event in {"dropped", "undropped"}):
        raise HTTPException(422, "Watch event scope does not match the media type")
    if media_type == "series" and scope == "movie":
        raise HTTPException(422, "Series events cannot target movies")
    if title_event and scope != ("movie" if media_type == "movie" else "series"):
        raise HTTPException(422, "Watchlist and drop changes target a title")
    if scope == "episode" and (
        payload.episode is None or not payload.videoId or "season" not in payload.model_fields_set
    ):
        raise HTTPException(422, "Episode events require videoId, season (nullable), and episode")
    if (
        scope in {"season", "series"}
        and not title_event
        and payload.event not in {"rated", "unrated"}
        and not payload.videos
    ):
        raise HTTPException(422, "Series and season watched marks require videos")
    if scope == "season" and payload.season is None:
        raise HTTPException(422, "Season events require a season")
    body = payload.model_dump(mode="json")
    body["scope"] = scope
    return body


class ViewerInvite(BaseModel):
    viewer: Annotated[str, Field(pattern=r"^[a-z0-9]+(?:-[a-z0-9]+)*$", min_length=1, max_length=32)]


class ViewerAccept(BaseModel):
    invitation: Annotated[str, Field(min_length=32, max_length=128)]


@router.post("/api/stremio-addon/watch-state/viewers", status_code=201)
async def invite_viewer(
    payload: ViewerInvite, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)
):
    config = await db.scalar(select(StremioAddonConfig).where(StremioAddonConfig.user_id == user.id).with_for_update())
    if not config or not config.watch_state_enabled or not config.is_enabled:
        raise HTTPException(400, "Enable Watch State before inviting a viewer")
    viewer = await db.scalar(
        select(WatchStateViewer).where(
            WatchStateViewer.addon_id == config.id,
            WatchStateViewer.viewer == payload.viewer,
        )
    )
    if viewer and viewer.user_id:
        raise HTTPException(409, "Viewer already bound; revoke the binding first")
    if viewer is None:
        viewer = WatchStateViewer(addon_id=config.id, viewer=payload.viewer)
        db.add(viewer)
    token = secrets.token_urlsafe(32)
    viewer.invitation_hash = hashlib.sha256(token.encode()).hexdigest()
    viewer.expires_at = datetime.now(timezone.utc) + timedelta(days=1)
    await db.commit()
    return {"viewer": payload.viewer, "invitation": token, "expires_at": viewer.expires_at}


@router.post("/api/stremio-addon/watch-state/viewers/accept")
async def accept_viewer(
    payload: ViewerAccept, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)
):
    viewer = await db.scalar(
        select(WatchStateViewer)
        .where(
            WatchStateViewer.invitation_hash == hashlib.sha256(payload.invitation.encode()).hexdigest(),
            WatchStateViewer.user_id.is_(None),
            WatchStateViewer.expires_at > datetime.now(timezone.utc),
        )
        .with_for_update()
    )
    if not viewer:
        raise HTTPException(404, "Invitation not found or expired")
    config = await db.get(StremioAddonConfig, viewer.addon_id)
    owner = await db.get(User, config.user_id)
    if not config.is_enabled or not config.watch_state_enabled or not owner.is_active:
        raise HTTPException(404, "Invitation not available")
    viewer.user_id = user.id
    viewer.invitation_hash = None
    viewer.expires_at = None
    await db.commit()
    return {"viewer": viewer.viewer, "status": "bound"}


@router.delete("/api/stremio-addon/watch-state/viewers/{binding_id}", status_code=204, response_class=Response)
async def revoke_viewer(binding_id: str, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    viewer = await db.get(WatchStateViewer, binding_id, with_for_update=True)
    config = await db.get(StremioAddonConfig, viewer.addon_id) if viewer else None
    if not viewer or user.id not in {config.user_id, viewer.user_id}:
        raise HTTPException(404, "Binding not found")
    await db.delete(viewer)
    await db.commit()


@router.get("/api/stremio-addon/watch-state/status")
async def watch_state_status(user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    from librarysync.core.watch_state_pull import build_watch_state as build_private_state

    state = await build_private_state(db, user.id, None, mark_pulled=False)
    receipts = list(
        (
            await db.scalars(
                select(WatchStateReceipt)
                .where(
                    WatchStateReceipt.user_id == user.id,
                )
                .order_by(WatchStateReceipt.received_at.desc())
                .limit(100)
            )
        ).all()
    )
    snapshot = await db.get(WatchStateSnapshot, user.id)
    bindings = list(
        (
            await db.scalars(
                select(WatchStateViewer)
                .outerjoin(
                    StremioAddonConfig,
                    StremioAddonConfig.id == WatchStateViewer.addon_id,
                )
                .where((StremioAddonConfig.user_id == user.id) | (WatchStateViewer.user_id == user.id))
            )
        ).all()
    )
    jobs = list(
        (
            await db.scalars(
                select(OutboxJob)
                .where(
                    OutboxJob.user_id == user.id,
                    OutboxJob.target_provider != "internal",
                )
                .order_by(OutboxJob.created_at.desc())
                .limit(100)
            )
        ).all()
    )
    playback = list(
        (
            await db.scalars(
                select(WatchStateEntry)
                .where(
                    WatchStateEntry.user_id == user.id,
                    WatchStateEntry.category == "playback",
                )
                .order_by(WatchStateEntry.occurred_at.desc())
                .limit(100)
            )
        ).all()
    )
    playback_ids = {row.video_id or row.meta_id: row.id for row in playback}
    result = {
        "last_received_at": receipts[0].received_at if receipts else None,
        "last_snapshot_at": snapshot.built_at if snapshot else None,
        "last_pulled_at": snapshot.last_pulled_at if snapshot else None,
        "events": [
            {
                "id": row.id,
                "event": row.event,
                "viewer": row.viewer or None,
                "status": row.status,
                "duplicates": row.duplicates,
                "received_at": row.received_at,
                "error": row.error,
                "outcomes": row.outcomes,
                "metaId": row.payload["metaId"],
                "type": row.media_type,
            }
            for row in receipts
        ],
        "viewers": [
            {"id": row.id, "viewer": row.viewer, "status": "bound" if row.user_id else "invited", "can_revoke": True}
            for row in bindings
        ],
        "deliveries": [
            {
                "id": job.id,
                "provider": job.target_provider,
                "operation": job.job_type,
                "status": job.status,
                "error": job.last_error,
            }
            for job in jobs
        ],
        "resume": [
            {**row, "id": playback_ids.get(row["videoId"])}
            for row in state["items"]
            if not row["played"] and row.get("positionMs")
        ],
        "ratings": state["ratings"],
        "next_up": state["watched"]["nextUp"],
        "dropped": state["watched"]["dropped"],
    }
    await db.commit()
    return result


@router.delete("/api/stremio-addon/watch-state/resume/{entry_id}", status_code=204, response_class=Response)
async def clear_resume(entry_id: str, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    await db.scalar(select(User.id).where(User.id == user.id).with_for_update())
    entry = await db.get(WatchStateEntry, entry_id, with_for_update=True)
    if not entry or entry.user_id != user.id or entry.category != "playback":
        raise HTTPException(404, "Resume point not found")
    entry.payload = {**entry.payload, "event": "start", "positionMs": None, "durationMs": None}
    entry.occurred_at = datetime.now(timezone.utc)
    await db.commit()


@router.post("/api/stremio-addon/watch-state/ratings/{media_type}", status_code=204, response_class=Response)
async def manage_rating(
    media_type: Literal["movie", "series"],
    payload: WatchStatePush,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    from librarysync.core.watch_state_events import apply_event

    if payload.event not in {"rated", "unrated"}:
        raise HTTPException(422, "This endpoint only changes ratings")
    body = validate_event(media_type, payload.videoId or payload.metaId, payload)
    body.update(id=str(uuid.uuid4()), at=int(datetime.now(timezone.utc).timestamp()))
    await db.scalar(select(User.id).where(User.id == user.id).with_for_update())
    await apply_event(db, user.id, media_type, body)
    await db.commit()


@router.post("/api/stremio-addon/watch-state/events/{receipt_id}/retry")
async def retry_event(receipt_id: str, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    receipt = await db.get(WatchStateReceipt, receipt_id, with_for_update=True)
    if not receipt or receipt.user_id != user.id:
        raise HTTPException(404, "Event not found")
    if receipt.status in {"unresolved", "failed"}:
        receipt.status = "pending"
        receipt.error = None
        await db.commit()
    return {"status": receipt.status}


class EventMapping(BaseModel):
    video_id: Identifier
    media_item_id: Identifier
    episode_item_id: Identifier | None = None
    season: EpisodeNumber | None = None
    episode: EpisodeNumber | None = None


@router.post("/api/stremio-addon/watch-state/events/{receipt_id}/resolve")
async def resolve_event(
    receipt_id: str, payload: EventMapping, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)
):
    receipt = await db.get(WatchStateReceipt, receipt_id, with_for_update=True)
    if not receipt or receipt.user_id != user.id:
        raise HTTPException(404, "Event not found")
    if receipt.status != "unresolved":
        raise HTTPException(409, "Only unresolved events can be mapped")
    videos = receipt.payload.get("videos") or [receipt.payload]
    video = next((v for v in videos if (v.get("videoId") or v.get("metaId")) == payload.video_id), None)
    if video is None:
        raise HTTPException(422, "Video is not part of this event")
    media = await db.get(MediaItem, payload.media_item_id)
    if not media or (media.media_type == "movie") != (receipt.media_type == "movie"):
        raise HTTPException(422, "Title does not match the event type")
    mapping = {"_canonical_media_item_id": media.id}
    if receipt.media_type == "series":
        episode = await db.get(EpisodeItem, payload.episode_item_id) if payload.episode_item_id else None
        if episode is None and payload.season is not None and payload.episode is not None:
            episode = await db.scalar(
                select(EpisodeItem).where(
                    EpisodeItem.show_media_item_id == media.id,
                    EpisodeItem.season_number == payload.season,
                    EpisodeItem.episode_number == payload.episode,
                )
            )
            if episode is None:
                episode = EpisodeItem(
                    show_media_item_id=media.id, season_number=payload.season, episode_number=payload.episode
                )
                db.add(episode)
                await db.flush()
        if not episode or episode.show_media_item_id != media.id:
            raise HTTPException(422, "Choose an episode belonging to this title")
        mapping["_canonical_episode_item_id"] = episode.id
    receipt.payload = {
        **receipt.payload,
        "_mappings": {**receipt.payload.get("_mappings", {}), payload.video_id: mapping},
    }
    receipt.status = "pending"
    receipt.error = None
    await db.commit()
    return {"status": receipt.status}
