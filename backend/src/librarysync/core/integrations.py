import json

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from librarysync.core.security import decrypt_value, rotate_encrypted_value
from librarysync.db.models import Integration, IntegrationSecret


async def load_integration_with_secrets(
    db: AsyncSession, user_id: str, provider: str
) -> tuple[Integration | None, dict[str, object] | None]:
    result = await db.execute(
        select(Integration).where(
            Integration.user_id == user_id, Integration.provider == provider
        )
    )
    integration = result.scalars().first()
    if not integration:
        return None, None
    result = await db.execute(
        select(IntegrationSecret).where(
            IntegrationSecret.integration_id == integration.id
        )
    )
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
    """Re-encrypt stored credentials under the current key after a key rotation."""
    result = await db.execute(select(IntegrationSecret))
    rotated = 0
    for secret in result.scalars().all():
        try:
            updated = rotate_encrypted_value(secret.secret_data)
        except ValueError:
            continue
        if updated is not None:
            secret.secret_data = updated
            rotated += 1
    await db.commit()
    return rotated
