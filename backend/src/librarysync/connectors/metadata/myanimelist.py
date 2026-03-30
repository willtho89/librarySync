from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from librarysync.connectors.metadata.base import (
    MEDIA_SCOPE_ALL,
    MediaCandidate,
    MetadataProvider,
    ProviderCapabilities,
    ProviderConfig,
)
from librarysync.core.http_client import get_http_client

JIKAN_API_BASE = "https://api.jikan.moe/v4"
DEFAULT_SEARCH_LIMIT = 10
MEDIA_TYPE_ANIME = "anime"


@dataclass(frozen=True)
class MyAnimeListConfig(ProviderConfig):
    pass


def _extract_year(value: str | None) -> int | None:
    if not value:
        return None
    try:
        return int(value.split("-", 1)[0])
    except (ValueError, TypeError):
        return None


def _poster_url(images: dict[str, Any] | None) -> str | None:
    if not images:
        return None
    jpg = images.get("jpg") if isinstance(images, dict) else None
    if isinstance(jpg, dict):
        return jpg.get("image_url")
    return None


def _normalize_title(raw: dict[str, Any]) -> str:
    return (
        raw.get("title_english") or raw.get("title") or raw.get("title_japanese") or "Unknown title"
    )


class MyAnimeListMetadataProvider(MetadataProvider[MyAnimeListConfig, None]):
    provider = "myanimelist"
    config_schema = MyAnimeListConfig
    secrets_schema = None
    capabilities = ProviderCapabilities(
        scopes={MEDIA_TYPE_ANIME},
        supports_external_id=False,
        supports_search=True,
        supports_details=True,
        supports_episodes=False,
    )

    async def search(self, query: str, scope: str = "all") -> list[MediaCandidate]:
        if scope not in (MEDIA_TYPE_ANIME, MEDIA_SCOPE_ALL):
            return []
        payload = await self._get(
            "/anime",
            {
                "q": query,
                "limit": DEFAULT_SEARCH_LIMIT,
            },
        )
        items = payload.get("data") or []
        return [self._normalize_candidate(item) for item in items]

    async def find_by_external_id(
        self, external_id: str, scope: str = "all"
    ) -> list[MediaCandidate]:
        if scope not in (MEDIA_TYPE_ANIME, MEDIA_SCOPE_ALL):
            return []
        return []

    async def get_details(self, provider_id: str, media_type: str) -> MediaCandidate | None:
        payload = await self._get(f"/anime/{provider_id}", {})
        data = payload.get("data") or {}
        return self._normalize_candidate(data)

    async def validate_credentials(self) -> None:
        await self._get(
            "/anime",
            {
                "q": "naruto",
                "limit": 1,
            },
        )

    async def _get(self, path: str, params: dict[str, Any]) -> dict[str, Any]:
        async with get_http_client(base_url=JIKAN_API_BASE, timeout=15.0) as client:
            response = await client.get(path, params=params)
            response.raise_for_status()
            data = response.json()
            if isinstance(data, dict):
                return data
            return {}

    def _normalize_candidate(self, raw: dict[str, Any]) -> MediaCandidate:
        mal_id = raw.get("mal_id") or raw.get("id")
        title = _normalize_title(raw)
        year = raw.get("year")
        if year is None:
            year = _extract_year(raw.get("aired", {}).get("from"))
        poster_url = _poster_url(raw.get("images"))

        # Extract runtime (parse duration string)
        runtime_in_seconds = None
        duration_str = raw.get("duration")
        if duration_str:
            # Parse strings like "24 min", "1 hr 30 min"
            import re

            hours_match = re.search(r"(\d+)\s*hr", duration_str, re.IGNORECASE)
            hours = int(hours_match.group(1)) if hours_match else 0
            minutes_match = re.search(r"(\d+)\s*min", duration_str, re.IGNORECASE)
            minutes = int(minutes_match.group(1)) if minutes_match else 0
            runtime_in_seconds = (hours * 60 + minutes) * 60

        # Extract genres
        genres = None
        if raw.get("genres"):
            genres = [genre["name"] for genre in raw["genres"] if genre.get("name")]

        # Extract overview
        overview = raw.get("synopsis")

        return MediaCandidate(
            provider=self.provider,
            provider_id=str(mal_id) if mal_id is not None else "",
            media_type=MEDIA_TYPE_ANIME,
            title=title,
            year=year,
            poster_url=poster_url,
            imdb_id=None,
            runtime_in_seconds=runtime_in_seconds,
            genres=genres,
            overview=overview,
            raw=raw,
        )
