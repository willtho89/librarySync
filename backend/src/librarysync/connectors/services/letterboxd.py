import json
import logging
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date, datetime, timezone
from typing import Any

import httpx

DEFAULT_LETTERBOXD_API_BASE_URL = "https://api.letterboxd.com/api/v0"
LETTERBOXD_REQUIRED_FIELDS = ("client_id", "client_secret", "refresh_token")
logger = logging.getLogger(__name__)


@dataclass
class LogEntryCheck:
    already_logged_today: bool
    has_any_entries: bool
    entry_id: str | None = None


class LetterboxdError(RuntimeError):
    def __init__(
        self,
        message: str,
        status_code: int | None = None,
        response_body: str | None = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.response_body = response_body


def has_required_letterboxd_fields(values: Mapping[str, object]) -> bool:
    for field in LETTERBOXD_REQUIRED_FIELDS:
        value = values.get(field)
        if not isinstance(value, str) or not value:
            return False
    return True


class LetterboxdClient:
    def __init__(
        self,
        api_base_url: str,
        client_id: str,
        client_secret: str,
        refresh_token: str,
        cookies: dict[str, str] | None = None,
    ) -> None:
        self.api_base_url = api_base_url.rstrip("/")
        self.client_id = client_id
        self.client_secret = client_secret
        self.refresh_token = refresh_token
        self.cookies = cookies or {}

    async def fetch_me(self, access_token: str | None = None) -> dict:
        if access_token is None:
            access_token = await self.refresh_access_token()
        me_url = f"{self.api_base_url}/me"
        headers = {
            "Authorization": f"Bearer {access_token}",
            "Accept": "application/json",
        }
        try:
            async with httpx.AsyncClient(timeout=15.0) as client:
                response = await client.get(me_url, headers=headers, cookies=self.cookies)
                response.raise_for_status()
                return response.json()
        except httpx.HTTPStatusError as exc:
            raise LetterboxdError(
                f"Letterboxd /me returned {exc.response.status_code}",
                status_code=exc.response.status_code,
            ) from exc
        except (httpx.RequestError, json.JSONDecodeError) as exc:
            raise LetterboxdError("Letterboxd /me request failed") from exc

    async def refresh_access_token(self) -> str:
        token_url = f"{self.api_base_url}/auth/token"
        payload = {
            "grant_type": "refresh_token",
            "refresh_token": self.refresh_token,
            "client_id": self.client_id,
            "client_secret": self.client_secret,
        }
        headers = {"Accept": "application/json"}
        try:
            async with httpx.AsyncClient(timeout=15.0) as client:
                response = await client.post(
                    token_url,
                    data=payload,
                    headers=headers,
                    cookies=self.cookies,
                )
                if response.status_code == 404:
                    fallback_url = f"{self.api_base_url}/oauth/token"
                    response = await client.post(
                        fallback_url,
                        data=payload,
                        headers=headers,
                        cookies=self.cookies,
                    )
                response.raise_for_status()
                data = response.json()
        except httpx.HTTPStatusError as exc:
            raise LetterboxdError(
                f"Letterboxd token refresh returned {exc.response.status_code}",
                status_code=exc.response.status_code,
            ) from exc
        except (httpx.RequestError, json.JSONDecodeError) as exc:
            raise LetterboxdError("Letterboxd token refresh failed") from exc

        access_token = data.get("access_token")
        if not access_token:
            raise LetterboxdError("Letterboxd token response missing access_token")
        return str(access_token)

    async def log_watch(
        self,
        imdb_id: str | None,
        tmdb_id: str | None,
        watched_at: datetime,
        rewatch: bool,
        rating: float | None = None,
        tags: list[str] | None = None,
        like: bool | None = None,
        access_token: str | None = None,
        film_id: str | None = None,
    ) -> tuple[dict[str, Any], int]:
        if not access_token:
            access_token = await self.refresh_access_token()
        if not film_id:
            film_id = await self.resolve_film_id(access_token, imdb_id, tmdb_id)
        payload = {
            "filmId": film_id,
            "diaryDetails": {
                "diaryDate": watched_at.date().isoformat(),
                "rewatch": rewatch,
            },
        }
        if rating is not None:
            payload["rating"] = rating
        if tags:
            payload["tags"] = tags
        if like is not None:
            payload["like"] = like
        data, status_code = await self._post_json(
            "/log-entries",
            access_token,
            payload,
        )
        return data, status_code

    async def update_log_entry_rating(
        self,
        entry_id: str,
        rating: float,
        access_token: str | None = None,
    ) -> tuple[dict[str, Any], int]:
        if not access_token:
            access_token = await self.refresh_access_token()
        payload = {"rating": rating}
        path = f"/log-entry/{entry_id}"
        return await self._update_json(path, access_token, payload)

    async def update_log_entry(
        self,
        entry_id: str,
        watched_at: datetime | None = None,
        rating: float | None = None,
        rewatch: bool | None = None,
        tags: list[str] | None = None,
        like: bool | None = None,
        access_token: str | None = None,
    ) -> tuple[dict[str, Any], int]:
        if not access_token:
            access_token = await self.refresh_access_token()
        payload: dict[str, Any] = {}
        diary_details: dict[str, Any] = {}
        if watched_at is not None:
            diary_details["diaryDate"] = watched_at.date().isoformat()
        if rewatch is not None:
            diary_details["rewatch"] = rewatch
        if diary_details:
            payload["diaryDetails"] = diary_details
        if rating is not None:
            payload["rating"] = rating
        if tags is not None:
            payload["tags"] = tags
        if like is not None:
            payload["like"] = like
        if not payload:
            raise LetterboxdError("No updates provided for log entry")
        path = f"/log-entry/{entry_id}"
        return await self._update_json(path, access_token, payload)

    async def resolve_film_id(
        self,
        access_token: str,
        imdb_id: str | None,
        tmdb_id: str | None,
    ) -> str:
        candidates: list[tuple[str, dict[str, str] | None]] = []
        if tmdb_id:
            tmdb_key = _prefix_external_id("tmdb", tmdb_id)
            candidates.extend(
                [
                    (f"/film/{tmdb_key}", None),
                    ("/films", {"filmId": tmdb_key, "perPage": "1"}),
                ]
            )
        if imdb_id:
            imdb_key = _prefix_external_id("imdb", imdb_id)
            candidates.extend(
                [
                    ("/films", {"filmId": imdb_key, "perPage": "1"}),
                    ("/search", {"input": imdb_id, "type": "film"}),
                ]
            )
        if not candidates:
            raise LetterboxdError("Letterboxd film lookup requires an IMDb or TMDB ID")
        attempts: list[str] = []
        last_error: LetterboxdError | None = None
        for path, params in candidates:
            try:
                payload = await self._get_json(path, access_token, params=params)
            except LetterboxdError as exc:
                if exc.status_code == 404:
                    attempts.append(_describe_attempt(path, params, "status=404"))
                    last_error = exc
                    continue
                raise
            film_id = _extract_film_id(payload)
            if film_id:
                if attempts:
                    logger.info(
                        "Letterboxd film lookup succeeded via %s after %s attempt(s)",
                        path,
                        len(attempts) + 1,
                    )
                return film_id
            attempts.append(_describe_attempt(path, params, "no_results"))
        message = _format_lookup_failure(imdb_id, tmdb_id, attempts)
        if last_error:
            raise LetterboxdError(
                message,
                status_code=last_error.status_code,
                response_body=last_error.response_body,
            )
        raise LetterboxdError(message, status_code=404)

    async def fetch_log_entries(
        self,
        access_token: str,
        film_id: str,
        cursor: str | None = None,
        per_page: int = 20,
        member_id: str | None = None,
        member_relationship: str = "Owner",
    ) -> dict[str, Any]:
        if not member_id:
            raise LetterboxdError("Letterboxd log entry lookup requires member id")
        params = {
            "film": film_id,
            "perPage": str(per_page),
            "member": member_id,
            "memberRelationship": member_relationship,
        }
        if cursor:
            params["cursor"] = cursor
        return await self._get_json("/log-entries", access_token, params=params)

    async def check_log_entries_for_date(
        self,
        access_token: str,
        film_id: str,
        watched_date: date,
        max_pages: int = 5,
    ) -> LogEntryCheck:
        me_payload = await self.fetch_me(access_token=access_token)
        member_id = _extract_member_id(me_payload)
        if not member_id:
            raise LetterboxdError("Letterboxd /me response missing member id")
        cursor: str | None = None
        has_any_entries = False
        for _ in range(max_pages):
            payload = await self.fetch_log_entries(
                access_token,
                film_id,
                cursor=cursor,
                member_id=member_id,
            )
            entries = _extract_log_entries(payload)
            if not entries:
                break
            filtered_entries: list[dict[str, Any]] = []
            saw_film_id = False
            for entry in entries:
                entry_film_id = _extract_log_entry_film_id(entry)
                if entry_film_id:
                    saw_film_id = True
                    if entry_film_id != film_id:
                        continue
                filtered_entries.append(entry)
            if saw_film_id:
                entries = filtered_entries
            if entries:
                has_any_entries = True
            for entry in entries:
                entry_date = _extract_log_entry_date(entry)
                if entry_date and entry_date == watched_date:
                    return LogEntryCheck(
                        already_logged_today=True,
                        has_any_entries=True,
                        entry_id=_extract_log_entry_id(entry),
                    )
            next_cursor = _extract_next_cursor(payload)
            if not next_cursor or next_cursor == cursor:
                break
            cursor = next_cursor
        return LogEntryCheck(
            already_logged_today=False,
            has_any_entries=has_any_entries,
            entry_id=None,
        )

    async def fetch_recent_log_entries(
        self,
        access_token: str,
        since: datetime,
        member_id: str | None = None,
        per_page: int = 20,
        max_pages: int = 10,
        year: int | None = None,
        month: int | None = None,
    ) -> list[dict[str, Any]]:
        if since.tzinfo is None:
            since = since.replace(tzinfo=timezone.utc)
        if not member_id:
            me_payload = await self.fetch_me(access_token=access_token)
            member_id = _extract_member_id(me_payload)
        if not member_id:
            raise LetterboxdError("Letterboxd /me response missing member id")
        since_date = since.date()
        try:
            path, base_params, payload = await self._resolve_log_entries_strategy(
                access_token,
                per_page,
                member_id=member_id,
                year=year,
                month=month,
            )
        except LetterboxdError as exc:
            if year is None and month is None:
                raise
            if exc.status_code not in {400, 404}:
                raise
            logger.info(
                "Letterboxd log entry filters not supported, retrying without month/year"
            )
            path, base_params, payload = await self._resolve_log_entries_strategy(
                access_token,
                per_page,
                member_id=member_id,
            )
        entries: list[dict[str, Any]] = []
        cursor: str | None = None
        for page_index in range(max_pages):
            page_payload = payload
            if page_index > 0:
                params = dict(base_params)
                if cursor:
                    params["cursor"] = cursor
                page_payload = await self._get_json(path, access_token, params=params)
            page_entries = _extract_log_entries(page_payload)
            if not page_entries:
                break
            saw_recent = False
            hit_cutoff = False
            for entry in page_entries:
                entry_dt = _extract_log_entry_datetime(entry)
                if entry_dt is None:
                    continue
                if entry_dt.date() >= since_date:
                    entries.append(entry)
                    saw_recent = True
                    continue
                if saw_recent:
                    hit_cutoff = True
                    break
            next_cursor = _extract_next_cursor(page_payload)
            if hit_cutoff or not saw_recent:
                break
            if not next_cursor or next_cursor == cursor:
                break
            cursor = next_cursor
        return entries

    async def _resolve_log_entries_strategy(
        self,
        access_token: str,
        per_page: int,
        member_id: str | None = None,
        year: int | None = None,
        month: int | None = None,
    ) -> tuple[str, dict[str, str], dict[str, Any]]:
        if not member_id:
            raise LetterboxdError("Letterboxd log entry lookup requires member id")
        member_value = member_id
        base_params = {
            "perPage": str(per_page),
            "sort": "Date",
            "member": member_value,
            "memberRelationship": "Owner",
            "where": "HasDiaryDate",
        }
        if year is not None:
            base_params["year"] = str(year)
        if month is not None:
            base_params["month"] = str(month)
        try:
            payload = await self._get_json(
                "/log-entries", access_token, params=base_params
            )
            return "/log-entries", base_params, payload
        except LetterboxdError as exc:
            if exc.status_code not in {400, 404}:
                raise

        raise LetterboxdError(
            "Letterboxd log entry lookup failed", status_code=404
        )

    async def _get_json(
        self, path: str, access_token: str, params: dict[str, str] | None = None
    ) -> dict[str, Any]:
        response = await self._request(
            "GET",
            path,
            access_token,
            params=params,
        )
        try:
            return response.json()
        except json.JSONDecodeError as exc:
            raise LetterboxdError("Letterboxd response was not JSON") from exc

    async def _post_json(
        self,
        path: str,
        access_token: str,
        payload: dict[str, Any],
        fallback_paths: tuple[str, ...] = (),
    ) -> tuple[dict[str, Any], int]:
        paths = (path, *fallback_paths)
        last_error: LetterboxdError | None = None
        for target in paths:
            try:
                response = await self._request(
                    "POST",
                    target,
                    access_token,
                    json_body=payload,
                )
                if not response.content:
                    return {}, response.status_code
                return response.json(), response.status_code
            except LetterboxdError as exc:
                last_error = exc
                if exc.status_code in {400, 404, 415}:
                    try:
                        response = await self._request(
                            "POST",
                            target,
                            access_token,
                            data=payload,
                        )
                        if not response.content:
                            return {}, response.status_code
                        try:
                            return response.json(), response.status_code
                        except json.JSONDecodeError as inner_exc:
                            raise LetterboxdError(
                                "Letterboxd response was not JSON"
                            ) from inner_exc
                    except LetterboxdError as inner_exc:
                        last_error = inner_exc
                continue
            except json.JSONDecodeError as exc:
                raise LetterboxdError("Letterboxd response was not JSON") from exc
        if last_error:
            raise last_error
        raise LetterboxdError("Letterboxd request failed")

    async def _update_json(
        self,
        path: str,
        access_token: str,
        payload: dict[str, Any],
        methods: tuple[str, ...] = ("PATCH", "PUT", "POST"),
    ) -> tuple[dict[str, Any], int]:
        last_error: LetterboxdError | None = None
        for method in methods:
            try:
                response = await self._request(
                    method,
                    path,
                    access_token,
                    json_body=payload,
                )
                if not response.content:
                    return {}, response.status_code
                return response.json(), response.status_code
            except LetterboxdError as exc:
                last_error = exc
                if exc.status_code in {400, 404, 405, 415}:
                    continue
                raise
            except json.JSONDecodeError as exc:
                raise LetterboxdError("Letterboxd response was not JSON") from exc
        if last_error:
            raise last_error
        raise LetterboxdError("Letterboxd request failed")

    async def _request(
        self,
        method: str,
        path: str,
        access_token: str,
        params: dict[str, str] | None = None,
        data: dict[str, Any] | None = None,
        json_body: dict[str, Any] | None = None,
    ) -> httpx.Response:
        url = path if path.startswith("http") else f"{self.api_base_url}{path}"
        headers = {
            "Authorization": f"Bearer {access_token}",
            "Accept": "application/json",
        }
        try:
            async with httpx.AsyncClient(timeout=15.0) as client:
                response = await client.request(
                    method,
                    url,
                    headers=headers,
                    params=params,
                    data=data,
                    json=json_body,
                    cookies=self.cookies,
                )
                response.raise_for_status()
                return response
        except httpx.HTTPStatusError as exc:
            raise LetterboxdError(
                f"Letterboxd request returned {exc.response.status_code}",
                status_code=exc.response.status_code,
                response_body=_safe_body(exc.response.text),
            ) from exc
        except httpx.RequestError as exc:
            raise LetterboxdError("Letterboxd request failed") from exc


def _extract_film_id(payload: Any) -> str | None:
    if isinstance(payload, dict):
        film = payload.get("film")
        if isinstance(film, dict):
            film_id = film.get("id")
            if film_id:
                return str(film_id)
        if isinstance(film, str):
            return film
        for key in ("id", "filmId", "film_id"):
            value = payload.get(key)
            if isinstance(value, (str, int)):
                return str(value)
        items = payload.get("items") or payload.get("results")
        if isinstance(items, list):
            for item in items:
                film_id = _extract_film_id(item)
                if film_id:
                    return film_id
    if isinstance(payload, list):
        for item in payload:
            film_id = _extract_film_id(item)
            if film_id:
                return film_id
    return None


def _safe_body(value: str | None, limit: int = 500) -> str | None:
    if not value:
        return None
    trimmed = value.strip()
    if len(trimmed) > limit:
        return f"{trimmed[:limit]}..."
    return trimmed


def _describe_attempt(
    path: str, params: dict[str, str] | None, outcome: str
) -> str:
    if not params:
        return f"{path}:{outcome}"
    query = "&".join(f"{key}={value}" for key, value in params.items())
    return f"{path}?{query}:{outcome}"


def _format_lookup_failure(
    imdb_id: str | None, tmdb_id: str | None, attempts: list[str]
) -> str:
    imdb_label = imdb_id or "-"
    tmdb_label = tmdb_id or "-"
    message = (
        "Letterboxd film lookup failed "
        f"(imdb_id={imdb_label}, tmdb_id={tmdb_label})"
    )
    if attempts:
        joined = "; ".join(attempts)
        return f"{message} attempts={joined}"
    return message


def _prefix_external_id(prefix: str, value: str) -> str:
    lowered = value.lower()
    expected = f"{prefix}:"
    if lowered.startswith(expected):
        return value
    return f"{prefix}:{value}"


def _extract_log_entries(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, dict):
        items = (
            payload.get("items")
            or payload.get("results")
            or payload.get("entries")
            or payload.get("logEntries")
        )
        if isinstance(items, list):
            return [item for item in items if isinstance(item, dict)]
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, dict)]
    return []


