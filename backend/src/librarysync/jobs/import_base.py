from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import ClassVar, Iterable

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from librarysync.core.import_schedule import (
    DEFAULT_IMPORT_INTERVAL_SECONDS,
    IMPORT_REQUESTED_KEY,
    compute_next_import_at,
    parse_datetime,
    record_import_run,
    should_run_import,
)
from librarysync.core.worker_identity import worker_instance_id
from librarysync.db.models import Integration

IMPORT_LEASE_SECONDS = 20 * 60
IMPORT_FAILURE_DELAY = timedelta(minutes=5)


@dataclass(frozen=True)
class ImportResult:
    imported: int
    attempted: bool


@dataclass(frozen=True)
class ImportContext:
    db: AsyncSession
    now: datetime


class ImportStrategy(ABC):
    provider: ClassVar[str]
    default_interval_seconds: ClassVar[int | None] = DEFAULT_IMPORT_INTERVAL_SECONDS

    async def run_once(
        self,
        db: AsyncSession,
        now: datetime,
        skip_user_ids: set[str] | None = None,
        limit: int = 25,
    ) -> int:
        integrations = await _claim_due_integrations(
            db,
            self.provider,
            now,
            limit,
            skip_user_ids=skip_user_ids,
        )
        if not integrations:
            return 0
        context = ImportContext(db=db, now=now)
        total_imported = 0
        for integration in integrations:
            try:
                if not should_run_import(
                    integration.config,
                    now,
                    default_interval_seconds=self.default_interval_seconds,
                ):
                    integration.next_import_at = compute_next_import_at(
                        integration.config,
                        now,
                        default_interval_seconds=self.default_interval_seconds,
                    )
                    continue
                requested_at = parse_datetime(
                    (integration.config or {}).get(IMPORT_REQUESTED_KEY)
                )
                import_result = await self.import_for_integration(
                    context, integration, requested_at
                )
                total_imported += import_result.imported
                if import_result.attempted or requested_at is None:
                    integration.config = record_import_run(integration.config, now)
                integration.next_import_at = compute_next_import_at(
                    integration.config,
                    now,
                    default_interval_seconds=self.default_interval_seconds,
                )
            except Exception:
                integration.next_import_at = now + IMPORT_FAILURE_DELAY
                raise
            finally:
                integration.import_lease_until = None
                integration.import_lease_owner = None
                db.add(integration)
                await db.commit()
        return total_imported

    @abstractmethod
    async def import_for_integration(
        self,
        context: ImportContext,
        integration: Integration,
        requested_at: datetime | None,
    ) -> ImportResult:
        raise NotImplementedError


class ImportStrategyRegistry:
    def __init__(self, strategies: Iterable[ImportStrategy]) -> None:
        self._strategies = {strategy.provider: strategy for strategy in strategies}

    def get(self, provider: str) -> ImportStrategy | None:
        return self._strategies.get(provider)

    def list(self) -> list[ImportStrategy]:
        return list(self._strategies.values())


class ImportCoordinator:
    def __init__(self, registry: ImportStrategyRegistry) -> None:
        self._registry = registry

    async def run_once(
        self,
        db: AsyncSession,
        now: datetime,
        skip_user_ids: set[str] | None = None,
        limit: int = 25,
    ) -> int:
        total = 0
        for strategy in self._registry.list():
            total += await strategy.run_once(
                db,
                now,
                skip_user_ids=skip_user_ids,
                limit=limit,
            )
        return total


async def _claim_due_integrations(
    db: AsyncSession,
    provider: str,
    now: datetime,
    limit: int,
    skip_user_ids: set[str] | None = None,
) -> list[Integration]:
    lease_until = now + timedelta(seconds=IMPORT_LEASE_SECONDS)
    async with db.begin():
        query = select(Integration).where(
            Integration.provider == provider,
            Integration.next_import_at <= now,
            or_(
                Integration.import_lease_until.is_(None),
                Integration.import_lease_until <= now,
            ),
        )
        if skip_user_ids:
            query = query.where(~Integration.user_id.in_(skip_user_ids))
        query = query.order_by(Integration.next_import_at, Integration.user_id)
        query = query.limit(limit).with_for_update(skip_locked=True)
        result = await db.execute(query)
        integrations = result.scalars().all()
        for integration in integrations:
            integration.import_lease_until = lease_until
            integration.import_lease_owner = worker_instance_id()
            integration.updated_at = now
    return integrations
