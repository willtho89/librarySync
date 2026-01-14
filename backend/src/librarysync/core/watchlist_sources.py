from __future__ import annotations

from datetime import datetime, timezone
from typing import Iterable

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from librarysync.core.watchlist import log_watchlist_event
from librarysync.db.models import WatchlistItem, WatchlistSource, WatchlistSourceItem

MANUAL_SOURCE_PROVIDER = "manual"
MANUAL_SOURCE_TYPE = "manual"
MANUAL_SOURCE_EXTERNAL_ID = "manual"
PERSONAL_SOURCE_TYPE = "personal"
URL_SOURCE_TYPE = "url"
LEGACY_LIST_SOURCE_TYPE = "list"


async def ensure_watchlist_source(
    db: AsyncSession,
    user_id: str,
    provider: str,
    source_type: str,
    external_id: str,
    *,
    url: str | None = None,
    name: str | None = None,
    is_enabled: bool | None = None,
) -> WatchlistSource:
    result = await db.execute(
        select(WatchlistSource).where(
            WatchlistSource.user_id == user_id,
            WatchlistSource.provider == provider,
            WatchlistSource.source_type == source_type,
            WatchlistSource.external_id == external_id,
        )
    )
    source = result.scalars().first()
    if source:
        updated = False
        if url and not source.url:
            source.url = url
            updated = True
        if name and not source.name:
            source.name = name
            updated = True
        if updated:
            source.updated_at = datetime.now(timezone.utc)
        return source
    source = WatchlistSource(
        user_id=user_id,
        provider=provider,
        source_type=source_type,
        external_id=external_id,
        url=url,
        name=name,
        is_enabled=True if is_enabled is None else is_enabled,
    )
    db.add(source)
    await db.flush()
    return source


async def ensure_manual_watchlist_source(db: AsyncSession, user_id: str) -> WatchlistSource:
    return await ensure_watchlist_source(
        db,
        user_id=user_id,
        provider=MANUAL_SOURCE_PROVIDER,
        source_type=MANUAL_SOURCE_TYPE,
        external_id=MANUAL_SOURCE_EXTERNAL_ID,
    )


async def ensure_personal_watchlist_source(
    db: AsyncSession,
    user_id: str,
    provider: str,
    *,
    name: str,
    url: str | None = None,
) -> WatchlistSource:
    return await ensure_watchlist_source(
        db,
        user_id=user_id,
        provider=provider,
        source_type=PERSONAL_SOURCE_TYPE,
        external_id="watchlist",
        name=name,
        url=url,
    )


async def list_watchlist_sources(
    db: AsyncSession,
    user_id: str,
    *,
    provider: str | None = None,
    include_disabled: bool = False,
) -> list[WatchlistSource]:
    stmt = select(WatchlistSource).where(
        WatchlistSource.user_id == user_id,
        WatchlistSource.provider != MANUAL_SOURCE_PROVIDER,
    )
    if provider:
        stmt = stmt.where(WatchlistSource.provider == provider)
    if not include_disabled:
        stmt = stmt.where(WatchlistSource.is_enabled.is_(True))
    result = await db.execute(stmt.order_by(WatchlistSource.created_at))
    return list(result.scalars().all())


async def remove_watchlist_source(
    db: AsyncSession,
    source: WatchlistSource,
    *,
    now: datetime | None = None,
) -> int:
    if now is None:
        now = datetime.now(timezone.utc)
    result = await db.execute(
        select(WatchlistSourceItem).where(WatchlistSourceItem.source_id == source.id)
    )
    items = result.scalars().all()
    if not items:
        await db.delete(source)
        await db.commit()
        return 0
    watchlist_item_ids = {item.watchlist_item_id for item in items}
    await db.execute(
        delete(WatchlistSourceItem).where(WatchlistSourceItem.source_id == source.id)
    )
    removed_count = 0
    for watchlist_item_id in watchlist_item_ids:
        remaining = await db.execute(
            select(WatchlistSourceItem.id).where(
                WatchlistSourceItem.watchlist_item_id == watchlist_item_id
            )
        )
        if remaining.scalars().first():
            continue
        item_result = await db.execute(
            select(WatchlistItem).where(WatchlistItem.id == watchlist_item_id)
        )
        watchlist_item = item_result.scalars().first()
        if not watchlist_item:
            continue
        if watchlist_item.source == "manual":
            continue
        await log_watchlist_event(
            db,
            watchlist_item.user_id,
            watchlist_item.media_item_id,
            "watchlist_removed",
            {"reason": "source_deleted", "source_id": source.id},
        )
        await db.delete(watchlist_item)
        removed_count += 1
    await db.delete(source)
    await db.commit()
    return removed_count