def _extract_log_entry_id(payload: Any) -> str | None:
    if isinstance(payload, dict):
        for key in ("id", "entryId", "diaryEntryId", "logEntryId"):
            value = payload.get(key)
            if isinstance(value, (str, int)):
                return str(value)
        entry = payload.get("entry")
        if isinstance(entry, dict):
            for key in ("id", "entryId", "logEntryId"):
                value = entry.get(key)
                if isinstance(value, (str, int)):
                    return str(value)
    return None


def _extract_log_entry_film_id(payload: Any) -> str | None:
    if not isinstance(payload, dict):
        return None
    candidates = [payload]
    nested = payload.get("entry")
    if isinstance(nested, dict):
        candidates.append(nested)
    for candidate in candidates:
        film_value = candidate.get("film")
        if isinstance(film_value, dict):
            film_id = film_value.get("id") or film_value.get("filmId")
            if isinstance(film_id, (str, int)):
                return str(film_id)
        if isinstance(film_value, str):
            return film_value
        for key in ("filmId", "film_id"):
            film_id = candidate.get(key)
            if isinstance(film_id, (str, int)):
                return str(film_id)
    return None


def _extract_next_cursor(payload: Any) -> str | None:
    if not isinstance(payload, dict):
        return None
    for key in ("nextCursor", "next_cursor", "next", "cursor"):
        value = payload.get(key)
        if isinstance(value, str):
            return value
        if isinstance(value, dict):
            nested = value.get("cursor") or value.get("nextCursor") or value.get(
                "next_cursor"
            )
            if isinstance(nested, str):
                return nested
    return None


