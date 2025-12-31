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

DEFAULT_TRAKT_API_BASE_URL = "https://api.trakt.tv"
TRAKT_OAUTH_AUTHORIZE_URL = "https://api.trakt.tv/oauth/authorize"
TRAKT_OAUTH_TOKEN_URL = "https://api.trakt.tv/oauth/token"
TRAKT_REQUIRED_FIELDS = ("access_token", "refresh_token")


@dataclass(frozen=True)
class TraktToken:
    access_token: str
    refresh_token: str
    expires_at: datetime | None = None
    scope: str | None = None
    token_type: str | None = None


class TraktError(RuntimeError):
    def __init__(
        self,
        message: str,
        status_code: int | None = None,
        response_body: str | None = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.response_body = response_body


def has_required_trakt_fields(values: Mapping[str, object]) -> bool:
    for field in TRAKT_REQUIRED_FIELDS:
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
        return True
    now = datetime.now(timezone.utc)
    return expires_at <= (now + timedelta(seconds=skew_seconds))


def normalize_token_payload(payload: Mapping[str, Any]) -> TraktToken:
    access_token = str(payload.get("access_token") or "").strip()
    refresh_token = str(payload.get("refresh_token") or "").strip()
    if not access_token or not refresh_token:
        raise TraktError("Trakt token response missing access_token or refresh_token")
    created_at = payload.get("created_at")
    expires_in = payload.get("expires_in")
    expires_at: datetime | None = None
    if isinstance(created_at, (int, float)) and isinstance(expires_in, (int, float)):
        expires_at = datetime.fromtimestamp(
            float(created_at) + float(expires_in), tz=timezone.utc
        )
    elif isinstance(expires_in, (int, float)):
        expires_at = datetime.now(timezone.utc) + timedelta(seconds=float(expires_in))
    scope = payload.get("scope")
    token_type = payload.get("token_type")
    return TraktToken(
        access_token=access_token,
        refresh_token=refresh_token,
        expires_at=expires_at,
        scope=str(scope) if isinstance(scope, str) else None,
        token_type=str(token_type) if isinstance(token_type, str) else None,
    )


def token_to_secret_payload(token: TraktToken) -> dict[str, object]:
    payload: dict[str, object] = {
        "access_token": token.access_token,
        "refresh_token": token.refresh_token,
    }
    if token.expires_at:
        payload["expires_at"] = token.expires_at.isoformat()
    if token.scope:
        payload["scope"] = token.scope
    if token.token_type:
        payload["token_type"] = token.token_type
    return payload


class TraktClient:
    def __init__(
        self,
        client_id: str,
        client_secret: str,
        api_base_url: str = DEFAULT_TRAKT_API_BASE_URL,
        authorize_url: str = TRAKT_OAUTH_AUTHORIZE_URL,
        token_url: str = TRAKT_OAUTH_TOKEN_URL,
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

    async def exchange_code(self, code: str, redirect_uri: str) -> TraktToken:
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
    ) -> TraktToken:
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
        response = await self._request("GET", "/users/me", access_token=access_token)
        return self._parse_json(response)

    async def add_history(
        self, payload: dict[str, Any], access_token: str
    ) -> tuple[dict[str, Any], int]:
        response = await self._request(
            "POST", "/sync/history", access_token=access_token, json_body=payload
        )
        parsed = self._parse_json(response)
        return parsed if isinstance(parsed, dict) else {}, response.status_code

    async def update_history(
        self, history_id: str, watched_at: datetime, access_token: str
    ) -> tuple[dict[str, Any], int]:
        payload = {"watched_at": watched_at.isoformat()}
        path = f"/sync/history/{history_id}"
        response = await self._request(
            "PUT", path, access_token=access_token, json_body=payload
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

    async def fetch_history(
        self,
        access_token: str,
        history_type: str | None = None,
        start_at: datetime | None = None,
        end_at: datetime | None = None,
        page: int = 1,
        limit: int = 50,
    ) -> tuple[list[dict[str, Any]], dict[str, str]]:
        path = "/sync/history"
        if history_type:
            path = f"/sync/history/{history_type}"
        params: dict[str, str] = {"page": str(page), "limit": str(limit)}
        if start_at:
            params["start_at"] = start_at.isoformat()
        if end_at:
            params["end_at"] = end_at.isoformat()
        response = await self._request(
            "GET", path, access_token=access_token, params=params
        )
        payload = self._parse_json(response)
        if isinstance(payload, list):
            items = payload
        elif isinstance(payload, dict) and isinstance(payload.get("items"), list):
            items = payload["items"]
        else:
            items = []
        return items, dict(response.headers)

    async def get_history(
        self,
        access_token: str,
        history_type: str | None = None,
        days: int | None = None,
        start_at: datetime | None = None,
        end_at: datetime | None = None,
        per_page: int = 50,
        max_pages: int = 10,
    ) -> list[dict[str, Any]]:
        if start_at is None and days is not None:
            start_at = datetime.now(timezone.utc) - timedelta(days=days)
        entries: list[dict[str, Any]] = []
        page = 1
        while page <= max_pages:
            items, headers = await self.fetch_history(
                access_token,
                history_type=history_type,
                start_at=start_at,
                end_at=end_at,
                page=page,
                limit=per_page,
            )
            if not items:
                break
            entries.extend(item for item in items if isinstance(item, dict))
            page_count = _parse_page_count(headers)
            if page_count and page >= page_count:
                break
            if len(items) < per_page:
                break
            page += 1
        return entries

    async def fetch_ratings(
        self,
        access_token: str,
        rating_type: str | None = None,
        page: int = 1,
        limit: int = 50,
    ) -> tuple[list[dict[str, Any]], dict[str, str]]:
        path = "/sync/ratings"
        if rating_type:
            path = f"{path}/{rating_type}"
        params: dict[str, str] = {"page": str(page), "limit": str(limit)}
        response = await self._request(
            "GET", path, access_token=access_token, params=params
        )
        payload = self._parse_json(response)
        if isinstance(payload, list):
            items = payload
        elif isinstance(payload, dict) and isinstance(payload.get("items"), list):
            items = payload["items"]
        else:
            items = []
        return items, dict(response.headers)

    async def get_ratings(
        self,
        access_token: str,
        rating_type: str | None = None,
        per_page: int = 50,
        max_pages: int = 10,
    ) -> list[dict[str, Any]]:
        entries: list[dict[str, Any]] = []
        page = 1
        while page <= max_pages:
            items, headers = await self.fetch_ratings(
                access_token,
                rating_type=rating_type,
                page=page,
                limit=per_page,
            )
            if not items:
                break
            entries.extend(item for item in items if isinstance(item, dict))
            page_count = _parse_page_count(headers)
            if page_count and page >= page_count:
                break
            if len(items) < per_page:
                break
            page += 1
        return entries

    async def _post_json(self, url: str, payload: dict[str, Any]) -> dict[str, Any]:
        headers = {"Accept": "application/json", "Content-Type": "application/json"}
        try:
            async with httpx.AsyncClient(timeout=15.0) as client:
                response = await client.post(url, json=payload, headers=headers)
                response.raise_for_status()
                return self._parse_json(response)
        except httpx.HTTPStatusError as exc:
            raise TraktError(
                f"Trakt token request returned {exc.response.status_code}",
                status_code=exc.response.status_code,
                response_body=_safe_body(exc.response.text),
            ) from exc
        except (httpx.RequestError, json.JSONDecodeError) as exc:
            raise TraktError("Trakt token request failed") from exc

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
            "trakt-api-version": "2",
            "trakt-api-key": self.client_id,
            "Accept": "application/json",
        }
        try:
            async with httpx.AsyncClient(timeout=15.0) as client:
                response = await client.request(
                    method, url, headers=headers, params=params, json=json_body
                )
                response.raise_for_status()
                return response
        except httpx.HTTPStatusError as exc:
            raise TraktError(
                f"Trakt request returned {exc.response.status_code}",
                status_code=exc.response.status_code,
                response_body=_safe_body(exc.response.text),
            ) from exc
        except httpx.RequestError as exc:
            raise TraktError("Trakt request failed") from exc

    def _parse_json(self, response: httpx.Response) -> Any:
        if not response.content:
            return {}
        try:
            payload = response.json()
        except json.JSONDecodeError as exc:
            raise TraktError("Trakt response was not JSON") from exc
        return payload


class TraktConnector(ServiceConnector):
    async def oauth_start(self, user_id: str) -> str:
        raise NotImplementedError("Trakt OAuth start not implemented")

    async def oauth_callback(self, user_id: str, code: str, state: str) -> None:
        raise NotImplementedError("Trakt OAuth callback not implemented")

    async def refresh_token_if_needed(self, user_id: str) -> None:
        raise NotImplementedError("Trakt token refresh not implemented")

    async def push_progress(self, user_id: str, event: ProgressEvent) -> None:
        raise NotImplementedError("Trakt progress push not implemented")

    async def push_completed(self, user_id: str, event: ProgressEvent) -> None:
        raise NotImplementedError("Trakt completion push not implemented")


def _safe_body(value: str | None, limit: int = 500) -> str | None:
    if not value:
        return None
    trimmed = value.strip()
    if len(trimmed) > limit:
        return f"{trimmed[:limit]}..."
    return trimmed


def _parse_page_count(headers: dict[str, str]) -> int | None:
    value = headers.get("x-pagination-page-count") or headers.get(
        "X-Pagination-Page-Count"
    )
    if not value:
        return None
    try:
        return int(value)
    except ValueError:
        return None
