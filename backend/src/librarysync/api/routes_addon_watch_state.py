from __future__ import annotations

from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from pydantic import BaseModel, Field, StrictBool, StrictInt
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from librarysync.api.deps import get_db
from librarysync.core.addon_watch_state import build_watch_state, record_watch_state
from librarysync.db.models import StremioAddonConfig, User

router = APIRouter(prefix="/stremio-addon", tags=["stremio-addon"])
Identifier = Annotated[str, Field(min_length=1, max_length=255)]
EpisodeNumber = Annotated[StrictInt, Field(ge=0, le=2147483647)]


class WatchStatePush(BaseModel):
    id: Annotated[str, Field(min_length=1, max_length=1024)]
    event: Literal["stop", "played", "unplayed"]
    scope: Literal["movie", "episode"] | None = None
    at: Annotated[StrictInt, Field(ge=0, le=253402300799)]
    metaId: Identifier
    videoId: Identifier | None = None
    season: EpisodeNumber | None = None
    episode: EpisodeNumber | None = None
    played: StrictBool = False
    ids: dict[str, Annotated[str, Field(min_length=1, max_length=32)]] = Field(default_factory=dict)


async def _load_config(
    db: AsyncSession,
    addon_id: str,
    viewer: str | None,
    *,
    lock: bool = False,
) -> StremioAddonConfig:
    query = (
        select(StremioAddonConfig)
        .join(User, User.id == StremioAddonConfig.user_id)
        .where(StremioAddonConfig.id == addon_id, User.is_active.is_(True))
    )
    if lock:
        # Serialize pushes for this user. The WatchEvent unique key also protects retry deduplication.
        query = query.with_for_update()
    config = await db.scalar(query)
    if viewer is not None or not config or not config.is_enabled or not config.watch_state_enabled:
        raise HTTPException(404, "Not found")
    return config


@router.get("/{addon_id}/watch_state/pull.json", include_in_schema=False)
async def pull_watch_state(
    addon_id: str,
    response: Response,
    since: str | None = Query(None, max_length=128),
    viewer: str | None = Query(None),
    db: AsyncSession = Depends(get_db),
) -> dict:
    config = await _load_config(db, addon_id, viewer)
    response.headers["Cache-Control"] = "no-store"
    return await build_watch_state(db, config.user_id, since)


@router.post("/{addon_id}/watch_state/push/{media_type}/{item_id}.json", include_in_schema=False)
async def push_watch_state(
    addon_id: str,
    media_type: Literal["movie", "series"],
    item_id: str,
    payload: WatchStatePush,
    viewer: str | None = Query(None),
    db: AsyncSession = Depends(get_db),
) -> Response:
    config = await _load_config(db, addon_id, viewer, lock=True)
    expected_scope = "movie" if media_type == "movie" else "episode"
    if payload.scope is not None and payload.scope != expected_scope:
        raise HTTPException(422, "Watch event scope does not match the media type")
    if item_id != (payload.videoId or payload.metaId):
        raise HTTPException(422, "Watch event path does not match the video id")
    if media_type == "series" and (payload.season is None or payload.episode is None or not payload.videoId):
        raise HTTPException(422, "Series history requires a video id and explicit season and episode numbers")
    await record_watch_state(db, config.user_id, media_type, payload.model_dump(exclude_none=True))
    await db.commit()
    return Response(status_code=204, headers={"Cache-Control": "no-store"})