def _extract_log_entry_date(payload: Any) -> date | None:
    if not isinstance(payload, dict):
        return None
    for candidate in (payload, payload.get("entry")):
        if not isinstance(candidate, dict):
            continue
        for key in ("diaryDate", "date", "watchedDate"):
            parsed = _parse_date_value(candidate.get(key))
            if parsed:
                return parsed
        diary = candidate.get("diaryDetails") or candidate.get("diary")
        if isinstance(diary, dict):
            for key in ("diaryDate", "date"):
                parsed = _parse_date_value(diary.get(key))
                if parsed:
                    return parsed
    return None


def _extract_log_entry_datetime(payload: Any) -> datetime | None:
    if not isinstance(payload, dict):
        return None
    for candidate in (payload, payload.get("entry")):
        if not isinstance(candidate, dict):
            continue
        for key in (
            "diaryDate",
            "watchedDate",
            "logDate",
            "watchedAt",
            "loggedAt",
            "createdAt",
            "updatedAt",
            "timestamp",
            "date",
        ):
            parsed = _parse_datetime_value(candidate.get(key))
            if parsed:
                return parsed
        diary = candidate.get("diaryDetails") or candidate.get("diary")
        if isinstance(diary, dict):
            for key in ("diaryDate", "date", "watchedDate"):
                parsed = _parse_datetime_value(diary.get(key))
                if parsed:
                    return parsed
    return None


