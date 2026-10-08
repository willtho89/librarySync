from datetime import datetime, timedelta, timezone
from functools import lru_cache
from typing import Any

import anyio
import bcrypt
import jwt

from librarysync.config import settings

MIN_PASSWORD_LENGTH = 8
# bcrypt only uses the first 72 bytes; longer passwords are rejected rather than truncated.
MAX_PASSWORD_BYTES = 72


def _password_byte_length(password: str) -> int:
    return len(password.encode("utf-8"))


def validate_password(password: str) -> None:
    if len(password) < MIN_PASSWORD_LENGTH:
        raise ValueError(f"Password must be at least {MIN_PASSWORD_LENGTH} characters.")
    if _password_byte_length(password) > MAX_PASSWORD_BYTES:
        raise ValueError(f"Password must be at most {MAX_PASSWORD_BYTES} bytes.")


def hash_password(password: str) -> str:
    validate_password(password)
    hashed = bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt())
    return hashed.decode("utf-8")


def verify_password(password: str, password_hash: str) -> bool:
    try:
        return bcrypt.checkpw(password.encode("utf-8"), password_hash.encode("utf-8"))
    except ValueError:
        return False


@lru_cache(maxsize=1)
def _dummy_password_hash() -> str:
    return bcrypt.hashpw(b"librarysync-timing-equalizer", bcrypt.gensalt()).decode("utf-8")


async def hash_password_async(password: str) -> str:
    """Hash off the event loop; bcrypt deliberately takes hundreds of milliseconds."""
    validate_password(password)
    return await anyio.to_thread.run_sync(hash_password, password)


async def verify_password_async(password: str, password_hash: str | None) -> bool:
    """Verify off the event loop. Unknown users are checked against a dummy hash so
    response timing does not reveal which usernames exist."""
    if password_hash is None:
        await anyio.to_thread.run_sync(verify_password, password, _dummy_password_hash())
        return False
    return await anyio.to_thread.run_sync(verify_password, password, password_hash)


def create_access_token(subject: str, expires_minutes: int | None = None) -> str:
    if not settings.secret_key:
        raise RuntimeError("LIBRARYSYNC_SECRET_KEY is not set")
    expire_minutes = expires_minutes or settings.jwt_access_token_minutes
    expire_at = datetime.now(timezone.utc) + timedelta(minutes=expire_minutes)
    payload = {"sub": subject, "exp": expire_at}
    return jwt.encode(payload, settings.secret_key, algorithm=settings.jwt_algorithm)


def decode_access_token(token: str) -> dict[str, Any] | None:
    if not settings.secret_key:
        raise RuntimeError("LIBRARYSYNC_SECRET_KEY is not set")
    try:
        return jwt.decode(token, settings.secret_key, algorithms=[settings.jwt_algorithm])
    except jwt.PyJWTError:
        return None
