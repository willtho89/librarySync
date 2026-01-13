import json
import secrets
from datetime import datetime, timedelta, timezone
from typing import Callable, Literal

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import RedirectResponse
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from librarysync.api.deps import get_current_user, get_db
from librarysync.config import settings
from librarysync.connectors.services.aiostreams_proxy import (
    AIOStreamsClient,
    AIOStreamsError,
    has_required_aiostreams_fields,
)
from librarysync.connectors.services.anilist import (
    AniListClient,
    AniListError,
)
from librarysync.connectors.services.anilist import (
    build_oauth_url as build_anilist_oauth_url,
)
from librarysync.connectors.services.anilist import (
    exchange_code_for_token as exchange_anilist_code,
)
from librarysync.connectors.services.anilist import (
    parse_expires_at as parse_anilist_expires_at,
)
from librarysync.connectors.services.anilist import (
    token_to_secret_payload as anilist_token_to_secret_payload,
)
from librarysync.connectors.services.letterboxd import (
    DEFAULT_LETTERBOXD_API_BASE_URL,
    LetterboxdClient,
    LetterboxdError,
    extract_member_id,
    extract_member_name,
    extract_watchlist_list_id,
    has_required_letterboxd_fields,
)
from librarysync.connectors.services.simkl import (
    SIMKL_OAUTH_AUTHORIZE_URL,
    SimklClient,
    SimklError,
)
from librarysync.connectors.services.simkl import (
    parse_expires_at as parse_simkl_expires_at,
)
from librarysync.connectors.services.simkl import (
    token_to_secret_payload as simkl_token_to_secret_payload,
)
from librarysync.connectors.services.stremio import (
    DEFAULT_STREMIO_API_BASE_URL,
    StremioClient,
    StremioError,
)
from librarysync.connectors.services.trakt import (
    TRAKT_OAUTH_AUTHORIZE_URL,
    TraktClient,
    TraktError,
    parse_expires_at,
    token_to_secret_payload,
)
from librarysync.core.import_all import (
    IMPORT_ALL_PROVIDER,
    build_import_all_config,
    build_import_all_queue,
    get_or_create_system_integration,
    import_all_active,
    set_import_queue_order,
)
from librarysync.core.import_control import (
    build_quick_import_config,
    mark_merge_required,
    quick_import_active,
    set_quick_import_interval,
)
from librarysync.core.import_schedule import normalize_interval_seconds
from librarysync.core.integrations import load_integration_with_secrets
from librarysync.core.security import decrypt_value, encrypt_value
from librarysync.core.watchlist import WATCHLIST_IMPORT_KEY, parse_watchlist_import_config
from librarysync.db.models import Integration, IntegrationSecret, User

router = APIRouter(
    prefix="/api/integrations",
    tags=["integrations"],
)


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


class StremioLoginConfig(BaseModel):
    email: str
    password: str
    api_base_url: str | None = None


class AIOStreamsConfig(BaseModel):
    api_base_url: str | None = None
    auth: str
    username: str | None = None


class QuickImportScheduleIn(BaseModel):
    interval_seconds: int | None = None


class ImportQueueOrderIn(BaseModel):
    order: list[str]


class WatchlistImportConfigIn(BaseModel):
    enabled: bool | None = None
    include_personal: bool | None = None
    list_urls: list[str] | None = None


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


def _merge_watchlist_import_config(
    config: dict | None,
    payload: WatchlistImportConfigIn,
) -> dict:
    current = parse_watchlist_import_config(config)
    updated = {
        "enabled": current.enabled,
        "include_personal": current.include_personal,
        "lists": list(current.list_urls),
    }
    fields = payload.model_fields_set
    if "enabled" in fields:
        updated["enabled"] = bool(payload.enabled)
    if "include_personal" in fields:
        updated["include_personal"] = bool(payload.include_personal)
    if "list_urls" in fields:
        raw = payload.list_urls or []
        updated["lists"] = [str(entry).strip() for entry in raw if str(entry).strip()]
    return updated


async def _get_integration(
    db: AsyncSession,
    user_id: str,
    provider: str,
) -> Integration | None:
    result = await db.execute(
        select(Integration).where(
            Integration.user_id == user_id,
            Integration.provider == provider,
        )
    )
    return result.scalars().first()


async def _load_integration(
    db: AsyncSession, user_id: str, provider: str
) -> tuple[Integration | None, dict[str, object] | None]:
    return await load_integration_with_secrets(db, user_id, provider)


