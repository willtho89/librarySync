from __future__ import annotations

from datetime import datetime, timezone

from librarysync.db.session import SessionLocal, init_session_factory
from librarysync.jobs.import_base import ImportCoordinator, ImportStrategyRegistry
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


async def process_imports_once() -> int:
    init_session_factory()
    async with SessionLocal() as db:
        coordinator = ImportCoordinator(DEFAULT_IMPORT_REGISTRY)
        now = datetime.now(timezone.utc)
        return await coordinator.run_once(db, now)
