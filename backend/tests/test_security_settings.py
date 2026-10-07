import asyncio
from dataclasses import replace

import pytest
from fastapi import HTTPException
from librarysync import config
from librarysync.api import deps
from librarysync.core import security


@pytest.fixture
def use_settings(monkeypatch):
    def _apply(**overrides):
        updated = replace(config.settings, **overrides)
        monkeypatch.setattr(security, "settings", updated)
        monkeypatch.setattr(deps, "settings", updated)
        return updated

    return _apply


def test_placeholder_secret_key_refuses_to_start(use_settings):
    use_settings(secret_key="change_me", allow_insecure_secret_key=False)
    with pytest.raises(RuntimeError, match="placeholder"):
        security.validate_security_settings()


def test_placeholder_secret_key_can_be_explicitly_allowed(use_settings):
    use_settings(secret_key="change_me", allow_insecure_secret_key=True, admin_api_key=None)
    security.validate_security_settings()


def test_missing_secret_key_refuses_to_start(use_settings):
    use_settings(secret_key="")
    with pytest.raises(RuntimeError, match="not set"):
        security.validate_security_settings()


def test_strong_secret_key_passes(use_settings):
    use_settings(secret_key="x" * 64, admin_api_key="a" * 40)
    security.validate_security_settings()


def test_previous_key_still_decrypts_and_rotates_to_current(use_settings):
    use_settings(secret_key="old-key", secret_key_previous=())
    stored = security.encrypt_value("credentials")

    use_settings(secret_key="new-strong-key", secret_key_previous=("old-key",))
    assert security.decrypt_value(stored) == "credentials"
    rotated = security.rotate_encrypted_value(stored)
    assert rotated is not None
    assert security.rotate_encrypted_value(rotated) is None

    use_settings(secret_key="new-strong-key", secret_key_previous=())
    assert security.decrypt_value(rotated) == "credentials"
    with pytest.raises(ValueError):
        security.decrypt_value(stored)


def test_placeholder_admin_key_disables_admin_api(use_settings):
    use_settings(admin_api_key="your_admin_api_key")
    with pytest.raises(HTTPException) as excinfo:
        asyncio.run(deps.get_admin_api_key("your_admin_api_key"))
    assert excinfo.value.status_code == 503


def test_admin_key_must_match(use_settings):
    use_settings(admin_api_key="a-real-admin-key")
    assert asyncio.run(deps.get_admin_api_key("a-real-admin-key")) == "a-real-admin-key"
    with pytest.raises(HTTPException) as excinfo:
        asyncio.run(deps.get_admin_api_key("a-real-admin-kez"))
    assert excinfo.value.status_code == 401
