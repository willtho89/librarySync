from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import httpx

from librarysync.core.http_client import get_http_client

DEFAULT_AIOSTREAMS_API_BASE_URL = "https://aiostreams.example"
AIOSTREAMS_REQUIRED_FIELDS = ("auth",)


class AIOStreamsError(RuntimeError):
    def __init__(
        self,
        message: str,
        status_code: int | None = None,
        response_body: str | None = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.response_body = response_body


def has_required_aiostreams_fields(values: Mapping[str, object]) -> bool:
    for field in AIOSTREAMS_REQUIRED_FIELDS:
        value = values.get(field)
        if not isinstance(value, str) or not value.strip():
            return False
    return True


class AIOStreamsClient:
    def __init__(self, api_base_url: str) -> None:
        self.api_base_url = api_base_url.rstrip("/")

    async def get_stats(self, auth: str) -> dict[str, Any]:
        url = f"{self.api_base_url}/api/v1/proxy/stats"
        params = {"auth": auth}
        headers = {"Accept": "application/json"}
        try:
            async with get_http_client(timeout=20.0, headers=headers) as client:
                response = await client.get(url, params=params)
                response.raise_for_status()
                payload = response.json()
        except httpx.HTTPStatusError as exc:
            raise AIOStreamsError(
                f"AIOStreams stats returned {exc.response.status_code}",
                status_code=exc.response.status_code,
                response_body=_safe_body(exc.response.text),
            ) from exc
        except (httpx.RequestError, ValueError) as exc:
            raise AIOStreamsError("AIOStreams stats request failed") from exc
        if not isinstance(payload, dict):
            raise AIOStreamsError("AIOStreams stats response was not JSON")
        return payload


def _safe_body(value: str | None, limit: int = 500) -> str | None:
    if not value:
        return None
    trimmed = value.strip()
    if len(trimmed) > limit:
        return f"{trimmed[:limit]}..."
    return trimmed
