import json
import secrets
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import RedirectResponse
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from librarysync.api.deps import get_current_user, get_db
from librarysync.connectors.services.letterboxd import (
    DEFAULT_LETTERBOXD_API_BASE_URL,
    LetterboxdClient,
    LetterboxdError,
    extract_member_id,
    has_required_letterboxd_fields,
)
from librarysync.connectors.services.trakt import (
    TRAKT_OAUTH_AUTHORIZE_URL,
    TraktClient,
    TraktError,
    parse_expires_at,
    token_to_secret_payload,
)
from librarysync.config import settings
from librarysync.core.integrations import load_integration_with_secrets
from librarysync.core.security import decrypt_value, encrypt_value
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


class LetterboxdConfig(BaseModel):
    api_base_url: str | None = None
    client_id: str | None = None
    client_secret: str | None = None
    refresh_token: str | None = None
    cookies: dict[str, str] | None = None


def _normalize_optional(value: str | None) -> str | None:
    if value is None:
        return None
    cleaned = value.strip()
    return cleaned or None


def _normalize_cookies(cookies: dict[str, str] | None) -> dict[str, str] | None:
    if cookies is None:
        return None
    cleaned: dict[str, str] = {}
    for key, value in cookies.items():
        key_str = str(key).strip()
        value_str = str(value).strip() if value is not None else ""
        if key_str and value_str:
            cleaned[key_str] = value_str
    return cleaned


async def _load_letterboxd_integration(
    db: AsyncSession, user_id: str
) -> tuple[Integration | None, dict[str, object] | None]:
    return await load_integration_with_secrets(db, user_id, "letterboxd")


async def _load_trakt_integration(
    db: AsyncSession, user_id: str
) -> tuple[Integration | None, dict[str, object] | None]:
    return await load_integration_with_secrets(db, user_id, "trakt")


def _require_trakt_settings() -> tuple[str, str, str]:
    if not settings.trakt_client_id or not settings.trakt_client_secret:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Trakt client ID/secret are not configured",
        )
    if not settings.base_url:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="LIBRARYSYNC_BASE_URL must be set for Trakt OAuth",
        )
    base_url = settings.base_url.rstrip("/")
    redirect_uri = f"{base_url}/api/integrations/trakt/callback"
    return settings.trakt_client_id, settings.trakt_client_secret, redirect_uri


@router.get(
    "",
    summary="List integrations",
    description="Return the current user's integration settings and secret status.",
)
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


@router.post(
    "/aiostreams",
    summary="Save AIOStreams settings",
    description="Upsert the AIOStreams base URL and optional API key.",
)
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


@router.post(
    "/aiostreams/test",
    summary="Test AIOStreams settings",
    description="Verify the AIOStreams configuration exists for the current user.",
)
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


@router.post(
    "/letterboxd",
    summary="Save Letterboxd settings",
    description="Upsert Letterboxd credentials and optional API base URL.",
)
async def save_letterboxd(
    payload: LetterboxdConfig,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    result = await db.execute(
        select(Integration).where(
            Integration.user_id == current_user.id,
            Integration.provider == "letterboxd",
        )
    )
    integration = result.scalars().first()
    is_new = integration is None
    if not integration:
        integration = Integration(
            user_id=current_user.id,
            provider="letterboxd",
            status="configured",
        )

    api_base_url = _normalize_optional(payload.api_base_url)
    existing_base = None
    if integration.config:
        existing_base = integration.config.get("api_base_url")
    if not api_base_url:
        api_base_url = existing_base or DEFAULT_LETTERBOXD_API_BASE_URL

    config = dict(integration.config or {})
    config["api_base_url"] = api_base_url
    integration.config = config
    integration.status = "configured"
    db.add(integration)
    await db.flush()

    result = await db.execute(
        select(IntegrationSecret).where(
            IntegrationSecret.integration_id == integration.id
        )
    )
    secret = result.scalars().first()
    secret_data: dict[str, object] = {}
    if secret:
        try:
            data = json.loads(decrypt_value(secret.secret_data))
        except (ValueError, json.JSONDecodeError):
            data = {}
        if isinstance(data, dict):
            secret_data = {str(key): value for key, value in data.items()}

    client_id = _normalize_optional(payload.client_id)
    client_secret = _normalize_optional(payload.client_secret)
    refresh_token = _normalize_optional(payload.refresh_token)
    cookies = _normalize_cookies(payload.cookies)

    updated: dict[str, object] = dict(secret_data)
    if client_id is not None:
        updated["client_id"] = client_id
    if client_secret is not None:
        updated["client_secret"] = client_secret
    if refresh_token is not None:
        updated["refresh_token"] = refresh_token
    if cookies is not None:
        updated["cookies"] = cookies

    if not updated and is_new:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Client ID, client secret, and refresh token are required",
        )

    if updated and not has_required_letterboxd_fields(updated):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Client ID, client secret, and refresh token are required",
        )

    has_secrets = False
    if updated:
        encrypted = encrypt_value(json.dumps(updated))
        if not secret:
            secret = IntegrationSecret(
                integration_id=integration.id,
                secret_data=encrypted,
            )
        else:
            secret.secret_data = encrypted
        db.add(secret)
        has_secrets = True

    await db.commit()
    return _integration_to_out(integration, has_secrets).model_dump()


