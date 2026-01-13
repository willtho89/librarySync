from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable, Protocol

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from librarysync.config import settings
from librarysync.core.import_all import (
    IMPORT_ALL_ERROR_KEY,
    IMPORT_ALL_INDEX_KEY,
    IMPORT_ALL_PROVIDER,
    IMPORT_ALL_STATUS_IN_PROGRESS,
    IMPORT_ALL_STATUS_KEY,
    IMPORT_ALL_STATUS_PENDING,
    build_import_all_queue,
    import_all_active,
    mark_import_all_completed,
    mark_import_all_failed,
    mark_import_all_started,
    parse_import_all_state,
)
from librarysync.core.import_control import (
    MERGE_COMPLETED_AT_KEY,
    MERGE_ERROR_KEY,
    MERGE_REQUIRED_AT_KEY,
    QUICK_IMPORT_ERROR_KEY,
    QUICK_IMPORT_INDEX_KEY,
    QUICK_IMPORT_STATUS_IN_PROGRESS,
    QUICK_IMPORT_STATUS_PENDING,
    build_quick_import_config,
    mark_merge_failed,
    mark_merge_required,
    mark_quick_import_completed,
    mark_quick_import_failed,
    mark_quick_import_started,
    parse_quick_import_state,
    should_run_quick_import,
)
from librarysync.core.import_history import (
    IMPORT_HISTORY_KEY,
    append_import_history,
    build_import_history_entry,
)
from librarysync.core.import_schedule import parse_datetime
from librarysync.db.models import Integration
from librarysync.db.session import SessionLocal, init_session_factory
from librarysync.jobs.aiostreams_import import AIOStreamsImportStrategy
from librarysync.jobs.anilist_import import AniListImportStrategy
from librarysync.jobs.import_base import ImportContext, ImportStrategyRegistry
from librarysync.jobs.letterboxd_import import LetterboxdImportStrategy
from librarysync.jobs.merge_history import enqueue_merge_history
from librarysync.jobs.simkl_import import SimklImportStrategy
from librarysync.jobs.stremio_import import StremioImportStrategy
from librarysync.jobs.trakt_import import TraktImportStrategy


class ImportRunState(Protocol):
    queue: list[str]
    index: int
    requested_at: datetime | None
    status: str | None
    completed_at: datetime | None
    error: str | None


@dataclass(frozen=True)
class ImportRunSpec:
    name: str
    registry: ImportStrategyRegistry
    parse_state: Callable[[dict | None], ImportRunState]
    mark_completed: Callable[[dict | None, datetime], dict]
    mark_failed: Callable[[dict | None, datetime, str], dict]
    index_key: str
    error_key: str
    history_event_type: str


QUICK_IMPORT_LOOKBACK_DAYS = 7
IMPORT_ALL_LOOKBACK_DAYS = settings.history_lookback_days


def _build_registry(lookback_days: int) -> ImportStrategyRegistry:
    return ImportStrategyRegistry(
        [
            LetterboxdImportStrategy(lookback_days=lookback_days),
            TraktImportStrategy(lookback_days=lookback_days),
            SimklImportStrategy(lookback_days=lookback_days),
            AniListImportStrategy(lookback_days=lookback_days),
            StremioImportStrategy(lookback_days=lookback_days),
            AIOStreamsImportStrategy(lookback_days=lookback_days),
        ]
    )


QUICK_IMPORT_REGISTRY = _build_registry(QUICK_IMPORT_LOOKBACK_DAYS)
IMPORT_ALL_REGISTRY = _build_registry(IMPORT_ALL_LOOKBACK_DAYS)

