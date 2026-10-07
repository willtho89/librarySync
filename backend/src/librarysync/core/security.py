"""Security helpers for encrypting secrets at rest."""

import base64
import hashlib
import logging

from cryptography.fernet import Fernet, InvalidToken, MultiFernet

from librarysync.config import settings

logger = logging.getLogger(__name__)

# Values shipped in .env.example / docs; anyone can forge sessions and decrypt
# stored credentials when one of these is used as the secret key.
PLACEHOLDER_SECRETS = {"change_me", "changeme", "change-me", "secret", "your_secret_key"}
PLACEHOLDER_ADMIN_KEYS = {"your_admin_api_key", "change_me", "changeme", "change-me"}
RECOMMENDED_SECRET_KEY_LENGTH = 32


def _fernet_for(secret: str) -> Fernet:
    digest = hashlib.sha256(secret.encode("utf-8")).digest()
    return Fernet(base64.urlsafe_b64encode(digest))


def _get_fernet() -> MultiFernet:
    """Encrypt with the current key; decrypt with it or any LIBRARYSYNC_SECRET_KEY_PREVIOUS key."""
    if not settings.secret_key:
        raise RuntimeError("LIBRARYSYNC_SECRET_KEY is not set")
    keys = [settings.secret_key, *settings.secret_key_previous]
    return MultiFernet([_fernet_for(key) for key in keys])


def encrypt_value(value: str) -> str:
    token = _get_fernet().encrypt(value.encode("utf-8"))
    return token.decode("utf-8")


def decrypt_value(value: str) -> str:
    try:
        plain = _get_fernet().decrypt(value.encode("utf-8"))
    except InvalidToken as exc:
        raise ValueError("Invalid encrypted value") from exc
    return plain.decode("utf-8")


def rotate_encrypted_value(value: str) -> str | None:
    """Return the value re-encrypted under the current key, or None if it already is."""
    token = value.encode("utf-8")
    try:
        _fernet_for(settings.secret_key or "").decrypt(token)
        return None
    except InvalidToken:
        pass
    try:
        return _get_fernet().rotate(token).decode("utf-8")
    except InvalidToken as exc:
        raise ValueError("Invalid encrypted value") from exc


def is_placeholder_admin_key(value: str | None) -> bool:
    return bool(value) and value.strip().lower() in PLACEHOLDER_ADMIN_KEYS


def validate_security_settings() -> None:
    """Refuse to start with a missing or well-known secret key; warn about weak ones."""
    secret = (settings.secret_key or "").strip()
    if not secret:
        raise RuntimeError("LIBRARYSYNC_SECRET_KEY is not set. Generate one with: openssl rand -hex 32")
    if secret.lower() in PLACEHOLDER_SECRETS:
        message = (
            "LIBRARYSYNC_SECRET_KEY is a published placeholder: anyone could forge logins and decrypt "
            "stored provider credentials. Generate a new key with `openssl rand -hex 32` and move the old "
            "value to LIBRARYSYNC_SECRET_KEY_PREVIOUS so stored credentials are re-encrypted."
        )
        if not settings.allow_insecure_secret_key:
            raise RuntimeError(message + " Set LIBRARYSYNC_ALLOW_INSECURE_SECRET_KEY=true to override.")
        logger.warning(message)
    elif len(secret) < RECOMMENDED_SECRET_KEY_LENGTH:
        logger.warning(
            "LIBRARYSYNC_SECRET_KEY is shorter than %s characters; consider rotating it",
            RECOMMENDED_SECRET_KEY_LENGTH,
        )
    if is_placeholder_admin_key(settings.admin_api_key):
        logger.warning("LIBRARYSYNC_ADMIN_API_KEY is a placeholder; admin endpoints are disabled")
