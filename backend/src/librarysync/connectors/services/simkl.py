from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.parse import urlencode

import httpx

from librarysync.connectors.services.base import ServiceConnector
from librarysync.core.canonical import ProgressEvent
from librarysync.core.http_client import get_http_client

DEFAULT_SIMKL_API_BASE_URL = "https://api.simkl.com"
SIMKL_OAUTH_AUTHORIZE_URL = "https://simkl.com/oauth/authorize"
SIMKL_OAUTH_TOKEN_URL = "https://api.simkl.com/oauth/token"
SIMKL_REQUIRED_FIELDS = ("access_token",)


@dataclass(frozen=True)
class SimklToken:
    access_token: str
    refresh_token: str
    expires_at: datetime | None = None
    scope: str | None = None
    token_type: str | None = None


class SimklError(RuntimeError):
    def __init__(
        self,
        message: str,
        status_code: int | None = None,
        response_body: str | None = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.response_body = response_body


def has_required_simkl_fields(values: Mapping[str, object]) -> bool:
    for field in SIMKL_REQUIRED_FIELDS:
        value = values.get(field)
        if not isinstance(value, str) or not value:
            return False
    return True


def parse_expires_at(value: object) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(float(value), tz=timezone.utc)
    if isinstance(value, str):
        cleaned = value.strip()
        if not cleaned:
            return None
        if cleaned.isdigit():
            try:
                return datetime.fromtimestamp(float(cleaned), tz=timezone.utc)
            except ValueError:
                return None
        try:
            if cleaned.endswith("Z"):
                cleaned = f"{cleaned[:-1]}+00:00"
            parsed = datetime.fromisoformat(cleaned)
            return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
        except ValueError:
            return None
    return None


def is_token_expired(expires_at: datetime | None, skew_seconds: int = 60) -> bool:
    if expires_at is None:
        return False
    now = datetime.now(timezone.utc)
    return expires_at <= (now + timedelta(seconds=skew_seconds))


def normalize_token_payload(payload: Mapping[str, Any]) -> SimklToken:
    error = payload.get("error")
    if isinstance(error, str) and error.strip():
        description = payload.get("error_description")
        detail = f": {description}" if isinstance(description, str) and description else ""
        raise SimklError(f"SIMKL token response error {error}{detail}")
    access_token = str(payload.get("access_token") or "").strip()
    refresh_token = str(payload.get("refresh_token") or "").strip()
    if not access_token:
        available = ", ".join(sorted(str(key) for key in payload.keys()))
        raise SimklError(
            "SIMKL token response missing access_token"
            + (f" (keys={available})" if available else "")
        )
    created_at = payload.get("created_at")
    expires_in = payload.get("expires_in")
    expires_at: datetime | None = None
    if isinstance(created_at, (int, float)) and isinstance(expires_in, (int, float)):
        expires_at = datetime.fromtimestamp(float(created_at) + float(expires_in), tz=timezone.utc)
    elif isinstance(expires_in, (int, float)):
        expires_at = datetime.now(timezone.utc) + timedelta(seconds=float(expires_in))
    scope = payload.get("scope")
    token_type = payload.get("token_type")
    return SimklToken(
        access_token=access_token,
        refresh_token=refresh_token,
        expires_at=expires_at,
        scope=str(scope) if isinstance(scope, str) else None,
        token_type=str(token_type) if isinstance(token_type, str) else None,
    )


def token_to_secret_payload(token: SimklToken) -> dict[str, object]:
    payload: dict[str, object] = {
        "access_token": token.access_token,
    }
    if token.refresh_token:
        payload["refresh_token"] = token.refresh_token
    if token.expires_at:
        payload["expires_at"] = token.expires_at.isoformat()
    if token.scope:
        payload["scope"] = token.scope
    if token.token_type:
        payload["token_type"] = token.token_type
    return payload


class SimklClient:
    def __init__(
        self,
        client_id: str,
        client_secret: str,
        api_base_url: str = DEFAULT_SIMKL_API_BASE_URL,
        authorize_url: str = SIMKL_OAUTH_AUTHORIZE_URL,
        token_url: str = SIMKL_OAUTH_TOKEN_URL,
    ) -> None:
        self.client_id = client_id
        self.client_secret = client_secret
        self.api_base_url = api_base_url.rstrip("/")
        self.authorize_url = authorize_url
        self.token_url = token_url

    def build_authorize_url(self, redirect_uri: str, state: str) -> str:
        params = urlencode(
            {
                "response_type": "code",
                "client_id": self.client_id,
                "redirect_uri": redirect_uri,
                "state": state,
            }
        )
        return f"{self.authorize_url}?{params}"

    async def exchange_code(self, code: str, redirect_uri: str) -> SimklToken:
        payload = {
            "code": code,
            "client_id": self.client_id,
            "client_secret": self.client_secret,
            "redirect_uri": redirect_uri,
            "grant_type": "authorization_code",
        }
        data = await self._post_json(self.token_url, payload)
        return normalize_token_payload(data)

    async def refresh_access_token(
        self, refresh_token: str, redirect_uri: str | None = None
    ) -> SimklToken:
        payload = {
            "refresh_token": refresh_token,
            "client_id": self.client_id,
            "client_secret": self.client_secret,
            "grant_type": "refresh_token",
        }
        if redirect_uri:
            payload["redirect_uri"] = redirect_uri
        data = await self._post_json(self.token_url, payload)
        return normalize_token_payload(data)

    async def fetch_me(self, access_token: str) -> dict[str, Any]:
        response = await self._request("GET", "/users/settings", access_token=access_token)
        return self._parse_json(response)

    async def add_history(
        self, payload: dict[str, Any], access_token: str
    ) -> tuple[dict[str, Any], int]:
        response = await self._request(
            "POST", "/sync/history", access_token=access_token, json_body=payload
        )
        parsed = self._parse_json(response)
        return parsed if isinstance(parsed, dict) else {}, response.status_code

    async def remove_history(
        self, payload: dict[str, Any], access_token: str
    ) -> tuple[dict[str, Any], int]:
        response = await self._request(
            "POST", "/sync/history/remove", access_token=access_token, json_body=payload
        )
        parsed = self._parse_json(response)
        return parsed if isinstance(parsed, dict) else {}, response.status_code

    async def add_ratings(
        self, payload: dict[str, Any], access_token: str
    ) -> tuple[dict[str, Any], int]:
        response = await self._request(
            "POST", "/sync/ratings", access_token=access_token, json_body=payload
        )
        parsed = self._parse_json(response)
        return parsed if isinstance(parsed, dict) else {}, response.status_code

    async def add_to_watchlist(
        self, payload: dict[str, Any], access_token: str
    ) -> tuple[dict[str, Any], int]:
        response = await self._request(
            "POST", "/sync/add-to-list", access_token=access_token, json_body=payload
        )
        parsed = self._parse_json(response)
        return parsed if isinstance(parsed, dict) else {}, response.status_code

    async def remove_from_watchlist(
        self, payload: dict[str, Any], access_token: str
    ) -> tuple[dict[str, Any], int]:
        response = await self._request(
            "POST", "/sync/history/remove", access_token=access_token, json_body=payload
        )
        parsed = self._parse_json(response)
        return parsed if isinstance(parsed, dict) else {}, response.status_code

    async def add_to_list(
        self, payload: dict[str, Any], access_token: str
    ) -> tuple[dict[str, Any], int]:
        response = await self._request(
            "POST", "/sync/add-to-list", access_token=access_token, json_body=payload
        )
        parsed = self._parse_json(response)
        return parsed if isinstance(parsed, dict) else {}, response.status_code

    async def fetch_history(
        self,
        access_token: str,
        history_type: str | None = None,
        start_at: datetime | None = None,
        end_at: datetime | None = None,
        page: int = 1,
        limit: int = 50,
    ) -> tuple[list[dict[str, Any]], dict[str, str], object]:
        params_sets: list[dict[str, str]] = []
        seen: set[tuple[tuple[str, str], ...]] = set()
        for include_dates in (True, False):
            for include_page in (True, False):
                for include_type in (True, False):
                    params: dict[str, str] = {}
                    if include_type and history_type:
                        params["type"] = history_type
                    if include_dates and start_at:
                        params["date_from"] = start_at.date().isoformat()
                    if include_dates and end_at:
                        params["date_to"] = end_at.date().isoformat()
                    if include_page:
                        params["page"] = str(page)
                        params["limit"] = str(limit)
                    key = tuple(sorted(params.items()))
                    if key in seen:
                        continue
                    seen.add(key)
                    params_sets.append(params)
        last_error: SimklError | None = None
        for params in params_sets:
            try:
                response = await self._request(
                    "GET", "/sync/history", access_token=access_token, params=params
                )
            except SimklError as exc:
                last_error = exc
                if exc.status_code not in {400, 404}:
                    raise
                continue
            payload = self._parse_json(response)
            items = _extract_history_items(payload, history_type)
            return items, dict(response.headers), payload
        if last_error:
            raise last_error
        return [], {}, {}

    async def fetch_activities(self, access_token: str) -> dict[str, Any]:
        response = await self._request(
            "POST", "/sync/activities", access_token=access_token, json_body={}
        )
        payload = self._parse_json(response)
        return payload if isinstance(payload, dict) else {}

    async def fetch_all_items(
        self,
        access_token: str,
        category: str | None = None,
        date_from: datetime | None = None,
        extended: str | None = None,
        episode_watched_at: bool = False,
    ) -> dict[str, Any]:
        path = "/sync/all-items"
        if category:
            path = f"{path}/{category}"
        params: dict[str, str] = {}
        if date_from:
            params["date_from"] = date_from.isoformat()
        if extended:
            params["extended"] = extended
        if episode_watched_at:
            params["episode_watched_at"] = "yes"
        response = await self._request("GET", path, access_token=access_token, params=params)
        payload = self._parse_json(response)
        if isinstance(payload, dict):
            return payload
        if isinstance(payload, list):
            if category:
                return {category: payload}
            return {"items": payload}
        return {}

    async def get_history(
        self,
        access_token: str,
        days: int | None = None,
        date_from: datetime | None = None,
        extended: str | None = "full",
        episode_watched_at: bool = True,
    ) -> dict[str, dict[str, Any]]:
        if date_from is None and days is not None:
            date_from = datetime.now(timezone.utc) - timedelta(days=days)
        movies_payload = await self.fetch_all_items(
            access_token,
            category="movies",
            date_from=date_from,
        )
        shows_payload = await self.fetch_all_items(
            access_token,
            category="shows",
            date_from=date_from,
            extended=extended,
            episode_watched_at=episode_watched_at,
        )
        anime_payload = await self.fetch_all_items(
            access_token,
            category="anime",
            date_from=date_from,
            extended=extended,
            episode_watched_at=episode_watched_at,
        )
        return {
            "movies": movies_payload,
            "shows": shows_payload,
            "anime": anime_payload,
        }

    async def _post_json(self, url: str, payload: dict[str, Any]) -> dict[str, Any]:
        headers = {"Accept": "application/json", "Content-Type": "application/json"}
        try:
            async with get_http_client(timeout=15.0, headers=headers) as client:
                response = await client.post(url, json=payload)
                response.raise_for_status()
                return self._parse_json(response)
        except httpx.HTTPStatusError as exc:
            raise SimklError(
                f"SIMKL token request returned {exc.response.status_code}",
                status_code=exc.response.status_code,
                response_body=_safe_body(exc.response.text),
            ) from exc
        except (httpx.RequestError, json.JSONDecodeError) as exc:
            raise SimklError("SIMKL token request failed") from exc

    async def _request(
        self,
        method: str,
        path: str,
        access_token: str,
        params: dict[str, str] | None = None,
        json_body: dict[str, Any] | None = None,
    ) -> httpx.Response:
        url = path if path.startswith("http") else f"{self.api_base_url}{path}"
        headers = {
            "Authorization": f"Bearer {access_token}",
            "simkl-api-key": self.client_id,
            "Accept": "application/json",
        }
        try:
            async with get_http_client(timeout=15.0, headers=headers) as client:
                response = await client.request(method, url, params=params, json=json_body)
                response.raise_for_status()
                return response
        except httpx.HTTPStatusError as exc:
            raise SimklError(
                f"SIMKL request returned {exc.response.status_code}",
                status_code=exc.response.status_code,
                response_body=_safe_body(exc.response.text),
            ) from exc
        except httpx.RequestError as exc:
            raise SimklError("SIMKL request failed") from exc

    def _parse_json(self, response: httpx.Response) -> Any:
        if not response.content:
            return {}
        try:
            payload = response.json()
        except json.JSONDecodeError as exc:
            raise SimklError("SIMKL response was not JSON") from exc
        return payload


def _extract_history_items(payload: object, history_type: str | None) -> list[dict[str, Any]]:
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, dict)]
    if not isinstance(payload, dict):
        return []
    for key in ("items", "history", "added"):
        value = payload.get(key)
        if isinstance(value, list):
            return [item for item in value if isinstance(item, dict)]
        if isinstance(value, dict) and history_type:
            items = _extract_typed_history(value, history_type)
            if items:
                return items
    if history_type:
        items = _extract_typed_history(payload, history_type)
        if items:
            return items
    return []


