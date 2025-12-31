from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from librarysync.core.import_all import (
    IMPORT_ALL_ERROR_KEY,
    IMPORT_ALL_INDEX_KEY,
    IMPORT_ALL_PROVIDER,
    IMPORT_ALL_STATUS_IN_PROGRESS,
    IMPORT_ALL_STATUS_KEY,
    IMPORT_ALL_STATUS_PENDING,
    load_active_import_all_users,
    mark_import_all_completed,
    mark_import_all_failed,
    mark_import_all_started,
    parse_import_all_state,
)
from librarysync.core.import_schedule import compute_next_import_at, record_import_run
from librarysync.db.models import Integration
from librarysync.db.session import SessionLocal, init_session_factory
from librarysync.jobs.import_base import ImportContext, ImportCoordinator, ImportStrategyRegistry
from librarysync.jobs.letterboxd_import import LetterboxdImportStrategy
from librarysync.jobs.simkl_import import SimklImportStrategy
from librarysync.jobs.stremio_import import StremioImportStrategy
from librarysync.jobs.trakt_import import TraktImportStrategy

DEFAULT_IMPORT_REGISTRY = ImportStrategyRegistry(
    [
        LetterboxdImportStrategy(),
        TraktImportStrategy(),
        SimklImportStrategy(),
        StremioImportStrategy(),
    ]
)
IMPORT_CLAIM_LIMIT = 25


async def process_imports_once() -> int:
    init_session_factory()
    async with SessionLocal() as db:
        coordinator = ImportCoordinator(DEFAULT_IMPORT_REGISTRY)
        now = datetime.now(timezone.utc)
        active_users = await load_active_import_all_users(db)
        await db.rollback()
        return await coordinator.run_once(
            db,
            now,
            skip_user_ids=active_users,
            limit=IMPORT_CLAIM_LIMIT,
        )


async def process_import_all_once(limit: int = 1) -> int:
    init_session_factory()
    async with SessionLocal() as db:
        runs = await _claim_import_all_runs(db, limit)
        if not runs:
            return 0
        processed = 0
        now = datetime.now(timezone.utc)
        for run in runs:
            processed += await _process_import_all_run(db, run, now)
        return processed


async def _claim_import_all_runs(db: AsyncSession, limit: int) -> list[Integration]:
    async with db.begin():
        result = await db.execute(
            select(Integration)
            .where(
                Integration.provider == IMPORT_ALL_PROVIDER,
                Integration.config.isnot(None),
                Integration.config[IMPORT_ALL_STATUS_KEY]
                .as_string()
                .in_([IMPORT_ALL_STATUS_PENDING, IMPORT_ALL_STATUS_IN_PROGRESS]),
            )
            .order_by(Integration.updated_at)
            .limit(limit)
            .with_for_update(skip_locked=True)
        )
        runs = result.scalars().all()
        if not runs:
            return []
        now = datetime.now(timezone.utc)
        for run in runs:
            state = parse_import_all_state(run.config)
            if state.status == IMPORT_ALL_STATUS_PENDING:
                run.config = mark_import_all_started(run.config, now)
                run.updated_at = now
    return runs


async def _process_import_all_run(
    db: AsyncSession, run: Integration, now: datetime
) -> int:
    state = parse_import_all_state(run.config)
    queue = list(state.queue)
    if not queue or state.index >= len(queue):
        run.config = mark_import_all_completed(run.config, now)
        run.config[IMPORT_ALL_INDEX_KEY] = len(queue)
        db.add(run)
        await db.commit()
        return 1
    provider = queue[state.index]
    strategy = DEFAULT_IMPORT_REGISTRY.get(provider)
    if not strategy:
        return await _advance_import_all_run(db, run, queue, state.index + 1, now)

    result = await db.execute(
        select(Integration).where(
            Integration.user_id == run.user_id,
            Integration.provider == provider,
        )
    )
    integration = result.scalars().first()
    if not integration:
        return await _advance_import_all_run(db, run, queue, state.index + 1, now)

    try:
        import_result = await strategy.import_for_integration(
            ImportContext(db=db, now=now),
            integration,
            state.requested_at,
        )
        if import_result.attempted:
            integration.config = record_import_run(integration.config, now)
            integration.next_import_at = compute_next_import_at(
                integration.config,
                now,
                default_interval_seconds=strategy.default_interval_seconds,
            )
            db.add(integration)
        run.config = dict(run.config or {})
        run.config[IMPORT_ALL_ERROR_KEY] = None
        run.config[IMPORT_ALL_INDEX_KEY] = state.index + 1
        if run.config[IMPORT_ALL_INDEX_KEY] >= len(queue):
            run.config = mark_import_all_completed(run.config, now)
        db.add(run)
        await db.commit()
        return 1
    except Exception as exc:
        run.config = mark_import_all_failed(run.config, now, str(exc)[:500])
        run.config[IMPORT_ALL_INDEX_KEY] = state.index
        db.add(run)
        await db.commit()
        return 1


async def _advance_import_all_run(
    db: AsyncSession,
    run: Integration,
    queue: list[str],
    next_index: int,
    now: datetime,
) -> int:
    run.config = dict(run.config or {})
    run.config[IMPORT_ALL_INDEX_KEY] = next_index
    if next_index >= len(queue):
        run.config = mark_import_all_completed(run.config, now)
    db.add(run)
    await db.commit()
    return 1
