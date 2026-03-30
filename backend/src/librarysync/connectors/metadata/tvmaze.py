from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

import httpx

from librarysync.connectors.metadata.base import (
    MediaCandidate,
    MetadataProvider,
    ProviderCapabilities,
    ProviderConfig,
)
from librarysync.core.http_client import get_http_client

TVMAZE_API_BASE = "https://api.tvmaze.com"
DEFAULT_SEARCH_LIMIT = 10
MEDIA_TYPE_TV = "tv"


@dataclass(frozen=True)
class TvmazeConfig(ProviderConfig):
    pass


def _extract_year(value: str | None) -> int | None:
    if not value:
        return None
    try:
        return int(value.split("-", 1)[0])
    except (ValueError, TypeError):
        return None


def _poster_url(image: dict[str, Any] | None) -> str | None:
    if not image:
        return None
    return image.get("medium") or image.get("original")


class TvmazeMetadataProvider(MetadataProvider[TvmazeConfig, None]):
    provider = "tvmaze"
    config_schema = TvmazeConfig
    secrets_schema = None
    capabilities = ProviderCapabilities(
        scopes={MEDIA_TYPE_TV},
        supports_external_id=True,
        supports_search=True,
        supports_details=True,
        supports_episodes=False,
    )

    async def search(self, query: str, scope: str = "all") -> list[MediaCandidate]:
        if scope not in {"all", MEDIA_TYPE_TV}:
            return []
        payload = await self._get("/search/shows", {"q": query})
        items = payload or []
        candidates: list[MediaCandidate] = []
        for item in items[:DEFAULT_SEARCH_LIMIT]:
            show = item.get("show") if isinstance(item, dict) else None
            if not isinstance(show, dict):
                continue
            candidates.append(self._normalize_candidate(show))
        return candidates

    async def find_by_external_id(
        self, external_id: str, scope: str = "all"
    ) -> list[MediaCandidate]:
        if scope not in {"all", MEDIA_TYPE_TV}:
            return []
        if not external_id.lower().startswith("tt"):
            return []
        try:
            payload = await self._get("/lookup/shows", {"imdb": external_id})
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code == 404:
                return []
            raise
        if not isinstance(payload, dict):
            return []
        candidate = self._normalize_candidate(payload)
        return [candidate] if candidate.provider_id else []

    async def get_details(self, provider_id: str, media_type: str) -> MediaCandidate | None:
        payload = await self._get(f"/shows/{provider_id}", {})
        if not isinstance(payload, dict):
            raise httpx.HTTPError("TVMaze details response missing")
        return self._normalize_candidate(payload)

    async def validate_credentials(self) -> None:
        await self._get("/shows/1", {})

    async def _get(self, path: str, params: dict[str, Any]) -> Any:
        async with get_http_client(base_url=TVMAZE_API_BASE, timeout=15.0) as client:
            response = await client.get(path, params=params)
            response.raise_for_status()
            return response.json()

    def _normalize_candidate(self, raw: dict[str, Any]) -> MediaCandidate:
        tvmaze_id = raw.get("id")
        title = raw.get("name") or raw.get("title") or "Unknown title"
        year = _extract_year(raw.get("premiered"))
        poster_url = _poster_url(raw.get("image"))
        externals = raw.get("externals") or {}
        imdb_id = externals.get("imdb") if isinstance(externals, dict) else None

        # Extract runtime (convert minutes to seconds)
        runtime_in_seconds = None
        if raw.get("runtime"):
            runtime_in_seconds = int(raw["runtime"]) * 60

        # Extract genres
        genres = None
        if raw.get("genres"):
            genres = raw["genres"]

        # Extract overview (summary field in TVMaze)
        overview = raw.get("summary")
        if overview:
            # Remove HTML tags from summary
            overview = re.sub(r"<[^>]+>", "", overview)

        return MediaCandidate(
            provider=self.provider,
            provider_id=str(tvmaze_id) if tvmaze_id is not None else "",
            media_type=MEDIA_TYPE_TV,
            title=title,
            year=year,
            poster_url=poster_url,
            imdb_id=imdb_id,
            runtime_in_seconds=runtime_in_seconds,
            genres=genres,
            overview=overview,
            raw=raw,
        )
