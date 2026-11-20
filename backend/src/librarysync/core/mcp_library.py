"""Account-scoped catalog operations exposed by the private MCP listener."""

import base64
import hmac
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy import and_, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from librarysync.core.watchlist import upsert_resolved_watchlist_item
from librarysync.core.watchlist_sources import ensure_manual_watchlist_source, upsert_watchlist_source_item
from librarysync.core.watchlist_sync import enqueue_personal_watchlist_sync
from librarysync.db.models import EpisodeItem, MediaItem, User, WatchedItem, WatchlistItem

CATALOG_TYPES = ("movie", "tv", "anime")
_TITLE_LIMIT = 5


class ToolArgs(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, hide_input_in_errors=True)


class HistoryArgs(ToolArgs):
    limit: int = Field(default=20, ge=1, le=50)
    cursor: str | None = Field(default=None, min_length=1, max_length=256, pattern=r"^[A-Za-z0-9_-]+$")


class FindTitleArgs(ToolArgs):
    title: str | None = Field(default=None, min_length=1, max_length=255)
    year: int | None = Field(default=None, ge=1, le=9999)
    media_type: Literal["movie", "tv", "anime"] | None = None
    imdb_id: str | None = Field(default=None, pattern=r"^tt[0-9]{7,10}$")

    @model_validator(mode="after")
    def validate_lookup(self) -> Self:
        if self.imdb_id is not None:
            if self.title is not None or self.year is not None or self.media_type is not None:
                raise ValueError("IMDb lookup does not accept title filters")
        elif self.title is None or not self.title.strip() or "\x00" in self.title:
            raise ValueError("An exact title or IMDb ID is required")
        return self


class AddToWatchlistArgs(ToolArgs):
    media_id: str = Field(pattern=r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")


@dataclass(frozen=True, slots=True)
class ApprovalTarget:
    id: str
    title: str
    year: int | None
    media_type: str
    provider_identity: str

    @classmethod
    def from_media(cls, media: MediaItem) -> Self:
        # These are the identifiers consumed by the existing watchlist payload builders.
        # Compare the exact snapshot under the media row lock before preparing any jobs.
        raw = media.raw if isinstance(media.raw, dict) else {}
        provider_identity = json.dumps(
            [media.imdb_id, media.tmdb_id, media.tvdb_id, raw.get("simkl_id"), raw.get("letterboxd_film_id")],
            sort_keys=True,
        )
        return cls(media.id, media.title, media.year, media.media_type, provider_identity)


class InvalidCursor(ValueError):
    pass


def _history_signature(secret: bytes, user_id: str, credential: str, position: bytes) -> bytes:
    return hmac.digest(secret, f"{user_id}:{credential}:".encode("ascii") + position, "sha256")


def _encode_cursor(secret: bytes, user_id: str, credential: str, watched_at: datetime, watch_id: str) -> str:
    position = json.dumps([watched_at.isoformat(), watch_id], separators=(",", ":")).encode("ascii")
    signed_position = _history_signature(secret, user_id, credential, position) + position
    return base64.urlsafe_b64encode(signed_position).decode().rstrip("=")


def _decode_cursor(secret: bytes, user_id: str, credential: str, cursor: str) -> tuple[datetime, str]:
    try:
        raw = base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4))
        signature, position = raw[:32], raw[32:]
        if hmac.compare_digest(signature, _history_signature(secret, user_id, credential, position)):
            date, watch_id = json.loads(position)
            return datetime.fromisoformat(date), watch_id
    except (ValueError, TypeError):
        pass
    raise InvalidCursor("Invalid or expired history cursor")