async def upsert_watchlist_source_item(
    db: AsyncSession,
    source: WatchlistSource,
    watchlist_item: WatchlistItem,
    *,
    external_item_id: str | None = None,
    now: datetime | None = None,
) -> WatchlistSourceItem:
    if now is None:
        now = datetime.now(timezone.utc)
    for pending in db.new:
        if not isinstance(pending, WatchlistSourceItem):
            continue
        if pending.source_id != source.id:
            continue
        if pending.watchlist_item_id != watchlist_item.id:
            continue
        pending.last_seen_at = now
        if external_item_id and not pending.external_item_id:
            pending.external_item_id = external_item_id
        return pending
    result = await db.execute(
        select(WatchlistSourceItem).where(
            WatchlistSourceItem.source_id == source.id,
            WatchlistSourceItem.watchlist_item_id == watchlist_item.id,
        )
    )
    existing = result.scalars().first()
    if existing:
        existing.last_seen_at = now
        if external_item_id and not existing.external_item_id:
            existing.external_item_id = external_item_id
        db.add(existing)
        return existing
    item = WatchlistSourceItem(
        source_id=source.id,
        watchlist_item_id=watchlist_item.id,
        user_id=watchlist_item.user_id,
        media_item_id=watchlist_item.media_item_id,
        external_item_id=external_item_id,
        added_at=now,
        last_seen_at=now,
    )
    db.add(item)
    return item


async def reconcile_watchlist_source(
    db: AsyncSession,
    source: WatchlistSource,
    *,
    now: datetime,
    seen_item_ids: Iterable[str],
) -> int:
    seen_set = {str(item_id) for item_id in seen_item_ids}
    if seen_set:
        stmt = select(WatchlistSourceItem).where(
            WatchlistSourceItem.source_id == source.id,
            WatchlistSourceItem.watchlist_item_id.notin_(seen_set),
        )
    else:
        stmt = select(WatchlistSourceItem).where(WatchlistSourceItem.source_id == source.id)
    result = await db.execute(stmt)
    stale = result.scalars().all()
    if not stale:
        source.last_synced_at = now
        db.add(source)
        await db.commit()
        return 0
    removed_count = 0
    watchlist_item_ids = {item.watchlist_item_id for item in stale}
    await db.execute(
        delete(WatchlistSourceItem).where(
            WatchlistSourceItem.id.in_([item.id for item in stale])
        )
    )
    for watchlist_item_id in watchlist_item_ids:
        remaining = await db.execute(
            select(WatchlistSourceItem.id).where(
                WatchlistSourceItem.watchlist_item_id == watchlist_item_id
            )
        )
        if remaining.scalars().first():
            continue
        item_result = await db.execute(
            select(WatchlistItem).where(WatchlistItem.id == watchlist_item_id)
        )
        watchlist_item = item_result.scalars().first()
        if not watchlist_item:
            continue
        if watchlist_item.source == "manual":
            continue
        await log_watchlist_event(
            db,
            watchlist_item.user_id,
            watchlist_item.media_item_id,
            "watchlist_removed",
            {"reason": "source_removed", "source_id": source.id},
        )
        await db.delete(watchlist_item)
        removed_count += 1
    source.last_synced_at = now
    db.add(source)
    await db.commit()
    return removed_count
