import json

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from librarysync.api.deps import get_current_user, get_db
from librarysync.core.security import encrypt_value
from librarysync.db.models import Integration, IntegrationSecret, User

router = APIRouter(
    prefix="/api/integrations",
    tags=["integrations"],
)


class AIOStreamsConfig(BaseModel):
    base_url: str
    api_key: str | None = None


class IntegrationOut(BaseModel):
    provider: str
    status: str
    config: dict | None
    has_secrets: bool


def _integration_to_out(integration: Integration, has_secrets: bool) -> IntegrationOut:
    return IntegrationOut(
        provider=integration.provider,
        status=integration.status,
        config=integration.config,
        has_secrets=has_secrets,
    )


@router.get("")
async def list_integrations(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    result = await db.execute(
        select(Integration).where(Integration.user_id == current_user.id)
    )
    integrations = result.scalars().all()
    integration_ids = [integration.id for integration in integrations]
    if integration_ids:
        result = await db.execute(
            select(IntegrationSecret.integration_id).where(
                IntegrationSecret.integration_id.in_(integration_ids)
            )
        )
        secret_ids = set(result.scalars().all())
    else:
        secret_ids = set()
    return {
        "integrations": [
            _integration_to_out(integration, integration.id in secret_ids).model_dump()
            for integration in integrations
        ]
    }


@router.post("/aiostreams")
async def save_aiostreams(
    payload: AIOStreamsConfig,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    base_url = payload.base_url.strip()
    if not base_url:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="Base URL is required"
        )
    result = await db.execute(
        select(Integration).where(
            Integration.user_id == current_user.id,
            Integration.provider == "aiostreams",
        )
    )
    integration = result.scalars().first()
    if not integration:
        integration = Integration(
            user_id=current_user.id,
            provider="aiostreams",
            status="configured",
        )
    integration.config = {"base_url": base_url}
    db.add(integration)
    await db.flush()

    has_secrets = False
    api_key = payload.api_key.strip() if payload.api_key else None
    if api_key:
        encrypted = encrypt_value(json.dumps({"api_key": api_key}))
        result = await db.execute(
            select(IntegrationSecret).where(
                IntegrationSecret.integration_id == integration.id
            )
        )
        secret = result.scalars().first()
        if not secret:
            secret = IntegrationSecret(
                integration_id=integration.id,
                secret_data=encrypted,
            )
        else:
            secret.secret_data = encrypted
        db.add(secret)
        has_secrets = True
    else:
        result = await db.execute(
            select(IntegrationSecret.integration_id).where(
                IntegrationSecret.integration_id == integration.id
            )
        )
        has_secrets = result.scalar_one_or_none() is not None

    await db.commit()
    return _integration_to_out(integration, has_secrets).model_dump()


@router.post("/aiostreams/test")
async def test_aiostreams(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    result = await db.execute(
        select(Integration).where(
            Integration.user_id == current_user.id,
            Integration.provider == "aiostreams",
        )
    )
    integration = result.scalars().first()
    if not integration or not integration.config or not integration.config.get("base_url"):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="AIOStreams configuration is missing",
        )
    return {"status": "ok"}


@router.get("/trakt/start")
async def trakt_start():
    raise HTTPException(status_code=501, detail="Not implemented")


@router.get("/trakt/callback")
async def trakt_callback():
    raise HTTPException(status_code=501, detail="Not implemented")


@router.post("/trakt/disconnect")
async def trakt_disconnect():
    raise HTTPException(status_code=501, detail="Not implemented")


@router.get("/simkl/start")
async def simkl_start():
    raise HTTPException(status_code=501, detail="Not implemented")


@router.get("/simkl/callback")
async def simkl_callback():
    raise HTTPException(status_code=501, detail="Not implemented")


@router.post("/simkl/disconnect")
async def simkl_disconnect():
    raise HTTPException(status_code=501, detail="Not implemented")