async def _delete_integration_secret(
    db: AsyncSession, integration: Integration
) -> None:
    result = await db.execute(
        select(IntegrationSecret).where(
            IntegrationSecret.integration_id == integration.id
        )
    )
    secret = result.scalars().first()
    if secret:
        await db.delete(secret)


async def _upsert_integration_secret(
    db: AsyncSession, integration: Integration, payload: dict[str, object]
) -> None:
    encrypted = encrypt_value(json.dumps(payload))
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


def _set_oauth_state(config: dict[str, object]) -> str:
    state = secrets.token_urlsafe(16)
    expires_at = datetime.now(timezone.utc) + timedelta(minutes=10)
    config["oauth_state"] = state
    config["oauth_state_expires_at"] = expires_at.isoformat()
    return state


def _clear_oauth_state(config: dict[str, object]) -> None:
    config.pop("oauth_state", None)
    config.pop("oauth_state_expires_at", None)


def _validate_oauth_state(
    config: dict[str, object],
    state: str,
    parse_expires: Callable[[str | None], datetime | None],
) -> None:
    stored_state = config.get("oauth_state")
    stored_expires = parse_expires(config.get("oauth_state_expires_at"))
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


async def _start_oauth_flow(
    *,
    db: AsyncSession,
    user_id: str,
    provider: str,
    build_url: Callable[[str], str],
) -> RedirectResponse:
    integration = await _get_integration(db, user_id, provider)
    if not integration:
        integration = Integration(
            user_id=user_id,
            provider=provider,
            status="pending",
        )
    config = dict(integration.config or {})
    state = _set_oauth_state(config)
    integration.config = config
    integration.status = "pending"
    try:
        redirect_url = build_url(state)
        db.add(integration)
        await db.commit()
    except Exception:
        await db.rollback()
        raise
    return RedirectResponse(url=redirect_url)


async def _disconnect_integration(
    db: AsyncSession,
    user_id: str,
    provider: str,
    cleanup: Callable[[dict[str, object]], None] | None = None,
) -> dict:
    integration = await _get_integration(db, user_id, provider)
    if not integration:
        return {"status": "ok"}
    try:
        await _delete_integration_secret(db, integration)
        integration.status = "disconnected"
        config = dict(integration.config or {})
        if cleanup:
            cleanup(config)
        integration.config = config
        db.add(integration)
        await db.commit()
    except Exception:
        await db.rollback()
        raise
    return {"status": "ok"}


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


def _require_simkl_settings() -> tuple[str, str, str]:
    if not settings.simkl_client_id or not settings.simkl_client_secret:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="SIMKL client ID/secret are not configured",
        )
    if not settings.base_url:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="LIBRARYSYNC_BASE_URL must be set for SIMKL OAuth",
        )
    base_url = settings.base_url.rstrip("/")
    redirect_uri = f"{base_url}/api/integrations/simkl/callback"
    return settings.simkl_client_id, settings.simkl_client_secret, redirect_uri


