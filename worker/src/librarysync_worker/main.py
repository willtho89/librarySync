import asyncio
import logging
from datetime import datetime, timedelta, timezone

from librarysync.jobs.letterboxd_import import process_letterboxd_imports_once
from librarysync.jobs.merge_history import process_history_merges_once
from librarysync.jobs.metadata_lookup import process_metadata_lookups_once
from librarysync.jobs.process_outbox import process_outbox_once
from librarysync.jobs.trakt_import import process_trakt_imports_once

logger = logging.getLogger(__name__)
MERGE_HISTORY_INTERVAL = timedelta(hours=6)


async def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    logger.info("librarysync worker starting")
    last_history_merge: datetime | None = None
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
        try:
            processed += await process_letterboxd_imports_once()
        except Exception:
            logger.exception("letterboxd import failed")
        try:
            processed += await process_trakt_imports_once()
        except Exception:
            logger.exception("trakt import failed")
        if (
            last_history_merge is None
            or now - last_history_merge >= MERGE_HISTORY_INTERVAL
        ):
            try:
                processed += await process_history_merges_once()
            except Exception:
                logger.exception("history merge failed")
            last_history_merge = now
        delay = 1.0 if processed == 0 else 0.2
        await asyncio.sleep(delay)


if __name__ == "__main__":
    asyncio.run(main())