QUICK_IMPORT_SPEC = ImportRunSpec(
    name="quick_import",
    registry=QUICK_IMPORT_REGISTRY,
    parse_state=parse_quick_import_state,
    mark_completed=mark_quick_import_completed,
    mark_failed=mark_quick_import_failed,
    index_key=QUICK_IMPORT_INDEX_KEY,
    error_key=QUICK_IMPORT_ERROR_KEY,
    history_event_type="quick_import",
)
IMPORT_ALL_SPEC = ImportRunSpec(
    name="import_all",
    registry=IMPORT_ALL_REGISTRY,
    parse_state=parse_import_all_state,
    mark_completed=mark_import_all_completed,
    mark_failed=mark_import_all_failed,
    index_key=IMPORT_ALL_INDEX_KEY,
    error_key=IMPORT_ALL_ERROR_KEY,
    history_event_type="import_all",
)


async def process_quick_import_once(limit: int = 1) -> int:
    init_session_factory()
    async with SessionLocal() as db:
        runs = await _claim_quick_import_runs(db, limit)
        if not runs:
            return 0
        processed = 0
        now = datetime.now(timezone.utc)
        for run in runs:
            processed += await _process_quick_import_run(db, run, now)
        return processed


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


async def _claim_quick_import_runs(db: AsyncSession, limit: int) -> list[Integration]:
    now = datetime.now(timezone.utc)
    candidate_limit = max(limit * 5, limit)
    async with db.begin():
        result = await db.execute(
            select(Integration)
            .where(Integration.provider == IMPORT_ALL_PROVIDER)
            .order_by(Integration.updated_at)
            .limit(candidate_limit)
            .with_for_update(skip_locked=True)
        )
        runs: list[Integration] = []
        for run in result.scalars().all():
            if len(runs) >= limit:
                break
            if import_all_active(run.config):
                continue
            if not should_run_quick_import(run.config, now):
                continue
            state = parse_quick_import_state(run.config)
            if state.status not in {
                QUICK_IMPORT_STATUS_PENDING,
                QUICK_IMPORT_STATUS_IN_PROGRESS,
            }:
                queue = await build_import_all_queue(db, run.user_id)
                if not queue:
                    continue
                config = build_quick_import_config(run.config, queue, now)
                config = mark_merge_required(config, now)
                run.config = config
                state = parse_quick_import_state(run.config)
            if state.status == QUICK_IMPORT_STATUS_PENDING:
                run.config = mark_quick_import_started(run.config, now)
                run.updated_at = now
            elif not _has_merge_required(run.config):
                run.config = mark_merge_required(run.config, now)
                run.updated_at = now
            runs.append(run)
    return runs


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
                config = mark_import_all_started(run.config, now)
                if not _has_merge_required(config):
                    config = mark_merge_required(config, now)
                run.config = config
                run.updated_at = now
            elif not _has_merge_required(run.config):
                run.config = mark_merge_required(run.config, now)
                run.updated_at = now
    return runs


async def _process_quick_import_run(
    db: AsyncSession, run: Integration, now: datetime
) -> int:
    return await _process_import_run(db, run, now, QUICK_IMPORT_SPEC)




async def _process_import_all_run(
    db: AsyncSession, run: Integration, now: datetime
) -> int:
    return await _process_import_run(db, run, now, IMPORT_ALL_SPEC)


