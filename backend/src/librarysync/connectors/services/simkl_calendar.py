"""Public SIMKL calendar v2 files, separate from authenticated history imports."""

from dataclasses import dataclass
from datetime import date, datetime, timezone

from librarysync.connectors.services.simkl import SimklError
from librarysync.core.http_client import get_app_version, get_http_client

CALENDAR_BASE_URL = "https://data.simkl.in/calendar/v2"


@dataclass(frozen=True)
class CalendarEpisode:
    catalog: str
    show_ids: dict[str, str]
    season: int | None
    number: int
    title: str | None
    aired_at: datetime
    finale_type: int | None


def _positive_int(value: object) -> int | None:
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        return None
    try:
        number = int(value)
    except ValueError:
        return None
    return number if number > 0 else None


def parse_calendar(payload: object, catalog: str) -> list[CalendarEpisode]:
    if not isinstance(payload, dict) or not isinstance(payload.get("calendar"), list):
        raise SimklError("SIMKL calendar v2 response is missing calendar[]")
    metadata = payload.get("metadata")
    if not isinstance(metadata, dict):
        raise SimklError("SIMKL calendar v2 response is missing metadata{}")
    episodes = []
    for entry in payload["calendar"]:
        if not isinstance(entry, dict) or not isinstance(entry.get("episode"), dict):
            continue
        show_id = _positive_int(entry.get("simkl_id"))
        show = metadata.get(str(show_id))
        if show_id is None or not isinstance(show, dict):
            continue
        episode = entry["episode"]
        number = _positive_int(episode.get("episode"))
        season = _positive_int(episode.get("season"))
        if number is None or (catalog == "tv" and season is None):
            continue
        timestamp = entry.get("date")
        if not isinstance(timestamp, str):
            continue
        try:
            aired_at = datetime.fromisoformat(timestamp)
        except ValueError:
            continue
        if aired_at.tzinfo is None:
            continue
        ids = {"simkl": str(show_id)}
        show_ids = show.get("ids")
        if isinstance(show_ids, dict):
            for key in ("imdb", "tmdb", "tvdb"):
                value = show_ids.get(key)
                if isinstance(value, (str, int)) and not isinstance(value, bool) and str(value).strip():
                    ids[key] = str(value).strip().lower()
        finale = _positive_int(entry.get("finale_type"))
        episodes.append(
            CalendarEpisode(
                catalog=catalog,
                show_ids=ids,
                season=season,
                number=number,
                title=episode.get("title") if isinstance(episode.get("title"), str) else None,
                aired_at=aired_at.astimezone(timezone.utc),
                finale_type=finale if finale in {1, 2, 3} else None,
            )
        )
    return episodes


async def fetch_calendar(client_id: str, catalog: str, month: date | None = None) -> list[CalendarEpisode]:
    if catalog not in {"tv", "anime"}:
        raise ValueError("Unsupported SIMKL episode calendar")
    prefix = f"{month.year}/{month.month}/" if month else ""
    params = {"client_id": client_id, "app-name": "librarySync", "app-version": get_app_version()}
    async with get_http_client(timeout=30.0) as client:
        response = await client.get(f"{CALENDAR_BASE_URL}/{prefix}{catalog}.json", params=params)
        response.raise_for_status()
        return parse_calendar(response.json(), catalog)