@router.post(
    "/letterboxd/test",
    summary="Test Letterboxd credentials",
    description="Validate stored Letterboxd credentials against the /me endpoint.",
)
async def test_letterboxd(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    integration, secret_data = await _load_letterboxd_integration(db, current_user.id)
    if not integration or not secret_data:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Letterboxd credentials are missing",
        )

    if not has_required_letterboxd_fields(secret_data):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Letterboxd credentials are incomplete",
        )

    api_base_url = DEFAULT_LETTERBOXD_API_BASE_URL
    if integration.config:
        api_base_url = integration.config.get("api_base_url") or api_base_url

    cookies_value = secret_data.get("cookies")
    cookies: dict[str, str] | None = None
    if isinstance(cookies_value, dict):
        cookies = {str(key): str(value) for key, value in cookies_value.items()}
    elif isinstance(cookies_value, str):
        try:
            parsed = json.loads(cookies_value)
        except json.JSONDecodeError:
            parsed = None
        if isinstance(parsed, dict):
            cookies = {str(key): str(value) for key, value in parsed.items()}

    client = LetterboxdClient(
        api_base_url=api_base_url,
        client_id=str(secret_data["client_id"]),
        client_secret=str(secret_data["client_secret"]),
        refresh_token=str(secret_data["refresh_token"]),
        cookies=cookies,
    )
    try:
        me_payload = await client.fetch_me()
    except LetterboxdError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)
        ) from exc

    member_id = extract_member_id(me_payload)
    if member_id:
        config = dict(integration.config or {})
        if config.get("member_id") != member_id:
            config["member_id"] = member_id
            integration.config = config
            db.add(integration)
            await db.commit()

    return {"status": "ok"}


@router.get(
    "/trakt/start",
    summary="Start Trakt OAuth",
    description="Initiate the Trakt OAuth flow for the current user.",
)
async def trakt_start(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    client_id, client_secret, redirect_uri = _require_trakt_settings()
    result = await db.execute(
        select(Integration).where(
            Integration.user_id == current_user.id,
            Integration.provider == "trakt",
        )
    )
    integration = result.scalars().first()
    if not integration:
        integration = Integration(
            user_id=current_user.id,
            provider="trakt",
            status="pending",
        )
    state = secrets.token_urlsafe(16)
    expires_at = datetime.now(timezone.utc) + timedelta(minutes=10)
    config = dict(integration.config or {})
    config["oauth_state"] = state
    config["oauth_state_expires_at"] = expires_at.isoformat()
    integration.config = config
    integration.status = "pending"
    db.add(integration)
    await db.commit()
    client = TraktClient(
        client_id=client_id,
        client_secret=client_secret,
        authorize_url=TRAKT_OAUTH_AUTHORIZE_URL,
    )
    redirect_url = client.build_authorize_url(redirect_uri, state)
    return RedirectResponse(url=redirect_url)


@router.get(
    "/trakt/callback",
    summary="Trakt OAuth callback",
    description="Handle the Trakt OAuth callback and store tokens.",
)
async def trakt_callback(
    code: str | None = None,
    state: str | None = None,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    if not code or not state:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Missing OAuth code or state",
        )
    client_id, client_secret, redirect_uri = _require_trakt_settings()
    integration, _ = await _load_trakt_integration(db, current_user.id)
    if not integration:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Trakt integration not initialized",
        )
    config = dict(integration.config or {})
    stored_state = config.get("oauth_state")
    stored_expires = parse_expires_at(config.get("oauth_state_expires_at"))
    if stored_state is None or stored_state != state:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid OAuth state",
        )
    if stored_expires and stored_expires < datetime.now(timezone.utc):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="OAuth state expired",
        )
    client = TraktClient(client_id=client_id, client_secret=client_secret)
    try:
        token = await client.exchange_code(code, redirect_uri)
    except TraktError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)
        ) from exc

    secret_payload = token_to_secret_payload(token)
    encrypted = encrypt_value(json.dumps(secret_payload))
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

    config.pop("oauth_state", None)
    config.pop("oauth_state_expires_at", None)
    integration.status = "connected"
    integration.config = config

    try:
        me_payload = await client.fetch_me(token.access_token)
    except TraktError:
        me_payload = None
    if isinstance(me_payload, dict):
        username = me_payload.get("username")
        if isinstance(username, str) and username.strip():
            config["trakt_username"] = username.strip()
            integration.config = config
    db.add(integration)
    await db.commit()
    return RedirectResponse(url="/static/integrations.html")


@router.post(
    "/trakt/disconnect",
    summary="Disconnect Trakt",
    description="Remove stored Trakt tokens for the current user.",
)
async def trakt_disconnect(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    result = await db.execute(
        select(Integration).where(
            Integration.user_id == current_user.id,
            Integration.provider == "trakt",
        )
    )
    integration = result.scalars().first()
    if not integration:
        return {"status": "ok"}
    result = await db.execute(
        select(IntegrationSecret).where(
            IntegrationSecret.integration_id == integration.id
        )
    )
    secret = result.scalars().first()
    if secret:
        await db.delete(secret)
    integration.status = "disconnected"
    config = dict(integration.config or {})
    config.pop("trakt_username", None)
    config.pop("oauth_state", None)
    config.pop("oauth_state_expires_at", None)
    integration.config = config
    db.add(integration)
    await db.commit()
    return {"status": "ok"}


@router.get(
    "/simkl/start",
    summary="Start SIMKL OAuth (stub)",
    description="Placeholder for initiating the SIMKL OAuth flow.",
)
async def simkl_start():
    raise HTTPException(status_code=501, detail="Not implemented")


@router.get(
    "/simkl/callback",
    summary="SIMKL OAuth callback (stub)",
    description="Placeholder for handling the SIMKL OAuth callback.",
)
async def simkl_callback():
    raise HTTPException(status_code=501, detail="Not implemented")


@router.post(
    "/simkl/disconnect",
    summary="Disconnect SIMKL (stub)",
    description="Placeholder for disconnecting the SIMKL integration.",
)
async def simkl_disconnect():
    raise HTTPException(status_code=501, detail="Not implemented")