def _require_anilist_settings() -> tuple[str, str, str]:
    if not settings.anilist_client_id or not settings.anilist_client_secret:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="AniList client ID/secret are not configured",
        )
    if not settings.base_url:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="LIBRARYSYNC_BASE_URL must be set for AniList OAuth",
        )
    base_url = settings.base_url.rstrip("/")
    redirect_uri = f"{base_url}/api/integrations/anilist/callback"
    return settings.anilist_client_id, settings.anilist_client_secret, redirect_uri


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
    integrations = [
        integration
        for integration in result.scalars().all()
        if integration.provider != IMPORT_ALL_PROVIDER
    ]
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
    "/{provider}/watchlist",
    summary="Update watchlist import settings",
    description="Update watchlist import settings for a provider.",
)
async def update_watchlist_import_settings(
    provider: Literal["trakt", "simkl", "letterboxd"],
    payload: WatchlistImportConfigIn,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    integration = await _get_integration(db, current_user.id, provider)
    if not integration:
        integration = Integration(
            user_id=current_user.id,
            provider=provider,
            status="configured",
            config={},
        )
    config = dict(integration.config or {})
    config[WATCHLIST_IMPORT_KEY] = _merge_watchlist_import_config(config, payload)
    integration.config = config
    db.add(integration)
    await db.commit()
    return {"provider": provider, "watchlist_import": config[WATCHLIST_IMPORT_KEY]}


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
    integration = await _get_integration(db, current_user.id, "letterboxd")
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
    credentials_changed = False
    if client_id is not None and client_id != secret_data.get("client_id"):
        credentials_changed = True
    if client_secret is not None and client_secret != secret_data.get("client_secret"):
        credentials_changed = True
    if refresh_token is not None and refresh_token != secret_data.get("refresh_token"):
        credentials_changed = True

    updated: dict[str, object] = dict(secret_data)
    if client_id is not None:
        updated["client_id"] = client_id
    if client_secret is not None:
        updated["client_secret"] = client_secret
    if refresh_token is not None:
        updated["refresh_token"] = refresh_token
    if cookies is not None:
        updated["cookies"] = cookies
    if credentials_changed:
        updated.pop("access_token", None)
        updated.pop("expires_at", None)
        updated.pop("token_type", None)

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
        await _upsert_integration_secret(db, integration, updated)
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
    integration, secret_data = await _load_integration(db, current_user.id, "letterboxd")
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
    member_name = extract_member_name(me_payload)
    watchlist_list_id = extract_watchlist_list_id(me_payload)
    if member_id:
        config = dict(integration.config or {})
        if config.get("member_id") != member_id:
            config["member_id"] = member_id
        if member_name and config.get("member_name") != member_name:
            config["member_name"] = member_name
        if watchlist_list_id and config.get("watchlist_list_id") != watchlist_list_id:
            config["watchlist_list_id"] = watchlist_list_id
        integration.config = config
        db.add(integration)
        await db.commit()

    return {"status": "ok"}


@router.post(
    "/letterboxd/disconnect",
    summary="Disconnect Letterboxd",
    description="Remove stored Letterboxd credentials for the current user.",
)
async def letterboxd_disconnect(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    return await _disconnect_integration(
        db,
        current_user.id,
        "letterboxd",
        cleanup=_clear_letterboxd_profile,
    )


@router.post(
    "/import/quick/schedule",
    summary="Configure quick import schedule",
    description="Update the quick import interval for the current user.",
)
async def update_quick_import_schedule(
    payload: QuickImportScheduleIn,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    if payload.interval_seconds is not None and payload.interval_seconds < 0:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Interval must be a positive number of seconds",
        )
    integration = await get_or_create_system_integration(db, current_user.id)
    interval_seconds = normalize_interval_seconds(payload.interval_seconds)
    integration.config = set_quick_import_interval(integration.config, interval_seconds)
    integration.status = "system"
    db.add(integration)
    await db.commit()
    return {"status": "ok", "interval_seconds": interval_seconds}


@router.get(
    "/import/queue",
    summary="Get import queue order",
    description="Return the current import priority order for the user.",
)
async def get_import_queue_order(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    queue = await build_import_all_queue(db, current_user.id)
    return {"queue": queue}


@router.post(
    "/import/queue",
    summary="Update import queue order",
    description="Set the provider priority order used for imports.",
)
async def update_import_queue_order(
    payload: ImportQueueOrderIn,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    integration = await get_or_create_system_integration(db, current_user.id)
    integration.config = set_import_queue_order(integration.config, payload.order)
    integration.status = "system"
    db.add(integration)
    await db.commit()
    queue = await build_import_all_queue(db, current_user.id)
    return {"status": "ok", "queue": queue}


@router.post(
    "/import/quick",
    summary="Trigger a quick history import",
    description="Queue a 7-day import across configured integrations.",
)
async def trigger_quick_import(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    integration = await get_or_create_system_integration(db, current_user.id)
    if import_all_active(integration.config) or quick_import_active(integration.config):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Import is already in progress",
        )
    queue = await build_import_all_queue(db, current_user.id)
    if not queue:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="No integrations are ready for import",
        )
    now = datetime.now(timezone.utc)
    integration.status = "system"
    config = build_quick_import_config(
        integration.config,
        queue,
        now,
    )
    integration.config = mark_merge_required(config, now)
    db.add(integration)
    await db.commit()
    return {"status": "queued", "providers": queue}


@router.post(
    "/import/all",
    summary="Trigger a priority-ordered history import",
    description="Queue a full import sequence across configured integrations.",
)
async def trigger_import_all(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    integration = await get_or_create_system_integration(db, current_user.id)
    if import_all_active(integration.config) or quick_import_active(integration.config):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Import is already in progress",
        )
    queue = await build_import_all_queue(db, current_user.id)
    if not queue:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="No integrations are ready for import",
        )
    now = datetime.now(timezone.utc)
    integration.status = "system"
    config = build_import_all_config(
        integration.config,
        queue,
        now,
    )
    integration.config = mark_merge_required(config, now)
    db.add(integration)
    await db.commit()
    return {"status": "queued", "providers": queue}


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
    client = TraktClient(
        client_id=client_id,
        client_secret=client_secret,
        authorize_url=TRAKT_OAUTH_AUTHORIZE_URL,
    )
    return await _start_oauth_flow(
        db=db,
        user_id=current_user.id,
        provider="trakt",
        build_url=lambda state: client.build_authorize_url(redirect_uri, state),
    )


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
    integration, _ = await _load_integration(db, current_user.id, "trakt")
    if not integration:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Trakt integration not initialized",
        )
    config = dict(integration.config or {})
    _validate_oauth_state(config, state, parse_expires_at)
    client = TraktClient(client_id=client_id, client_secret=client_secret)
    try:
        token = await client.exchange_code(code, redirect_uri)
    except TraktError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)
        ) from exc

    secret_payload = token_to_secret_payload(token)
    await _upsert_integration_secret(db, integration, secret_payload)

    _clear_oauth_state(config)
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
    return RedirectResponse(url="/settings")


