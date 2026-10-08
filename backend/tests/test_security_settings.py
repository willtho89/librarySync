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


@pytest.mark.asyncio
async def test_key_rotation_keeps_a_token_refreshed_after_the_secrets_were_listed(use_settings, tmp_path):
    import json

    from librarysync.core import integration_tokens, integrations
    from librarysync.db.models import Base, Integration, IntegrationSecret, User
    from sqlalchemy import select
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'rotation.db'}")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, autoflush=False, expire_on_commit=False)
    use_settings(secret_key="old-key", secret_key_previous=())
    async with factory() as db:
        db.add(User(id="u", username="u", password_hash="x"))
        db.add(Integration(id="int", user_id="u", provider="trakt"))
        await db.commit()
        await integration_tokens.save_integration_secret(db, "int", {"refresh_token": "old-refresh"})
        await db.commit()

    use_settings(secret_key="new-key", secret_key_previous=("old-key",))
    async with factory() as rotation_db:
        execute = rotation_db.execute
        calls = 0

        async def _execute_then_refresh(statement, *args, **kwargs):
            nonlocal calls
            result = await execute(statement, *args, **kwargs)
            calls += 1
            if calls == 1:
                # A worker rotates the refresh token right after the rotation listed the rows.
                async with factory() as worker_db:
                    await integration_tokens.save_integration_secret(worker_db, "int", {"refresh_token": "new-refresh"})
                    await worker_db.commit()
            return result

        rotation_db.execute = _execute_then_refresh
        await integrations.reencrypt_integration_secrets(rotation_db)

    async with factory() as db:
        secret = (await db.execute(select(IntegrationSecret))).scalars().one()
        assert json.loads(security.decrypt_value(secret.secret_data))["refresh_token"] == "new-refresh"
    await engine.dispose()
