from __future__ import annotations

from typing import Iterable, TypeVar

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from librarysync.db.models import WatchEvent

T = TypeVar("T")


def chunked(values: Iterable[T], size: int) -> Iterable[list[T]]:
    batch: list[T] = []
    for value in values:
        batch.append(value)
        if len(batch) >= size:
            yield batch
            batch = []
    if batch:
        yield batch


async def load_existing_entry_keys(
    db: AsyncSession,
    user_id: str,
    event_type: str,
    entry_keys: Iterable[str],
    chunk_size: int = 200,
) -> set[str]:
    keys = [key for key in entry_keys if key]
    if not keys:
        return set()
    existing: set[str] = set()
    for batch in chunked(keys, chunk_size):
        result = await db.execute(
            select(WatchEvent.entry_key).where(
                WatchEvent.user_id == user_id,
                WatchEvent.event_type == event_type,
                WatchEvent.entry_key.in_(batch),
            )
        )
        for entry_key in result.scalars().all():
            if entry_key:
                existing.add(str(entry_key))
    return existing
