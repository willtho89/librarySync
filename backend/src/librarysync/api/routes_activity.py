from fastapi import APIRouter, Depends, HTTPException

from librarysync.api.deps import get_current_user

router = APIRouter(
    prefix="/api",
    tags=["activity"],
    dependencies=[Depends(get_current_user)],
)


@router.get(
    "/activity/events",
    summary="List recent progress events (stub)",
    description="Placeholder for returning recent progress and completion events.",
)
async def events():
    raise HTTPException(status_code=501, detail="Not implemented")


@router.get(
    "/activity/sessions",
    summary="List active sessions (stub)",
    description="Placeholder for returning active playback sessions.",
)
async def sessions():
    raise HTTPException(status_code=501, detail="Not implemented")


@router.get(
    "/outbox",
    summary="List outbox jobs (stub)",
    description="Placeholder for returning outbox delivery jobs.",
)
async def outbox():
    raise HTTPException(status_code=501, detail="Not implemented")


@router.get(
    "/status",
    summary="Get sync status (stub)",
    description="Placeholder for returning poller and worker status.",
)
async def status():
    raise HTTPException(status_code=501, detail="Not implemented")