async def list_history(db: AsyncSession, user_id: str, credential: str, cursor_key: bytes, args: HistoryArgs) -> dict:
    # A row is one stored history record (including re-watches and duplicate imports),
    # not one title. The date/ID keyset is live, not a snapshot across calls.
    query = (
        select(
            WatchedItem.id,
            WatchedItem.watched_at,
            WatchedItem.rating,
            MediaItem.title,
            MediaItem.media_type,
            MediaItem.year,
            MediaItem.imdb_id,
            EpisodeItem.season_number,
            EpisodeItem.episode_number,
        )
        .select_from(WatchedItem)
        .outerjoin(EpisodeItem, EpisodeItem.id == WatchedItem.episode_item_id)
        .join(MediaItem, MediaItem.id == func.coalesce(WatchedItem.media_item_id, EpisodeItem.show_media_item_id))
        .where(WatchedItem.user_id == user_id)
        .order_by(WatchedItem.watched_at.desc(), WatchedItem.id.desc())
        .limit(args.limit + 1)
    )
    if args.cursor is not None:
        date, watch_id = _decode_cursor(cursor_key, user_id, credential, args.cursor)
        query = query.where(
            or_(WatchedItem.watched_at < date, and_(WatchedItem.watched_at == date, WatchedItem.id < watch_id))
        )
    rows = (await db.execute(query)).all()
    items = []
    for row in rows[: args.limit]:
        date = row.watched_at
        date = date.replace(tzinfo=timezone.utc) if date.tzinfo is None else date.astimezone(timezone.utc)
        item = {
            "id": row.id,
            "title": row.title,
            "media_type": row.media_type,
            "year": row.year,
            "imdb_id": row.imdb_id,
            "date": date.isoformat(),
            "rating": row.rating,
        }
        if row.season_number is not None:
            item.update(season=row.season_number, episode=row.episode_number)
        items.append(item)
    cursor = None
    if len(rows) > args.limit:
        last = rows[args.limit - 1]
        cursor = _encode_cursor(cursor_key, user_id, credential, last.watched_at, last.id)
    return {"items": items, "next_cursor": cursor}


async def find_title(db: AsyncSession, user_id: str, args: FindTitleArgs) -> dict:
    watched_directly = (
        select(WatchedItem.id).where(WatchedItem.user_id == user_id, WatchedItem.media_item_id == MediaItem.id).exists()
    )
    watched_episode = (
        select(WatchedItem.id)
        .join(EpisodeItem, WatchedItem.episode_item_id == EpisodeItem.id)
        .where(WatchedItem.user_id == user_id, EpisodeItem.show_media_item_id == MediaItem.id)
        .exists()
    )
    query = (
        select(
            MediaItem.id,
            MediaItem.title,
            MediaItem.media_type,
            MediaItem.year,
            MediaItem.imdb_id,
            or_(watched_directly, watched_episode).label("watched"),
            WatchlistItem.status.label("watchlist_status"),
        )
        .outerjoin(WatchlistItem, and_(WatchlistItem.media_item_id == MediaItem.id, WatchlistItem.user_id == user_id))
        .where(MediaItem.media_type.in_(CATALOG_TYPES))
        .order_by(MediaItem.year.asc().nulls_last(), MediaItem.media_type, MediaItem.id)
        .limit(_TITLE_LIMIT + 1)
    )
    if args.imdb_id is not None:
        query = query.where(MediaItem.imdb_id == args.imdb_id)
    else:
        query = query.where(MediaItem.title == args.title)
        if args.year is not None:
            query = query.where(MediaItem.year == args.year)
        if args.media_type is not None:
            query = query.where(MediaItem.media_type == args.media_type)
    rows = (await db.execute(query)).mappings().all()
    return {
        "status": "found" if rows else "not_found",
        "ambiguous": len(rows) > 1,
        "truncated": len(rows) > _TITLE_LIMIT,
        "matches": [dict(row) for row in rows[:_TITLE_LIMIT]],
    }


async def load_approval_target(db: AsyncSession, media_id: str) -> ApprovalTarget | None:
    media = await db.get(MediaItem, media_id)
    if media is None or media.media_type not in CATALOG_TYPES:
        return None
    return ApprovalTarget.from_media(media)


async def add_approved_watchlist_item(db: AsyncSession, user_id: str, target: ApprovalTarget) -> dict[str, str]:
    # Serialize different first additions as well as duplicates for this account (the manual
    # source is account-wide). FOR NO KEY UPDATE is compatible with FK checks on the user row.
    active = await db.scalar(
        select(User.id).where(User.id == user_id, User.is_active.is_(True)).with_for_update(key_share=True)
    )
    if active is None:
        return {"status": "account_inactive"}
    media = await db.scalar(select(MediaItem).where(MediaItem.id == target.id).with_for_update(read=True))
    if media is None or ApprovalTarget.from_media(media) != target:
        return {"status": "target_changed"}

    now = datetime.now(timezone.utc)
    item, status = await upsert_resolved_watchlist_item(db, user_id, media, "manual", now=now, create_only=True)
    if status == "created":
        source = await ensure_manual_watchlist_source(db, user_id)
        await upsert_watchlist_source_item(db, source, item, external_item_id=None, now=now)
        await enqueue_personal_watchlist_sync(db, item, media)
    return {"status": status, "id": item.id}
