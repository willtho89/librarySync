"""Serialized OAuth token refresh for provider integrations.

Trakt and Letterboxd rotate refresh tokens on every refresh, so two workers
refreshing the same integration at once would leave one of them holding a
revoked token. Refreshes lock the integration secret row, re-read it, and only
call the provider when no other worker has refreshed in the meantime. The new
token is committed straight away so a later rollback of the caller's work
cannot lose it.
"""

import json
from collections.abc import Awaitable, Callable
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from librarysync.connectors.services import letterboxd, simkl, trakt
from librarysync.core.security import decrypt_value, encrypt_value
from librarysync.db.models import Integration, IntegrationSecret

SecretData = dict[str, object]


def _decode(secret: IntegrationSecret) -> SecretData | None:
    try:
        data = json.loads(decrypt_value(secret.secret_data))
    except (ValueError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict):
        return None
    return {str(key): value for key, value in data.items()}


async def _lock_secret(db: AsyncSession, integration_id: str) -> IntegrationSecret | None:
    result = await db.execute(
        select(IntegrationSecret)
        .where(IntegrationSecret.integration_id == integration_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    return result.scalars().first()


async def save_integration_secret(db: AsyncSession, integration_id: str, secret_data: SecretData) -> None:
    encrypted = encrypt_value(json.dumps(secret_data))
    result = await db.execute(select(IntegrationSecret).where(IntegrationSecret.integration_id == integration_id))
    secret = result.scalars().first()
    if not secret:
        secret = IntegrationSecret(integration_id=integration_id, secret_data=encrypted)
    else:
        secret.secret_data = encrypted
    db.add(secret)


async def ensure_fresh_secret(
    db: AsyncSession,
    integration_id: str,
    secret_data: SecretData,
    *,
    is_fresh: Callable[[SecretData], bool],
    refresh: Callable[[SecretData], Awaitable[SecretData]],
) -> SecretData:
    """Return secret data holding a usable access token, refreshing at most once across workers.

    ``refresh`` receives the latest stored secret and returns the fields to merge into it.
    """
    if is_fresh(secret_data):
        return secret_data
    secret = await _lock_secret(db, integration_id)
    current = dict(secret_data)
    if secret is not None:
        current = _decode(secret) or current
        if is_fresh(current):
            await db.commit()
            return current
    try:
        updated = {**current, **(await refresh(current))}
    except Exception:
        # Release the row lock; the caller decides how to report the failure.
        await db.commit()
        raise
    await save_integration_secret(db, integration_id, updated)
    await db.commit()
    return updated


async def expire_access_token(db: AsyncSession, user_id: str, provider: str) -> bool:
    """Force the next delivery to refresh the stored token after the provider rejected it."""
    result = await db.execute(
        select(Integration.id).where(Integration.user_id == user_id, Integration.provider == provider)
    )
    integration_id = result.scalars().first()
    if not integration_id:
        return False
    secret = await _lock_secret(db, integration_id)
    data = _decode(secret) if secret is not None else None
    if not data or not data.get("refresh_token"):
        return False
    data["expires_at"] = datetime(1970, 1, 1, tzinfo=timezone.utc).isoformat()
    secret.secret_data = encrypt_value(json.dumps(data))
    return True


def _has_unexpired_token(
    data: SecretData,
    parse_expires_at: Callable[[object], datetime | None],
    is_expired: Callable[[datetime | None], bool],
) -> bool:
    access_token = data.get("access_token")
    return (
        isinstance(access_token, str)
        and bool(access_token)
        and not is_expired(parse_expires_at(data.get("expires_at")))
    )


async def ensure_trakt_access_token(
    db: AsyncSession, integration_id: str, secret_data: SecretData, client: trakt.TraktClient
) -> str:
    access_token = secret_data.get("access_token")
    refresh_token = secret_data.get("refresh_token")
    if not isinstance(access_token, str) or not access_token:
        raise trakt.TraktError("Trakt access token is missing", status_code=401)
    if not isinstance(refresh_token, str) or not refresh_token:
        raise trakt.TraktError("Trakt refresh token is missing", status_code=401)

    async def _refresh(current: SecretData) -> SecretData:
        token = await client.refresh_access_token(str(current.get("refresh_token") or refresh_token))
        return trakt.token_to_secret_payload(token)

    fresh = await ensure_fresh_secret(
        db,
        integration_id,
        secret_data,
        is_fresh=lambda data: _has_unexpired_token(data, trakt.parse_expires_at, trakt.is_token_expired),
        refresh=_refresh,
    )
    return str(fresh["access_token"])


async def ensure_simkl_access_token(
    db: AsyncSession, integration_id: str, secret_data: SecretData, client: simkl.SimklClient
) -> str:
    access_token = secret_data.get("access_token")
    refresh_token = secret_data.get("refresh_token")
    if not isinstance(access_token, str) or not access_token:
        raise simkl.SimklError("SIMKL access token is missing", status_code=401)
    if not isinstance(refresh_token, str) or not refresh_token:
        # SIMKL issues long-lived tokens without a refresh token.
        return access_token

    async def _refresh(current: SecretData) -> SecretData:
        token = await client.refresh_access_token(str(current.get("refresh_token") or refresh_token))
        return simkl.token_to_secret_payload(token)

    fresh = await ensure_fresh_secret(
        db,
        integration_id,
        secret_data,
        is_fresh=lambda data: _has_unexpired_token(data, simkl.parse_expires_at, simkl.is_token_expired),
        refresh=_refresh,
    )
    return str(fresh["access_token"])


async def ensure_letterboxd_access_token(
    db: AsyncSession, integration_id: str, secret_data: SecretData, client: letterboxd.LetterboxdClient
) -> str:
    async def _refresh(current: SecretData) -> SecretData:
        # Letterboxd rotates refresh tokens; always spend the latest stored one.
        latest_refresh = current.get("refresh_token")
        if isinstance(latest_refresh, str) and latest_refresh:
            client.refresh_token = latest_refresh
        token = await client.refresh_access_token_payload()
        return letterboxd.token_to_secret_payload(token)

    fresh = await ensure_fresh_secret(
        db,
        integration_id,
        secret_data,
        is_fresh=lambda data: _has_unexpired_token(data, letterboxd.parse_expires_at, letterboxd.is_token_expired),
        refresh=_refresh,
    )
    return str(fresh["access_token"])