def _extract_typed_history(payload: dict[str, Any], history_type: str) -> list[dict[str, Any]]:
    keys = _history_keys(history_type)
    for key in keys:
        items = payload.get(key)
        if isinstance(items, list):
            return [item for item in items if isinstance(item, dict)]
        if isinstance(items, dict):
            nested = _extract_history_list_from_container(items)
            if nested:
                return nested
            if _looks_like_entry(items):
                return [items]
    return []


def _history_keys(history_type: str) -> list[str]:
    cleaned = history_type.strip().lower()
    keys = [cleaned]
    if cleaned.endswith("s"):
        keys.append(cleaned[:-1])
    return list(dict.fromkeys(keys))


def _extract_history_list_from_container(payload: dict[str, Any]) -> list[dict[str, Any]]:
    for key in ("items", "history", "entries", "list", "records", "episodes"):
        value = payload.get(key)
        if isinstance(value, list):
            return [item for item in value if isinstance(item, dict)]
        if isinstance(value, dict):
            nested = _extract_history_list_from_container(value)
            if nested:
                return nested
    return []


def _looks_like_entry(payload: dict[str, Any]) -> bool:
    if "ids" in payload:
        return True
    if "title" in payload and ("watched_at" in payload or "last_watched_at" in payload):
        return True
    return False


class SimklConnector(ServiceConnector):
    async def oauth_start(self, user_id: str) -> str:
        raise NotImplementedError("SIMKL OAuth start not implemented")

    async def oauth_callback(self, user_id: str, code: str, state: str) -> None:
        raise NotImplementedError("SIMKL OAuth callback not implemented")

    async def refresh_token_if_needed(self, user_id: str) -> None:
        raise NotImplementedError("SIMKL token refresh not implemented")

    async def push_progress(self, user_id: str, event: ProgressEvent) -> None:
        raise NotImplementedError("SIMKL progress push not implemented")

    async def push_completed(self, user_id: str, event: ProgressEvent) -> None:
        raise NotImplementedError("SIMKL completion push not implemented")


def _safe_body(value: str | None, limit: int = 500) -> str | None:
    if not value:
        return None
    trimmed = value.strip()
    if len(trimmed) > limit:
        return f"{trimmed[:limit]}..."
    return trimmed
