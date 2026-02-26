from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any

import httpx

from librarysync.core.http_client import get_http_client

DEFAULT_PUBLICMETADB_API_BASE_URL = "https://publicmetadb.com"
PUBLICMETADB_REQUIRED_FIELDS = ("api_key",)


class PublicMetaDbError(RuntimeError):
    def __init__(
        self,
        message: str,
        status_code: int | None = None,
        response_body: str | None = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.response_body = response_body


def has_required_publicmetadb_fields(values: Mapping[str, object]) -> bool:
    for field in PUBLICMETADB_REQUIRED_FIELDS:
        value = values.get(field)
        if not isinstance(value, str) or not value:
            return False
    return True


class PublicMetaDbClient:
    def __init__(self, api_base_url: str = DEFAULT_PUBLICMETADB_API_BASE_URL) -> None:
        self.api_base_url = api_base_url.rstrip("/")

    async def validate_credentials(self, api_key: str) -> None:
        await self.list_watched(api_key)

    async def list_watched(
        self,
        api_key: str,
    ) -> tuple[dict[str, Any], int]:
        response = await self._request(
            "GET",
            "/api/external/watched",
            api_key=api_key,
        )
        payload = self._parse_json(response)
        return payload if isinstance(payload, dict) else {}, response.status_code

    async def mark_watched(
        self,
        api_key: str,
        *,
        tmdb_id: int,
        media_type: str,
        season: int | None = None,
        episode: int | None = None,
    ) -> tuple[dict[str, Any], int]:
        payload: dict[str, Any] = {
            "tmdb_id": tmdb_id,
            "media_type": media_type,
        }
        if season is not None:
            payload["season"] = season
        if episode is not None:
            payload["episode"] = episode
        response = await self._request(
            "POST",
            "/api/external/watched",
            api_key=api_key,
            json_body=payload,
        )
        parsed = self._parse_json(response)
        return parsed if isinstance(parsed, dict) else {}, response.status_code

    async def delete_watched(
        self, api_key: str, watched_id: str
    ) -> tuple[dict[str, Any], int]:
        response = await self._request(
            "DELETE",
            f"/api/external/watched/{watched_id}",
            api_key=api_key,
        )
        payload = self._parse_json(response)
        return payload if isinstance(payload, dict) else {}, response.status_code

    async def list_ratings(
        self,
        api_key: str,
        *,
        tmdb_id: int,
        media_type: str,
        label: str | None = None,
    ) -> tuple[dict[str, Any], int]:
        params: dict[str, str | int] = {
            "tmdb_id": tmdb_id,
            "media_type": media_type,
        }
        if label:
            params["label"] = label
        response = await self._request(
            "GET",
            "/api/external/ratings",
            api_key=api_key,
            params=params,
        )
        payload = self._parse_json(response)
        return payload if isinstance(payload, dict) else {}, response.status_code

    async def create_rating(
        self,
        api_key: str,
        *,
        tmdb_id: int,
        media_type: str,
        score: int,
        label: str | None = None,
    ) -> tuple[dict[str, Any], int]:
        payload: dict[str, Any] = {
            "tmdb_id": tmdb_id,
            "media_type": media_type,
            "score": score,
        }
        if label:
            payload["label"] = label
        response = await self._request(
            "POST",
            "/api/external/ratings",
            api_key=api_key,
            json_body=payload,
        )
        parsed = self._parse_json(response)
        return parsed if isinstance(parsed, dict) else {}, response.status_code

    async def delete_rating(
        self, api_key: str, rating_id: str
    ) -> tuple[dict[str, Any], int]:
        response = await self._request(
            "DELETE",
            f"/api/external/ratings/{rating_id}",
            api_key=api_key,
        )
        payload = self._parse_json(response)
        return payload if isinstance(payload, dict) else {}, response.status_code

    async def list_episode_ratings(
        self,
        api_key: str,
        *,
        tmdb_id: int,
        media_type: str,
        season: int,
        episode: int,
        label: str | None = None,
    ) -> tuple[dict[str, Any], int]:
        params: dict[str, str | int] = {
            "tmdb_id": tmdb_id,
            "media_type": media_type,
            "season": season,
            "episode": episode,
        }
        if label:
            params["label"] = label
        response = await self._request(
            "GET",
            "/api/external/episode-ratings",
            api_key=api_key,
            params=params,
        )
        payload = self._parse_json(response)
        return payload if isinstance(payload, dict) else {}, response.status_code

    async def create_episode_rating(
        self,
        api_key: str,
        *,
        tmdb_id: int,
        media_type: str,
        season: int,
        episode: int,
        score: int,
        label: str | None = None,
    ) -> tuple[dict[str, Any], int]:
        payload: dict[str, Any] = {
            "tmdb_id": tmdb_id,
            "media_type": media_type,
            "season": season,
            "episode": episode,
            "score": score,
        }
        if label:
            payload["label"] = label
        response = await self._request(
            "POST",
            "/api/external/episode-ratings",
            api_key=api_key,
            json_body=payload,
        )
        parsed = self._parse_json(response)
        return parsed if isinstance(parsed, dict) else {}, response.status_code

    async def delete_episode_rating(
        self, api_key: str, rating_id: str
    ) -> tuple[dict[str, Any], int]:
        response = await self._request(
            "DELETE",
            f"/api/external/episode-ratings/{rating_id}",
            api_key=api_key,
        )
        payload = self._parse_json(response)
        return payload if isinstance(payload, dict) else {}, response.status_code

    async def _request(
        self,
        method: str,
        path: str,
        *,
        api_key: str,
        params: dict[str, str | int] | None = None,
        json_body: dict[str, Any] | None = None,
    ) -> httpx.Response:
        url = path if path.startswith("http") else f"{self.api_base_url}{path}"
        headers = {
            "Accept": "application/json",
            "Authorization": f"Bearer {api_key}",
        }
        if json_body is not None:
            headers["Content-Type"] = "application/json"
        try:
            async with get_http_client(timeout=20.0, headers=headers) as client:
                response = await client.request(
                    method=method,
                    url=url,
                    params=params,
                    json=json_body,
                )
                response.raise_for_status()
                return response
        except httpx.HTTPStatusError as exc:
            raise PublicMetaDbError(
                f"PublicMetaDB request returned {exc.response.status_code}",
                status_code=exc.response.status_code,
                response_body=_safe_body(exc.response.text),
            ) from exc
        except httpx.RequestError as exc:
            raise PublicMetaDbError("PublicMetaDB request failed") from exc

    def _parse_json(self, response: httpx.Response) -> Any:
        if not response.content:
            return {}
        try:
            payload = response.json()
        except json.JSONDecodeError as exc:
            raise PublicMetaDbError(
                "PublicMetaDB response was not JSON",
                status_code=response.status_code,
                response_body=_safe_body(response.text),
            ) from exc
        if isinstance(payload, dict):
            message = payload.get("error") or payload.get("message") or payload.get("detail")
            if isinstance(message, str) and message.strip():
                status_value = payload.get("status")
                if isinstance(status_value, str) and status_value.lower() in {
                    "error",
                    "failed",
                }:
                    raise PublicMetaDbError(
                        message.strip(),
                        status_code=response.status_code,
                        response_body=_safe_body(response.text),
                    )
        return payload


def _safe_body(value: str | None, limit: int = 500) -> str | None:
    if not value:
        return None
    trimmed = value.strip()
    if len(trimmed) > limit:
        return f"{trimmed[:limit]}..."
    return trimmed
