import json

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from librarysync.core.security import decrypt_value, rotate_encrypted_value
from librarysync.db.models import Integration, IntegrationSecret


async def load_integration_with_secrets(
    db: AsyncSession, user_id: str, provider: str
) -> tuple[Integration | None, dict[str, object] | None]:
    result = await db.execute(
        select(Integration).where(Integration.user_id == user_id, Integration.provider == provider)
    )
    integration = result.scalars().first()
    if not integration:
        return None, None
    result = await db.execute(select(IntegrationSecret).where(IntegrationSecret.integration_id == integration.id))
    secret = result.scalars().first()
    if not secret:
        return integration, None
    try:
        data = json.loads(decrypt_value(secret.secret_data))
    except (ValueError, json.JSONDecodeError):
        return integration, None
    if not isinstance(data, dict):
        return integration, None
    return integration, {str(key): value for key, value in data.items()}


async def reencrypt_integration_secrets(db: AsyncSession) -> int:
    """Re-encrypt stored credentials under the current key after a key rotation.

    Each row is re-read and rewritten under the same row lock OAuth refreshes take
    (core/integration_tokens), so a token refreshed concurrently by a worker is never
    replaced by the stale value read before it.
    """
    result = await db.execute(select(IntegrationSecret.id))
    secret_ids = list(result.scalars().all())
    await db.commit()
    rotated = 0
    for secret_id in secret_ids:
        locked = await db.execute(
            select(IntegrationSecret)
            .where(IntegrationSecret.id == secret_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        secret = locked.scalars().first()
        try:
            updated = rotate_encrypted_value(secret.secret_data) if secret is not None else None
        except ValueError:
            updated = None
        if updated is not None:
            secret.secret_data = updated
            rotated += 1
        # Commit per row so each lock is held only for its own rewrite.
        await db.commit()
    return rotated
