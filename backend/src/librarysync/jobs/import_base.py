from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime
from typing import ClassVar, Iterable

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from librarysync.core.import_schedule import (
    DEFAULT_IMPORT_INTERVAL_SECONDS,
    IMPORT_REQUESTED_KEY,
    parse_datetime,
    record_import_run,
    should_run_import,
)
from librarysync.db.models import Integration


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

    async def run_once(self, db: AsyncSession, now: datetime) -> int:
        result = await db.execute(
            select(Integration).where(Integration.provider == self.provider)
        )
        integrations = result.scalars().all()
        if not integrations:
            return 0
        context = ImportContext(db=db, now=now)
        total_imported = 0
        for integration in integrations:
            if not should_run_import(
                integration.config,
                now,
                default_interval_seconds=self.default_interval_seconds,
            ):
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

    async def run_once(self, db: AsyncSession, now: datetime) -> int:
        total = 0
        for strategy in self._registry.list():
            total += await strategy.run_once(db, now)
        return total
