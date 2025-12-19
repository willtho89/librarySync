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
    poll_interval_seconds: int
    completion_threshold_percent: float
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
        poll_interval_seconds=int(_get_env("POLL_INTERVAL_SECONDS", "60") or "60"),
        completion_threshold_percent=float(
            _get_env("COMPLETION_THRESHOLD_PERCENT", "85") or "85"
        ),
        log_level=_get_env("LOG_LEVEL", "INFO") or "INFO",
        jwt_access_token_minutes=int(
            _get_env("LIBRARYSYNC_JWT_ACCESS_TOKEN_MINUTES", "60") or "60"
        ),
        jwt_algorithm=_get_env("LIBRARYSYNC_JWT_ALGORITHM", "HS256") or "HS256",
        allow_registration=(
            (_get_env("LIBRARYSYNC_ALLOW_REGISTRATION", "true") or "true").lower()
            == "true"
        ),
    )


settings = load_settings()
