from datetime import datetime
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Path, status
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from librarysync.api.deps import get_current_user, get_db
from librarysync.core.blacklist import normalize_id, normalize_imdb_id
from librarysync.db.models import BlacklistItem, User

router = APIRouter(
    prefix="/api/blacklist",
    tags=["blacklist"],
    dependencies=[Depends(get_current_user)],
)


class BlacklistItemIn(BaseModel):
    provider: str = Field(..., min_length=1, max_length=32)
    provider_item_id: str = Field(..., min_length=1, max_length=64)
    media_type: Literal["tv"] = "tv"
    title: str = Field(..., min_length=1, max_length=255)
    year: int | None = None
    poster_url: str | None = None
    imdb_id: str | None = None
    tmdb_id: str | None = None
    tvdb_id: str | None = None
    tvmaze_id: str | None = None


class BlacklistItemOut(BaseModel):
    id: str
    provider: str
    provider_item_id: str
    media_type: str
    title: str
    year: int | None
    poster_url: str | None
    imdb_id: str | None
    tmdb_id: str | None
    tvdb_id: str | None
    tvmaze_id: str | None
    created_at: datetime


class BlacklistListOut(BaseModel):
    items: list[BlacklistItemOut]


@router.get(
    "",
    summary="List blacklist entries",
    description=(
        "Return the TV blacklist for the current user. Blacklisted shows are only "
        "applied during imports; manual history entries are unaffected."
    ),
)
async def list_blacklist(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> BlacklistListOut:
    result = await db.execute(
        select(BlacklistItem)
        .where(BlacklistItem.user_id == current_user.id)
        .order_by(BlacklistItem.created_at.desc())
    )
    items = result.scalars().all()
    return BlacklistListOut(
        items=[
            BlacklistItemOut(
                id=item.id,
                provider=item.provider,
                provider_item_id=item.provider_item_id,
                media_type=item.media_type,
                title=item.title,
                year=item.year,
                poster_url=item.poster_url,
                imdb_id=item.imdb_id,
                tmdb_id=item.tmdb_id,
                tvdb_id=item.tvdb_id,
                tvmaze_id=item.tvmaze_id,
                created_at=item.created_at,
            )
            for item in items
        ]
    )


@router.post(
    "",
    summary="Add blacklist entry",
    description=(
        "Add a TV show to the blacklist for the current user. Blacklisted shows are "
        "only applied during imports; manual history entries are unaffected."
    ),
)
async def create_blacklist(
    payload: BlacklistItemIn,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> BlacklistItemOut:
    provider = normalize_id(payload.provider)
    provider_item_id = normalize_id(payload.provider_item_id)
    title = normalize_id(payload.title)
    if not provider or not provider_item_id or not title:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="provider, provider_item_id, and title are required.",
        )
    provider = provider.lower()
    imdb_id = normalize_imdb_id(payload.imdb_id)
    tmdb_id = normalize_id(payload.tmdb_id)
    tvdb_id = normalize_id(payload.tvdb_id)
    tvmaze_id = normalize_id(payload.tvmaze_id)
    if not any([imdb_id, tmdb_id, tvdb_id, tvmaze_id]):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="At least one of imdb_id, tmdb_id, tvdb_id, or tvmaze_id is required.",
        )

    existing_result = await db.execute(
        select(BlacklistItem).where(
            BlacklistItem.user_id == current_user.id,
            BlacklistItem.provider == provider,
            BlacklistItem.provider_item_id == provider_item_id,
        )
    )
    existing = existing_result.scalars().first()
    if existing:
        return BlacklistItemOut(
            id=existing.id,
            provider=existing.provider,
            provider_item_id=existing.provider_item_id,
            media_type=existing.media_type,
            title=existing.title,
            year=existing.year,
            poster_url=existing.poster_url,
            imdb_id=existing.imdb_id,
            tmdb_id=existing.tmdb_id,
            tvdb_id=existing.tvdb_id,
            tvmaze_id=existing.tvmaze_id,
            created_at=existing.created_at,
        )

    item = BlacklistItem(
        user_id=current_user.id,
        media_type="tv",
        provider=provider,
        provider_item_id=provider_item_id,
        title=title,
        year=payload.year,
        poster_url=normalize_id(payload.poster_url),
        imdb_id=imdb_id,
        tmdb_id=tmdb_id,
        tvdb_id=tvdb_id,
        tvmaze_id=tvmaze_id,
    )
    db.add(item)
    await db.commit()
    await db.refresh(item)
    return BlacklistItemOut(
        id=item.id,
        provider=item.provider,
        provider_item_id=item.provider_item_id,
        media_type=item.media_type,
        title=item.title,
        year=item.year,
        poster_url=item.poster_url,
        imdb_id=item.imdb_id,
        tmdb_id=item.tmdb_id,
        tvdb_id=item.tvdb_id,
        tvmaze_id=item.tvmaze_id,
        created_at=item.created_at,
    )


@router.delete(
    "/{blacklist_id}",
    summary="Remove blacklist entry",
    description="Remove a blacklist entry by ID.",
)
async def delete_blacklist(
    blacklist_id: str = Path(..., min_length=1),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    result = await db.execute(
        select(BlacklistItem).where(
            BlacklistItem.id == blacklist_id,
            BlacklistItem.user_id == current_user.id,
        )
    )
    item = result.scalars().first()
    if not item:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found.")
    await db.delete(item)
    await db.commit()
    return {"status": "deleted"}
