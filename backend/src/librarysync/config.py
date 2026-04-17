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
    anilist_client_id: str | None
    anilist_client_secret: str | None
    admin_api_key: str | None
    history_lookback_days: int
    log_level: str
    jwt_access_token_minutes: int
    jwt_algorithm: str
    allow_registration: bool
    max_users: int
    gzip_enabled: bool
    gzip_min_size: int
    trakt_rate_limit_per_minute: int
    simkl_rate_limit_per_minute: int
    letterboxd_rate_limit_per_minute: int
    stremio_rate_limit_per_minute: int
    anilist_rate_limit_per_minute: int
    publicmetadb_rate_limit_per_minute: int
    publicmetadb_rate_limit_max_requests: int
    publicmetadb_rate_limit_interval_seconds: float
    publicmetadb_batch_rate_limit_max_requests: int
    publicmetadb_batch_rate_limit_interval_seconds: float
    tmdb_rate_limit_per_minute: int
    tvdb_rate_limit_per_minute: int
    enable_dashboard_stats: bool
    trakt_max_batch_size: int
    simkl_max_batch_size: int
    external_catalog_refresh_hours: int
    external_catalog_max_items: int


def load_settings() -> Settings:
    return Settings(
        database_url=_get_env("DATABASE_URL"),
        secret_key=_get_env("LIBRARYSYNC_SECRET_KEY"),
        base_url=_get_env("LIBRARYSYNC_BASE_URL"),
        trakt_client_id=_get_env("TRAKT_CLIENT_ID"),
        trakt_client_secret=_get_env("TRAKT_CLIENT_SECRET"),
        simkl_client_id=_get_env("SIMKL_CLIENT_ID"),
        simkl_client_secret=_get_env("SIMKL_CLIENT_SECRET"),
        anilist_client_id=_get_env("ANILIST_CLIENT_ID"),
        anilist_client_secret=_get_env("ANILIST_CLIENT_SECRET"),
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
        max_users=int(_get_env("LIBRARYSYNC_MAX_USERS", "1") or "1"),
        gzip_enabled=(
            (_get_env("LIBRARYSYNC_GZIP_ENABLED", "true") or "true").lower() == "true"
        ),
        gzip_min_size=int(_get_env("LIBRARYSYNC_GZIP_MIN_SIZE", "500") or "500"),
        trakt_rate_limit_per_minute=int(
            _get_env("LIBRARYSYNC_TRAKT_RATE_LIMIT_PER_MINUTE", "60") or "60"
        ),
        simkl_rate_limit_per_minute=int(
            _get_env("LIBRARYSYNC_SIMKL_RATE_LIMIT_PER_MINUTE", "60") or "60"
        ),
        letterboxd_rate_limit_per_minute=int(
            _get_env("LIBRARYSYNC_LETTERBOXD_RATE_LIMIT_PER_MINUTE", "30") or "30"
        ),
        stremio_rate_limit_per_minute=int(
            _get_env("LIBRARYSYNC_STREMIO_RATE_LIMIT_PER_MINUTE", "120") or "120"
        ),
        anilist_rate_limit_per_minute=int(
            _get_env("LIBRARYSYNC_ANILIST_RATE_LIMIT_PER_MINUTE", "90") or "90"
        ),
        publicmetadb_rate_limit_per_minute=int(
            _get_env("LIBRARYSYNC_PUBLICMETADB_RATE_LIMIT_PER_MINUTE", "120") or "120"
        ),
        publicmetadb_rate_limit_max_requests=int(
            _get_env("LIBRARYSYNC_PUBLICMETADB_RATE_LIMIT_MAX_REQUESTS", "300") or "300"
        ),
        publicmetadb_rate_limit_interval_seconds=float(
            _get_env("LIBRARYSYNC_PUBLICMETADB_RATE_LIMIT_INTERVAL_SECONDS", "10") or "10"
        ),
        publicmetadb_batch_rate_limit_max_requests=int(
            _get_env("LIBRARYSYNC_PUBLICMETADB_BATCH_RATE_LIMIT_MAX_REQUESTS", "3") or "3"
        ),
        publicmetadb_batch_rate_limit_interval_seconds=float(
            _get_env("LIBRARYSYNC_PUBLICMETADB_BATCH_RATE_LIMIT_INTERVAL_SECONDS", "1") or "1"
        ),
        tmdb_rate_limit_per_minute=int(
            _get_env("LIBRARYSYNC_TMDB_RATE_LIMIT_PER_MINUTE", "150") or "150"
        ),
        tvdb_rate_limit_per_minute=int(
            _get_env("LIBRARYSYNC_TVDB_RATE_LIMIT_PER_MINUTE", "150") or "150"
        ),
        enable_dashboard_stats=(
            (_get_env("LIBRARYSYNC_ENABLE_DASHBOARD_STATS", "true") or "true").lower() == "true"
        ),
        trakt_max_batch_size=int(
            _get_env("LIBRARYSYNC_TRAKT_MAX_BATCH_SIZE", "750") or "750"
        ),
        simkl_max_batch_size=int(
            _get_env("LIBRARYSYNC_SIMKL_MAX_BATCH_SIZE", "750") or "750"
        ),
        external_catalog_refresh_hours=int(
            _get_env("LIBRARYSYNC_EXTERNAL_CATALOG_REFRESH_HOURS", "3") or "3"
        ),
        external_catalog_max_items=int(
            _get_env("LIBRARYSYNC_EXTERNAL_CATALOG_MAX_ITEMS", "500") or "500"
        ),
    )


settings = load_settings()