async def _process_import_run(
    db: AsyncSession,
    run: Integration,
    now: datetime,
    spec: ImportRunSpec,
) -> int:
    state = spec.parse_state(run.config)
    queue = list(state.queue)
    if not queue or state.index >= len(queue):
        run.config = dict(run.config or {})
        run.config[spec.error_key] = None
        run.config[spec.index_key] = len(queue)
        await _finalize_merge(
            db,
            run,
            now,
            spec.mark_completed,
            history_event_type=spec.history_event_type,
            history_parser=spec.parse_state,
        )
        return 1

    provider = queue[state.index]
    strategy = spec.registry.get(provider)
    if not strategy:
        return await _advance_import_run(db, run, queue, state.index + 1, now, spec)

    result = await db.execute(
        select(Integration).where(
            Integration.user_id == run.user_id,
            Integration.provider == provider,
        )
    )
    integration = result.scalars().first()
    if not integration:
        return await _advance_import_run(db, run, queue, state.index + 1, now, spec)

    next_index: int | None = None
    try:
        await strategy.import_for_integration(
            ImportContext(db=db, now=now),
            integration,
            state.requested_at,
        )
        run.config = dict(run.config or {})
        run.config[spec.error_key] = None
        next_index = state.index + 1
        run.config[spec.index_key] = next_index
        if next_index >= len(queue):
            await _finalize_merge(
                db,
                run,
                now,
                spec.mark_completed,
                history_event_type=spec.history_event_type,
                history_parser=spec.parse_state,
            )
            return 1
        db.add(run)
        await db.commit()
        return 1
    except Exception as exc:
        run.config = spec.mark_failed(run.config, now, str(exc)[:500])
        run.config[spec.index_key] = next_index if next_index is not None else state.index
        await _finalize_merge(
            db,
            run,
            now,
            None,
            history_event_type=spec.history_event_type,
            history_parser=spec.parse_state,
        )
        return 1


async def _advance_import_run(
    db: AsyncSession,
    run: Integration,
    queue: list[str],
    next_index: int,
    now: datetime,
    spec: ImportRunSpec,
) -> int:
    run.config = dict(run.config or {})
    run.config[spec.index_key] = next_index
    if next_index >= len(queue):
        await _finalize_merge(
            db,
            run,
            now,
            spec.mark_completed,
            history_event_type=spec.history_event_type,
            history_parser=spec.parse_state,
        )
        return 1
    db.add(run)
    await db.commit()
    return 1


async def _finalize_merge(
    db: AsyncSession,
    run: Integration,
    now: datetime,
    finalize_run: callable | None,
    history_event_type: str | None = None,
    history_parser: callable | None = None,
) -> None:
    run.config = mark_merge_required(run.config, now)
    try:
        await enqueue_merge_history(db, now)
        if finalize_run is not None:
            run.config = finalize_run(run.config, now)
        if history_event_type and history_parser:
            run.config = _append_import_history_entry(
                run.config, history_event_type, history_parser
            )
    except Exception as exc:
        run.config = mark_merge_failed(run.config, now, str(exc)[:500])
        if finalize_run is not None and run.config:
            run.config = finalize_run(run.config, now)
        if history_event_type and history_parser:
            run.config = _append_import_history_entry(
                run.config, history_event_type, history_parser
            )
    db.add(run)
    await db.commit()


def _has_merge_required(config: dict | None) -> bool:
    if not isinstance(config, dict):
        return False
    value = config.get(MERGE_REQUIRED_AT_KEY)
    return bool(value)


def _append_import_history_entry(
    config: dict | None,
    event_type: str,
    parser: callable,
) -> dict | None:
    if config is None:
        return None
    state = parser(config)
    if not state.status or state.status not in {"completed", "failed"}:
        return config
    if _already_recorded_import(config, event_type, state.status, state.completed_at):
        return config
    entry = build_import_history_entry(
        event_type=event_type,
        status=state.status,
        requested_at=state.requested_at,
        started_at=state.started_at,
        completed_at=state.completed_at,
        error=state.error,
        queue=state.queue,
        merge_required_at=parse_datetime(config.get(MERGE_REQUIRED_AT_KEY)),
        merge_completed_at=parse_datetime(config.get(MERGE_COMPLETED_AT_KEY)),
        merge_error=config.get(MERGE_ERROR_KEY),
    )
    return append_import_history(config, entry)


def _already_recorded_import(
    config: dict,
    event_type: str,
    status: str,
    completed_at: datetime | None,
) -> bool:
    history = config.get(IMPORT_HISTORY_KEY)
    if not isinstance(history, list) or not history:
        return False
    last = history[-1]
    if not isinstance(last, dict):
        return False
    last_completed = last.get("completed_at")
    current_completed = completed_at.isoformat() if completed_at else None
    return (
        last.get("event_type") == event_type
        and last.get("status") == status
        and last_completed == current_completed
    )
