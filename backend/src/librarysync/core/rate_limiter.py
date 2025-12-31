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
        configs: dict[str, RateLimitConfig] = {}
        for provider, limit in {
            "trakt": settings.trakt_rate_limit_per_minute,
            "simkl": settings.simkl_rate_limit_per_minute,
            "letterboxd": settings.letterboxd_rate_limit_per_minute,
            "stremio": settings.stremio_rate_limit_per_minute,
        }.items():
            if limit <= 0:
                continue
            refill = limit / 60.0
            configs[provider] = RateLimitConfig(capacity=float(limit), refill_per_second=refill)
        return cls(configs)

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
        async with db.begin():
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


RATE_LIMITER = RateLimiter.from_settings()
