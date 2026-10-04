"""Drain durably accepted bulk events and canonical-mapping retries."""

from sqlalchemy import select

from librarysync.core.watch_state_events import process_receipt
from librarysync.db.models import StremioAddonConfig, User, WatchStateReceipt, WatchStateViewer
from librarysync.db.session import SessionLocal, init_session_factory


async def drain_watch_state(db, limit=10):
    receipts = list(
        (
            await db.scalars(
                select(WatchStateReceipt)
                .where(
                    WatchStateReceipt.status == "pending",
                )
                .order_by(WatchStateReceipt.received_at, WatchStateReceipt.id)
                .limit(limit)
                .with_for_update(skip_locked=True)
            )
        ).all()
    )
    for receipt in receipts:
        user = await db.get(User, receipt.user_id, with_for_update=True)
        config = await db.get(StremioAddonConfig, receipt.addon_id)
        owner = await db.get(User, config.user_id) if config else None
        allowed = (
            user and user.is_active and config and config.is_enabled and config.watch_state_enabled and owner.is_active
        )
        if allowed and receipt.viewer:
            allowed = await db.scalar(
                select(WatchStateViewer.id).where(
                    WatchStateViewer.addon_id == receipt.addon_id,
                    WatchStateViewer.viewer == receipt.viewer,
                    WatchStateViewer.user_id == receipt.user_id,
                )
            )
        if not allowed:
            receipt.status = "failed"
            receipt.error = "Watch State disabled or viewer access revoked"
        else:
            await process_receipt(db, receipt)
    await db.commit()
    return len(receipts)


async def process_watch_state_once():
    init_session_factory()
    async with SessionLocal() as db:
        return await drain_watch_state(db)
