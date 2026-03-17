import logging
from datetime import date, datetime, timezone
from functools import lru_cache
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from librarysync.config import settings

logger = logging.getLogger(__name__)


@lru_cache(maxsize=16)
def _resolve_timezone(timezone_name: str):
    try:
        return ZoneInfo(timezone_name)
    except ZoneInfoNotFoundError:
        logger.warning(
            "Unknown release date timezone '%s'; falling back to UTC",
            timezone_name,
        )
        return timezone.utc


def get_release_now_date(
    now: datetime | None = None,
    timezone_name: str | None = None,
) -> date:
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    release_timezone = _resolve_timezone(timezone_name or settings.release_date_timezone)
    return current.astimezone(release_timezone).date()