def _parse_date_value(value: Any) -> date | None:
    if not isinstance(value, str):
        return None
    cleaned = value.strip()
    if not cleaned:
        return None
    try:
        return date.fromisoformat(cleaned)
    except ValueError:
        pass
    try:
        if cleaned.endswith("Z"):
            cleaned = f"{cleaned[:-1]}+00:00"
        parsed = datetime.fromisoformat(cleaned)
    except ValueError:
        return None
    return parsed.date()


def _parse_datetime_value(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    cleaned = value.strip()
    if not cleaned:
        return None
    try:
        if cleaned.endswith("Z"):
            cleaned = f"{cleaned[:-1]}+00:00"
        parsed = datetime.fromisoformat(cleaned)
    except ValueError:
        parsed = None
    if parsed:
        if parsed.tzinfo is None:
            return parsed.replace(tzinfo=timezone.utc)
        return parsed
    date_value = _parse_date_value(cleaned)
    if not date_value:
        return None
    return datetime(
        date_value.year,
        date_value.month,
        date_value.day,
        tzinfo=timezone.utc,
    )


def extract_member_id(payload: Any) -> str | None:
    return _extract_member_id(payload)


def _extract_member_id(payload: Any) -> str | None:
    if isinstance(payload, dict):
        for key in ("id", "memberId", "member_id"):
            value = payload.get(key)
            if isinstance(value, (str, int)):
                return str(value)
        member = payload.get("member")
        if isinstance(member, dict):
            for key in ("id", "memberId", "member_id"):
                value = member.get(key)
                if isinstance(value, (str, int)):
                    return str(value)
    return None


def _extract_member_name(payload: Any) -> str | None:
    if isinstance(payload, dict):
        for key in ("username", "memberName", "name", "handle"):
            value = payload.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
        member = payload.get("member")
        if isinstance(member, dict):
            for key in ("username", "memberName", "name", "handle"):
                value = member.get(key)
                if isinstance(value, str) and value.strip():
                    return value.strip()
    return None
