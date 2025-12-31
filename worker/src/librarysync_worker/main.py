import asyncio
import logging
import os
from dataclasses import dataclass
from typing import Awaitable, Callable

from librarysync.config import settings
from librarysync.jobs.imports import process_import_all_once, process_imports_once
from librarysync.jobs.merge_history import process_history_merges_once
from librarysync.jobs.metadata_lookup import process_metadata_lookups_once
from librarysync.jobs.process_outbox import process_outbox_once

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ModeConfig:
    name: str
    handler: Callable[[], Awaitable[int]]
    idle_delay: float
    busy_delay: float


MODE_CONFIGS: dict[str, ModeConfig] = {
    "outbox": ModeConfig("outbox", process_outbox_once, 0.5, 0.1),
    "metadata": ModeConfig("metadata", process_metadata_lookups_once, 1.0, 0.2),
    "imports": ModeConfig("imports", process_imports_once, 2.0, 0.5),
    "import_all": ModeConfig("import_all", process_import_all_once, 2.0, 0.5),
    "merge": ModeConfig("merge", process_history_merges_once, 10.0, 1.0),
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


async def _run_mode_loop(mode: ModeConfig, worker_index: int) -> None:
    logger.info("Starting %s worker %s", mode.name, worker_index)
    while True:
        processed = 0
        try:
            processed = await mode.handler()
        except Exception:
            logger.exception("%s processing failed", mode.name)
        delay = mode.idle_delay if processed == 0 else mode.busy_delay
        await asyncio.sleep(delay)


async def main() -> None:
    logging.basicConfig(
        level=settings.log_level,
        format="%(asctime)s %(levelname)s %(message)s",
    )
    modes = _parse_modes()
    mode_names = ", ".join(mode.name for mode in modes)
    logger.info("librarysync worker starting (modes: %s)", mode_names)
    tasks: list[asyncio.Task[None]] = []
    for mode in modes:
        concurrency = _mode_concurrency(mode.name)
        for idx in range(concurrency):
            tasks.append(asyncio.create_task(_run_mode_loop(mode, idx)))
    await asyncio.gather(*tasks)


if __name__ == "__main__":
    asyncio.run(main())
