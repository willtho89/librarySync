from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from librarysync.config import settings
from librarysync.db.models import RateLimitBucket


@dataclass(frozen=True)
class RateLimitConfig:
    capacity: float
    refill_per_second: float


@dataclass(frozen=True)
class RateLimitDecision:
    allowed: bool
    retry_at: datetime | None


class RateLimiter:
    def __init__(self, configs: dict[str, RateLimitConfig]) -> None:
        self._configs = configs

    @classmethod
    def from_settings(cls) -> "RateLimiter":
        return cls(_build_rate_limit_configs())

    async def try_acquire(
        self,
        db: AsyncSession,
        user_id: str,
        provider: str,
        now: datetime | None = None,
    ) -> RateLimitDecision | None:
        config = self._configs.get(provider)
        if not config:
            return None
        if now is None:
            now = datetime.now(timezone.utc)
        begin_ctx = db.begin_nested() if db.in_transaction() else db.begin()
        async with begin_ctx:
            result = await db.execute(
                select(RateLimitBucket)
                .where(
                    RateLimitBucket.user_id == user_id,
                    RateLimitBucket.provider == provider,
                )
                .with_for_update()
            )
            bucket = result.scalars().first()
            if not bucket:
                bucket = RateLimitBucket(
                    user_id=user_id,
                    provider=provider,
                    tokens=config.capacity - 1.0,
                    last_refill_at=now,
                )
                db.add(bucket)
                return RateLimitDecision(True, None)
            last_refill = bucket.last_refill_at or bucket.updated_at or now
            elapsed = max((now - last_refill).total_seconds(), 0.0)
            tokens = min(config.capacity, bucket.tokens + elapsed * config.refill_per_second)
            if tokens < 1.0:
                retry_seconds = max((1.0 - tokens) / config.refill_per_second, 1.0)
                bucket.tokens = tokens
                bucket.last_refill_at = now
                bucket.updated_at = now
                return RateLimitDecision(False, now + timedelta(seconds=retry_seconds))
            bucket.tokens = tokens - 1.0
            bucket.last_refill_at = now
            bucket.updated_at = now
            return RateLimitDecision(True, None)


def _per_minute_config(limit: int) -> RateLimitConfig | None:
    if limit <= 0:
        return None
    refill = limit / 60.0
    return RateLimitConfig(capacity=float(limit), refill_per_second=refill)


def _window_config(max_requests: int, interval_seconds: float) -> RateLimitConfig | None:
    if max_requests <= 0 or interval_seconds <= 0:
        return None
    return RateLimitConfig(
        capacity=float(max_requests),
        refill_per_second=float(max_requests) / interval_seconds,
    )


def _build_rate_limit_configs() -> dict[str, RateLimitConfig]:
    configs: dict[str, RateLimitConfig] = {}
    for provider, limit in {
        "tmdb": settings.tmdb_rate_limit_per_minute,
        "tvdb": settings.tvdb_rate_limit_per_minute,
        "trakt": settings.trakt_rate_limit_per_minute,
        "simkl": settings.simkl_rate_limit_per_minute,
        "letterboxd": settings.letterboxd_rate_limit_per_minute,
        "stremio": settings.stremio_rate_limit_per_minute,
        "anilist": settings.anilist_rate_limit_per_minute,
    }.items():
        config = _per_minute_config(limit)
        if config:
            configs[provider] = config

    publicmetadb_config = _window_config(
        settings.publicmetadb_rate_limit_max_requests,
        settings.publicmetadb_rate_limit_interval_seconds,
    )
    if publicmetadb_config:
        configs["publicmetadb"] = publicmetadb_config
    else:
        fallback = _per_minute_config(settings.publicmetadb_rate_limit_per_minute)
        if fallback:
            configs["publicmetadb"] = fallback

    return configs


RATE_LIMITER = RateLimiter.from_settings()
