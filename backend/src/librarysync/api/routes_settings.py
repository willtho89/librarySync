from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from librarysync.api.deps import get_current_user, get_db
from librarysync.config import settings
from librarysync.db.models import User

router = APIRouter(
    prefix="/api/settings",
    tags=["settings"],
)


class SettingsUpdate(BaseModel):
    poll_interval: int | None = Field(None, ge=5)
    completion_threshold: float | None = Field(None, ge=0, le=100)
    include_adult_in_search: bool | None = None


class SettingsOut(BaseModel):
    poll_interval: int
    completion_threshold: float
    include_adult_in_search: bool


def _resolve_settings(user: User) -> SettingsOut:
    poll_interval = (
        user.poll_interval_seconds
        if user.poll_interval_seconds is not None
        else settings.poll_interval_seconds
    )
    completion_threshold = (
        user.completion_threshold_percent
        if user.completion_threshold_percent is not None
        else settings.completion_threshold_percent
    )
    include_adult_in_search = (
        user.include_adult_in_search
        if user.include_adult_in_search is not None
        else False
    )
    return SettingsOut(
        poll_interval=poll_interval,
        completion_threshold=completion_threshold,
        include_adult_in_search=include_adult_in_search,
    )


@router.get(
    "",
    summary="Get settings",
    description="Return effective settings for the current user.",
)
async def get_settings(
    current_user: User = Depends(get_current_user),
) -> SettingsOut:
    return _resolve_settings(current_user)


@router.post(
    "",
    summary="Update settings",
    description="Update per-user polling and completion thresholds.",
)
async def update_settings(
    payload: SettingsUpdate,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> SettingsOut:
    fields = payload.model_fields_set
    if "poll_interval" in fields:
        current_user.poll_interval_seconds = payload.poll_interval
    if "completion_threshold" in fields:
        current_user.completion_threshold_percent = payload.completion_threshold
    if "include_adult_in_search" in fields:
        current_user.include_adult_in_search = bool(payload.include_adult_in_search)
    db.add(current_user)
    await db.commit()
    await db.refresh(current_user)
    return _resolve_settings(current_user)
