from fastapi import APIRouter, Depends, HTTPException

from librarysync.api.deps import get_current_user

router = APIRouter(
    prefix="/api",
    tags=["activity"],
    dependencies=[Depends(get_current_user)],
)


@router.get("/activity/events")
async def events():
    raise HTTPException(status_code=501, detail="Not implemented")


@router.get("/activity/sessions")
async def sessions():
    raise HTTPException(status_code=501, detail="Not implemented")


@router.get("/outbox")
async def outbox():
    raise HTTPException(status_code=501, detail="Not implemented")


@router.get("/status")
async def status():
    raise HTTPException(status_code=501, detail="Not implemented")
