from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

import httpx

DEFAULT_STREMIO_API_BASE_URL = "https://api.strem.io"
STREMIO_REQUIRED_FIELDS = ("auth_key",)


@dataclass(frozen=True)
class StremioLogin:
    auth_key: str
    user: dict[str, Any]


class StremioError(RuntimeError):
    def __init__(
        self,
        message: str,
        code: int | None = None,
        status_code: int | None = None,
        response_body: str | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.status_code = status_code
        self.response_body = response_body


def has_required_stremio_fields(values: Mapping[str, object]) -> bool:
    for field in STREMIO_REQUIRED_FIELDS:
        value = values.get(field)
        if not isinstance(value, str) or not value:
            return False
    return True


def normalize_login_payload(payload: Mapping[str, Any]) -> StremioLogin:
    auth_key = str(payload.get("authKey") or payload.get("auth_key") or "").strip()
    if not auth_key:
        available = ", ".join(sorted(str(key) for key in payload.keys()))
        raise StremioError(
            "Stremio login response missing authKey"
            + (f" (keys={available})" if available else "")
        )
    user_payload = payload.get("user")
    if not isinstance(user_payload, dict):
        user_payload = {}
    return StremioLogin(auth_key=auth_key, user=user_payload)


class StremioClient:
    def __init__(self, api_base_url: str = DEFAULT_STREMIO_API_BASE_URL) -> None:
        self.api_base_url = api_base_url.rstrip("/")

    async def login(self, email: str, password: str) -> StremioLogin:
        payload = {
            "email": email,
            "password": password,
            "facebook": False,
            "type": "login",
        }
        data = await self._post_json("/api/login", payload)
        return normalize_login_payload(data)

    async def get_user(self, auth_key: str) -> dict[str, Any]:
        payload = {"authKey": auth_key, "type": "GetUser"}
        data = await self._post_json("/api/getUser", payload)
        return data if isinstance(data, dict) else {}

    async def get_library_item_timestamps(self, auth_key: str) -> list[Any]:
        payload = {"authKey": auth_key, "collection": "libraryItem"}
        data = await self._post_json("/api/datastoreMeta", payload)
        if isinstance(data, list):
            return data
        return []

    async def get_library_items(
        self, auth_key: str, ids: list[str] | None = None
    ) -> list[dict[str, Any]]:
        payload: dict[str, Any] = {"authKey": auth_key, "collection": "libraryItem"}
        if ids:
            payload["ids"] = ids
            payload["all"] = False
        else:
            payload["all"] = True
        data = await self._post_json("/api/datastoreGet", payload)
        if isinstance(data, list):
            return [item for item in data if isinstance(item, dict)]
        return []

    async def update_library_items(
        self, auth_key: str, changes: list[dict[str, Any]]
    ) -> dict[str, Any]:
        payload = {
            "authKey": auth_key,
            "collection": "libraryItem",
            "changes": changes,
        }
        data = await self._post_json("/api/datastorePut", payload)
        return data if isinstance(data, dict) else {}

    async def _post_json(self, path: str, payload: dict[str, Any]) -> Any:
        response = await self._request(path, payload)
        return self._parse_json(response)

    async def _request(self, path: str, payload: dict[str, Any]) -> httpx.Response:
        url = path if path.startswith("http") else f"{self.api_base_url}{path}"
        headers = {"Accept": "application/json", "Content-Type": "application/json"}
        try:
            async with httpx.AsyncClient(timeout=15.0) as client:
                response = await client.post(url, json=payload, headers=headers)
                response.raise_for_status()
                return response
        except httpx.HTTPStatusError as exc:
            raise StremioError(
                f"Stremio request returned {exc.response.status_code}",
                status_code=exc.response.status_code,
                response_body=_safe_body(exc.response.text),
            ) from exc
        except httpx.RequestError as exc:
            raise StremioError("Stremio request failed") from exc

    def _parse_json(self, response: httpx.Response) -> Any:
        if not response.content:
            return {}
        try:
            payload = response.json()
        except json.JSONDecodeError as exc:
            raise StremioError("Stremio response was not JSON") from exc
        if isinstance(payload, dict):
            error_payload = payload.get("error")
            if isinstance(error_payload, dict):
                message = str(error_payload.get("message") or "Stremio request failed")
                code = error_payload.get("code")
                if isinstance(code, int):
                    code_value: int | None = code
                elif isinstance(code, str) and code.strip().isdigit():
                    code_value = int(code.strip())
                else:
                    code_value = None
                raise StremioError(
                    message,
                    code=code_value,
                    status_code=response.status_code,
                    response_body=_safe_body(response.text),
                )
            if "result" in payload:
                return payload.get("result")
        return payload


def _safe_body(value: str | None, limit: int = 500) -> str | None:
    if not value:
        return None
    trimmed = value.strip()
    if len(trimmed) > limit:
        return f"{trimmed[:limit]}..."
    return trimmed
