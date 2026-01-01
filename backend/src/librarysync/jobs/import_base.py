from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime
from typing import ClassVar, Iterable

from sqlalchemy.ext.asyncio import AsyncSession

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