@router.post(
    "/trakt/disconnect",
    summary="Disconnect Trakt",
    description="Remove stored Trakt tokens for the current user.",
)
async def trakt_disconnect(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    return await _disconnect_integration(
        db,
        current_user.id,
        "trakt",
        cleanup=_clear_trakt_profile,
    )


@router.get(
    "/simkl/start",
    summary="Start SIMKL OAuth",
    description="Initiate the SIMKL OAuth flow for the current user.",
)
async def simkl_start(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    client_id, client_secret, redirect_uri = _require_simkl_settings()
    client = SimklClient(
        client_id=client_id,
        client_secret=client_secret,
        authorize_url=SIMKL_OAUTH_AUTHORIZE_URL,
    )
    return await _start_oauth_flow(
        db=db,
        user_id=current_user.id,
        provider="simkl",
        build_url=lambda state: client.build_authorize_url(redirect_uri, state),
    )


@router.get(
    "/simkl/callback",
    summary="SIMKL OAuth callback",
    description="Handle the SIMKL OAuth callback and store tokens.",
)
async def simkl_callback(
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
    client_id, client_secret, redirect_uri = _require_simkl_settings()
    integration, _ = await _load_integration(db, current_user.id, "simkl")
    if not integration:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="SIMKL integration not initialized",
        )
    config = dict(integration.config or {})
    _validate_oauth_state(config, state, parse_simkl_expires_at)
    client = SimklClient(client_id=client_id, client_secret=client_secret)
    try:
        token = await client.exchange_code(code, redirect_uri)
    except SimklError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail=_format_simkl_error(exc)
        ) from exc

    secret_payload = simkl_token_to_secret_payload(token)
    await _upsert_integration_secret(db, integration, secret_payload)

    _clear_oauth_state(config)
    integration.status = "connected"
    integration.config = config

    try:
        me_payload = await client.fetch_me(token.access_token)
    except SimklError:
        me_payload = None
    username = _extract_simkl_username(me_payload)
    if username:
        config["simkl_username"] = username
        integration.config = config
    db.add(integration)
    await db.commit()
    return RedirectResponse(url="/settings")


@router.post(
    "/simkl/disconnect",
    summary="Disconnect SIMKL",
    description="Remove stored SIMKL tokens for the current user.",
)
async def simkl_disconnect(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    return await _disconnect_integration(
        db,
        current_user.id,
        "simkl",
        cleanup=_clear_simkl_profile,
    )


@router.get(
    "/anilist/start",
    summary="Start AniList OAuth",
    description="Initiate the AniList OAuth flow for the current user.",
)
async def anilist_start(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    client_id, _client_secret, redirect_uri = _require_anilist_settings()
    return await _start_oauth_flow(
        db=db,
        user_id=current_user.id,
        provider="anilist",
        build_url=lambda state: build_anilist_oauth_url(client_id, redirect_uri, state),
    )


@router.get(
    "/anilist/callback",
    summary="AniList OAuth callback",
    description="Handle the AniList OAuth callback and store tokens.",
)
async def anilist_callback(
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
    client_id, client_secret, redirect_uri = _require_anilist_settings()
    integration, _ = await _load_integration(db, current_user.id, "anilist")
    if not integration:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="AniList integration not initialized",
        )
    config = dict(integration.config or {})
    _validate_oauth_state(config, state, parse_anilist_expires_at)

    try:
        token = await exchange_anilist_code(code, client_id, client_secret, redirect_uri)
    except AniListError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"AniList OAuth error: {exc}",
        ) from exc

    try:
        client = AniListClient(access_token=token.access_token)
        viewer = await client.get_viewer()
        anilist_username = viewer.get("name", "")
    except AniListError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Failed to get AniList user info: {exc}",
        ) from exc
    token_payload = anilist_token_to_secret_payload(token)
    await _upsert_integration_secret(db, integration, token_payload)

    integration.status = "active"
    config = dict(integration.config or {})
    config["anilist_username"] = anilist_username
    _clear_oauth_state(config)
    integration.config = config
    db.add(integration)

    await db.commit()

    return RedirectResponse(url="/settings?anilist=connected")


@router.post(
    "/anilist/disconnect",
    summary="Disconnect AniList",
    description="Remove stored AniList tokens for the current user.",
)
async def anilist_disconnect(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    return await _disconnect_integration(
        db,
        current_user.id,
        "anilist",
        cleanup=_clear_anilist_profile,
    )


@router.post(
    "/aiostreams",
    summary="Connect AIOStreams Proxy",
    description="Store AIOStreams Proxy auth for the current user.",
)
async def aiostreams_connect(
    payload: AIOStreamsConfig,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    auth = _normalize_optional(payload.auth)
    if not auth:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="Auth token is required"
        )
    api_base_url = _normalize_optional(payload.api_base_url)
    username = _normalize_optional(payload.username)

    integration = await _get_integration(db, current_user.id, "aiostreams")
    existing_base = None
    if integration and integration.config:
        existing_base = integration.config.get("api_base_url")
    if not api_base_url:
        api_base_url = existing_base
    if not api_base_url:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="API base URL is required",
        )
    if not username:
        username = _parse_aiostreams_username(auth)

    if not integration:
        integration = Integration(
            user_id=current_user.id,
            provider="aiostreams",
            status="connected",
        )

    config = dict(integration.config or {})
    config["api_base_url"] = api_base_url
    if username:
        config["username"] = username
    else:
        config.pop("username", None)
    integration.status = "connected"
    integration.config = config
    db.add(integration)
    await db.flush()

    secret_payload = {"auth": auth}
    await _upsert_integration_secret(db, integration, secret_payload)

    await db.commit()
    return _integration_to_out(integration, True).model_dump()


@router.post(
    "/aiostreams/test",
    summary="Test AIOStreams Proxy",
    description="Verify access to the AIOStreams stats endpoint.",
)
async def aiostreams_test(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    integration, secret_data = await _load_integration(db, current_user.id, "aiostreams")
    if not integration or not secret_data:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="AIOStreams integration not configured",
        )
    if not has_required_aiostreams_fields(secret_data):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="AIOStreams auth is missing",
        )
    api_base_url = None
    if integration.config and integration.config.get("api_base_url"):
        api_base_url = str(integration.config["api_base_url"])
    if not api_base_url:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="AIOStreams API base URL is missing",
        )
    auth = str(secret_data.get("auth"))
    username = None
    if integration.config and integration.config.get("username"):
        username = str(integration.config["username"])
    if not username:
        username = _parse_aiostreams_username(auth)
    client = AIOStreamsClient(api_base_url=api_base_url)
    try:
        stats = await client.get_stats(auth)
    except AIOStreamsError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)
        ) from exc
    if username:
        users = stats.get("users") if isinstance(stats, dict) else None
        if not isinstance(users, dict) or username not in users:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="AIOStreams user not found in stats response",
            )
    return {"status": "ok"}


