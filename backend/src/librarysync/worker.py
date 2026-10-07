import asyncio
import logging
import os
import signal
from dataclasses import dataclass
from typing import Awaitable, Callable

from librarysync.config import settings
from librarysync.core.http_client import quiet_http_request_logging
from librarysync.core.security import validate_security_settings
from librarysync.core.shutdown import request_shutdown
from librarysync.jobs.external_catalog_refresh import process_external_catalog_refresh_once
from librarysync.jobs.imports import process_import_all_once, process_quick_import_once
from librarysync.jobs.merge_history import (
    process_merge_all_history_once,
    process_merge_history_once,
)
from librarysync.jobs.metadata_backfill import process_metadata_backfill_once
from librarysync.jobs.metadata_cache import process_metadata_cache_refresh_once
from librarysync.jobs.metadata_lookup import process_metadata_lookups_once
from librarysync.jobs.process_outbox import process_outbox_once
from librarysync.jobs.watch_state import process_watch_state_once
from librarysync.jobs.watchlist_refresh import process_watchlist_refresh_once

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ModeConfig:
    name: str
    handler: Callable[[], Awaitable[int]]
    idle_delay: float
    busy_delay: float


MODE_CONFIGS: dict[str, ModeConfig] = {
    "watch_state": ModeConfig("watch_state", process_watch_state_once, 1.0, 0.1),
    "outbox": ModeConfig("outbox", process_outbox_once, 0.5, 0.1),
    "metadata": ModeConfig("metadata", process_metadata_lookups_once, 1.0, 0.2),
    "metadata_backfill": ModeConfig("metadata_backfill", process_metadata_backfill_once, 30.0, 5.0),
    "metadata_cache": ModeConfig("metadata_cache", process_metadata_cache_refresh_once, 60.0, 5.0),
    "quick_import": ModeConfig("quick_import", process_quick_import_once, 2.0, 0.5),
    "import_all": ModeConfig("import_all", process_import_all_once, 2.0, 0.5),
    "external_catalog_refresh": ModeConfig(
        "external_catalog_refresh", process_external_catalog_refresh_once, 60.0, 10.0
    ),
    "watchlist": ModeConfig("watchlist", process_watchlist_refresh_once, 60.0, 10.0),
    "merge_history": ModeConfig("merge_history", process_merge_history_once, 60.0, 10.0),
    "merge_all_history": ModeConfig(
        "merge_all_history", process_merge_all_history_once, 86400.0, 3600.0
    ),  # daily
}


def _parse_modes() -> list[ModeConfig]:
    raw = os.environ.get("LIBRARYSYNC_WORKER_MODES", "all")
    values = {value.strip().lower() for value in raw.split(",") if value.strip()}
    if not values or "all" in values:
        return list(MODE_CONFIGS.values())
    unknown = values.difference(MODE_CONFIGS.keys())
    if unknown:
        raise ValueError(f"Unknown worker mode(s): {', '.join(sorted(unknown))}")
    return [MODE_CONFIGS[name] for name in sorted(values)]


def _mode_concurrency(mode_name: str, default: int = 1) -> int:
    env_key = f"LIBRARYSYNC_WORKER_{mode_name.upper()}_CONCURRENCY"
    raw = os.environ.get(env_key)
    if raw is None:
        return default
    try:
        value = int(raw)
    except ValueError:
        return default
    return max(1, value)


MAX_FAILURE_BACKOFF_SECONDS = 300.0


def _next_delay(mode: ModeConfig, processed: int, consecutive_failures: int) -> float:
    if consecutive_failures:
        return min(mode.idle_delay * (2**consecutive_failures), MAX_FAILURE_BACKOFF_SECONDS)
    return mode.idle_delay if processed == 0 else mode.busy_delay


async def _run_mode_loop(mode: ModeConfig, worker_index: int, stop: asyncio.Event) -> None:
    logger.info("Starting %s worker %s", mode.name, worker_index)
    consecutive_failures = 0
    while not stop.is_set():
        processed = 0
        try:
            processed = await mode.handler()
            consecutive_failures = 0
        except asyncio.CancelledError:
            logger.info("Stopping %s worker %s", mode.name, worker_index)
            raise
        except Exception:
            consecutive_failures += 1
            logger.exception("%s processing failed", mode.name)
        delay = _next_delay(mode, processed, consecutive_failures)
        try:
            await asyncio.wait_for(stop.wait(), timeout=delay)
        except TimeoutError:
            pass
    logger.info("Stopped %s worker %s", mode.name, worker_index)


def _request_stop(stop: asyncio.Event) -> None:
    if not stop.is_set():
        logger.info("Shutdown requested; finishing in-flight work")
    request_shutdown()
    stop.set()


async def main() -> None:
    logging.basicConfig(
        level=settings.log_level,
        format="%(asctime)s %(levelname)s %(message)s",
    )
    quiet_http_request_logging()
    validate_security_settings()
    modes = _parse_modes()
    mode_names = ", ".join(mode.name for mode in modes)
    logger.info("librarysync worker starting (modes: %s)", mode_names)
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for signum in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(signum, _request_stop, stop)
        except (NotImplementedError, RuntimeError):  # pragma: no cover - non-POSIX loops
            pass
    tasks: list[asyncio.Task[None]] = []
    for mode in modes:
        concurrency = _mode_concurrency(mode.name)
        for idx in range(concurrency):
            tasks.append(asyncio.create_task(_run_mode_loop(mode, idx, stop)))
    try:
        await asyncio.gather(*tasks)
    finally:
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)


def run() -> None:
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("librarysync worker stopped")


if __name__ == "__main__":
    run()
