import os
from dataclasses import dataclass


def _get_env(name: str, default: str | None = None) -> str | None:
    value = os.environ.get(name, default)
    return value


@dataclass(frozen=True)
class Settings:
    database_url: str | None
    secret_key: str | None
    base_url: str | None
    trakt_client_id: str | None
    trakt_client_secret: str | None
    simkl_client_id: str | None
    simkl_client_secret: str | None
    admin_api_key: str | None
    history_lookback_days: int
    log_level: str
    jwt_access_token_minutes: int
    jwt_algorithm: str
    allow_registration: bool


def load_settings() -> Settings:
    return Settings(
        database_url=_get_env("DATABASE_URL"),
        secret_key=_get_env("LIBRARYSYNC_SECRET_KEY"),
        base_url=_get_env("LIBRARYSYNC_BASE_URL"),
        trakt_client_id=_get_env("TRAKT_CLIENT_ID"),
        trakt_client_secret=_get_env("TRAKT_CLIENT_SECRET"),
        simkl_client_id=_get_env("SIMKL_CLIENT_ID"),
        simkl_client_secret=_get_env("SIMKL_CLIENT_SECRET"),
        admin_api_key=_get_env("LIBRARYSYNC_ADMIN_API_KEY"),
        history_lookback_days=int(_get_env("HISTORY_LOOKBACK_DAYS", "30") or "30"),
        log_level=_get_env("LOG_LEVEL", "INFO") or "INFO",
        jwt_access_token_minutes=int(
            _get_env("LIBRARYSYNC_JWT_ACCESS_TOKEN_MINUTES", "60") or "60"
        ),
        jwt_algorithm=_get_env("LIBRARYSYNC_JWT_ALGORITHM", "HS256") or "HS256",
        allow_registration=(
            (_get_env("LIBRARYSYNC_ALLOW_REGISTRATION", "true") or "true").lower() == "true"
        ),
    )


settings = load_settings()