@router.post(
    "/aiostreams/disconnect",
    summary="Disconnect AIOStreams Proxy",
    description="Remove stored AIOStreams auth for the current user.",
)
async def aiostreams_disconnect(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    return await _disconnect_integration(
        db,
        current_user.id,
        "aiostreams",
        cleanup=_clear_aiostreams_profile,
    )


@router.post(
    "/stremio/login",
    summary="Connect Stremio",
    description="Login to Stremio and store the auth key for the current user.",
)
async def stremio_login(
    payload: StremioLoginConfig,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    email = payload.email.strip()
    password = payload.password
    if not email:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="Email is required"
        )
    if not password or not password.strip():
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="Password is required"
        )
    api_base_url = _normalize_optional(payload.api_base_url)

    integration = await _get_integration(db, current_user.id, "stremio")
    existing_base = None
    if integration and integration.config:
        existing_base = integration.config.get("api_base_url")
    if not api_base_url:
        api_base_url = existing_base or DEFAULT_STREMIO_API_BASE_URL

    client = StremioClient(api_base_url=api_base_url)
    try:
        login = await client.login(email, password)
    except StremioError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail=_format_stremio_error(exc)
        ) from exc

    if not integration:
        integration = Integration(
            user_id=current_user.id,
            provider="stremio",
            status="connected",
        )

    config = dict(integration.config or {})
    config["api_base_url"] = api_base_url
    _clear_stremio_profile(config)
    _apply_stremio_profile(config, login.user, email)
    integration.status = "connected"
    integration.config = config
    db.add(integration)
    await db.flush()

    secret_payload = {"auth_key": login.auth_key}
    await _upsert_integration_secret(db, integration, secret_payload)

    await db.commit()
    return _integration_to_out(integration, True).model_dump()


