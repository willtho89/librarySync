from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from librarysync.api.deps import get_current_user, get_db
from librarysync.db.models import User

router = APIRouter(
    prefix="/api/settings",
    tags=["settings"],
)


class SettingsUpdate(BaseModel):
    include_adult_in_search: bool | None = None


class SettingsOut(BaseModel):
    include_adult_in_search: bool


def _resolve_settings(user: User) -> SettingsOut:
    include_adult_in_search = bool(user.include_adult_in_search)
    return SettingsOut(
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
    description="Update per-user search settings.",
)
async def update_settings(
    payload: SettingsUpdate,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> SettingsOut:
    fields = payload.model_fields_set
    if "include_adult_in_search" in fields:
        current_user.include_adult_in_search = bool(payload.include_adult_in_search)
    db.add(current_user)
    await db.commit()
    await db.refresh(current_user)
    return _resolve_settings(current_user)
