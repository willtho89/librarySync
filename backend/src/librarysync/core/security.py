"""Security helpers for encrypting secrets at rest."""

import base64
import hashlib

from cryptography.fernet import Fernet, InvalidToken

from librarysync.config import settings


def _get_fernet() -> Fernet:
    if not settings.secret_key:
        raise RuntimeError("LIBRARYSYNC_SECRET_KEY is not set")
    digest = hashlib.sha256(settings.secret_key.encode("utf-8")).digest()
    key = base64.urlsafe_b64encode(digest)
    return Fernet(key)


def encrypt_value(value: str) -> str:
    token = _get_fernet().encrypt(value.encode("utf-8"))
    return token.decode("utf-8")


def decrypt_value(value: str) -> str:
    try:
        plain = _get_fernet().decrypt(value.encode("utf-8"))
    except InvalidToken as exc:
        raise ValueError("Invalid encrypted value") from exc
    return plain.decode("utf-8")