@router.post(
    "/stremio/disconnect",
    summary="Disconnect Stremio",
    description="Remove stored Stremio auth key for the current user.",
)
async def stremio_disconnect(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    return await _disconnect_integration(
        db,
        current_user.id,
        "stremio",
        cleanup=_clear_stremio_profile,
    )


def _extract_simkl_username(payload: object) -> str | None:
    if not isinstance(payload, dict):
        return None
    for key in ("username", "login", "name"):
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    user = payload.get("user")
    if isinstance(user, dict):
        for key in ("username", "login", "name"):
            value = user.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
    return None


def _format_simkl_error(error: SimklError) -> str:
    message = str(error)
    status_code = error.status_code
    response_body = error.response_body
    if response_body:
        response_body = _shorten_text(response_body)
    if status_code and response_body:
        return f"{message} (status={status_code}, body={response_body})"
    if status_code:
        return f"{message} (status={status_code})"
    if response_body:
        return f"{message} (body={response_body})"
    return message


def _shorten_text(value: str, limit: int = 300) -> str:
    trimmed = value.strip()
    if len(trimmed) > limit:
        return f"{trimmed[:limit]}..."
    return trimmed


def _clear_letterboxd_profile(config: dict[str, object]) -> None:
    config.pop("member_id", None)
    config.pop("member_name", None)


def _clear_trakt_profile(config: dict[str, object]) -> None:
    config.pop("trakt_username", None)
    _clear_oauth_state(config)


def _clear_simkl_profile(config: dict[str, object]) -> None:
    config.pop("simkl_username", None)
    _clear_oauth_state(config)


def _clear_anilist_profile(config: dict[str, object]) -> None:
    config.pop("anilist_username", None)
    _clear_oauth_state(config)


def _clear_aiostreams_profile(config: dict[str, object]) -> None:
    config.pop("username", None)


def _apply_stremio_profile(
    config: dict[str, object],
    user_payload: object,
    fallback_email: str | None = None,
) -> None:
    user: dict[str, object] = {}
    if isinstance(user_payload, dict):
        user = {str(key): value for key, value in user_payload.items()}

    user_id = _coerce_stremio_field(user.get("_id")) or _coerce_stremio_field(
        user.get("id")
    )
    if user_id:
        config["stremio_user_id"] = user_id

    email = _coerce_stremio_field(user.get("email")) or fallback_email
    if email:
        config["stremio_email"] = email

    name = _coerce_stremio_field(user.get("fullname"))
    if name:
        config["stremio_name"] = name


def _clear_stremio_profile(config: dict[str, object]) -> None:
    for key in ("stremio_user_id", "stremio_email", "stremio_name"):
        config.pop(key, None)


def _coerce_stremio_field(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    cleaned = value.strip()
    return cleaned or None


def _format_stremio_error(error: StremioError) -> str:
    message = str(error)
    status_code = error.status_code
    code = error.code
    response_body = error.response_body
    if response_body:
        response_body = _shorten_text(response_body)
    if code and status_code and response_body:
        return f"{message} (code={code}, status={status_code}, body={response_body})"
    if code and status_code:
        return f"{message} (code={code}, status={status_code})"
    if status_code and response_body:
        return f"{message} (status={status_code}, body={response_body})"
    if code:
        return f"{message} (code={code})"
    if status_code:
        return f"{message} (status={status_code})"
    if response_body:
        return f"{message} (body={response_body})"
    return message


def _parse_aiostreams_username(auth: str | None) -> str | None:
    if not isinstance(auth, str):
        return None
    cleaned = auth.strip()
    if not cleaned or ":" not in cleaned:
        return None
    return cleaned.split(":", 1)[0].strip() or None
