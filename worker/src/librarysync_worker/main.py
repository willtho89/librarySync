import asyncio
import logging

from librarysync.jobs.metadata_lookup import process_metadata_lookups_once


logger = logging.getLogger(__name__)


async def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    logger.info("librarysync worker starting")
    while True:
        try:
            processed = await process_metadata_lookups_once()
            delay = 1.0 if processed == 0 else 0.2
        except Exception:
            logger.exception("metadata lookup processing failed")
            delay = 2.0
        await asyncio.sleep(delay)


if __name__ == "__main__":
    asyncio.run(main())
