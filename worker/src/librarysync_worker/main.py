import asyncio
import logging
from datetime import datetime, timedelta, timezone

from librarysync.jobs.letterboxd_import import process_letterboxd_imports_once
from librarysync.jobs.trakt_import import process_trakt_imports_once
from librarysync.jobs.metadata_lookup import process_metadata_lookups_once
from librarysync.jobs.process_outbox import process_outbox_once


logger = logging.getLogger(__name__)
LETTERBOXD_IMPORT_INTERVAL = timedelta(hours=24)
TRAKT_IMPORT_INTERVAL = timedelta(hours=24)


async def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    logger.info("librarysync worker starting")
    last_letterboxd_import: datetime | None = None
    last_trakt_import: datetime | None = None
    while True:
        processed = 0
        try:
            processed += await process_metadata_lookups_once()
        except Exception:
            logger.exception("metadata lookup processing failed")
        try:
            processed += await process_outbox_once()
        except Exception:
            logger.exception("outbox processing failed")
        now = datetime.now(timezone.utc)
        if (
            last_letterboxd_import is None
            or now - last_letterboxd_import >= LETTERBOXD_IMPORT_INTERVAL
        ):
            try:
                processed += await process_letterboxd_imports_once()
            except Exception:
                logger.exception("letterboxd import failed")
            last_letterboxd_import = now
        if last_trakt_import is None or now - last_trakt_import >= TRAKT_IMPORT_INTERVAL:
            try:
                processed += await process_trakt_imports_once()
            except Exception:
                logger.exception("trakt import failed")
            last_trakt_import = now
        delay = 1.0 if processed == 0 else 0.2
        await asyncio.sleep(delay)


if __name__ == "__main__":
    asyncio.run(main())
