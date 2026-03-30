from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from librarysync.connectors.metadata.base import (
    MEDIA_SCOPE_ALL,
    MediaCandidate,
    MetadataProvider,
    ProviderCapabilities,
    ProviderConfig,
    ProviderContext,
)
from librarysync.core.http_client import get_http_client

KITSU_API_BASE = "https://kitsu.io/api/edge"
DEFAULT_SEARCH_LIMIT = 10
MEDIA_TYPE_ANIME = "anime"


@dataclass(frozen=True)
class KitsuConfig(ProviderConfig):
    language: str | None = None


def _extract_year(date_value: str | None) -> int | None:
    if not date_value:
        return None
    try:
        return int(date_value.split("-", 1)[0])
    except (ValueError, TypeError):
        return None


def _poster_url(poster: dict[str, Any] | None) -> str | None:
    if not poster:
        return None
    return (
        poster.get("small") or poster.get("medium") or poster.get("tiny") or poster.get("original")
    )


def _normalize_title(raw: dict[str, Any], language: str | None) -> str:
    attributes = raw.get("attributes") or {}
    titles = attributes.get("titles") or {}
    if language:
        key = language.replace("-", "_").lower()
        value = titles.get(key)
        if value:
            return value
    else:
        for key in ("en", "en_us", "en_gb"):
            value = titles.get(key)
            if value:
                return value
    return (
        titles.get("en")
        or attributes.get("canonicalTitle")
        or titles.get("en_jp")
        or titles.get("ja_jp")
        or attributes.get("slug")
        or "Unknown title"
    )


class KitsuMetadataProvider(MetadataProvider[KitsuConfig, None]):
    provider = "kitsu"
    config_schema = KitsuConfig
    secrets_schema = None
    capabilities = ProviderCapabilities(
        scopes={MEDIA_TYPE_ANIME},
        supports_external_id=True,
        supports_search=True,
        supports_details=True,
        supports_episodes=False,
    )

    def __init__(
        self,
        config: KitsuConfig,
        secrets: None,
        context: ProviderContext,
    ) -> None:
        super().__init__(config, secrets, context)
        self._language = config.language

    async def search(self, query: str, scope: str = "all") -> list[MediaCandidate]:
        if scope not in (MEDIA_TYPE_ANIME, MEDIA_SCOPE_ALL):
            return []
        payload = await self._get(
            "/anime",
            {
                "filter[text]": query,
                "page[limit]": DEFAULT_SEARCH_LIMIT,
            },
        )
        items = payload.get("data") or []
        return [self._normalize_candidate(item) for item in items]

    async def find_by_external_id(
        self, external_id: str, scope: str = "all"
    ) -> list[MediaCandidate]:
        if scope not in (MEDIA_TYPE_ANIME, MEDIA_SCOPE_ALL):
            return []
        if not external_id.lower().startswith("tt"):
            return []
        payload = await self._get(
            "/anime",
            {
                "filter[imdb_id]": external_id,
                "page[limit]": DEFAULT_SEARCH_LIMIT,
            },
        )
        items = payload.get("data") or []
        return [self._normalize_candidate(item) for item in items]

    async def get_details(self, provider_id: str, media_type: str) -> MediaCandidate | None:
        payload = await self._get(f"/anime/{provider_id}", {})
        data = payload.get("data") or {}
        return self._normalize_candidate(data)

    async def validate_credentials(self) -> None:
        await self._get(
            "/anime",
            {
                "page[limit]": 1,
            },
        )

    async def _get(self, path: str, params: dict[str, Any]) -> dict[str, Any]:
        async with get_http_client(base_url=KITSU_API_BASE, timeout=15.0) as client:
            response = await client.get(path, params=params)
            response.raise_for_status()
            return response.json()

    def _normalize_candidate(self, raw: dict[str, Any]) -> MediaCandidate:
        attributes = raw.get("attributes") or {}
        kitsu_id = raw.get("id") or ""
        title = _normalize_title(raw, self._language)
        year = _extract_year(
            attributes.get("startDate")
            or attributes.get("start_date")
            or attributes.get("createdAt")
        )
        poster_url = _poster_url(attributes.get("posterImage") or attributes.get("poster_image"))
        imdb_id = attributes.get("imdbId") or attributes.get("imdb_id")

        # Extract runtime (convert minutes to seconds)
        runtime_in_seconds = None
        if attributes.get("episodeLength"):
            runtime_in_seconds = int(attributes["episodeLength"]) * 60

        # Extract genres
        genres = None
        if attributes.get("genres"):
            genres_data = attributes["genres"]
            if isinstance(genres_data, list):
                genres = [genre.get("name") for genre in genres_data if genre.get("name")]

        # Extract overview
        overview = attributes.get("synopsis")

        return MediaCandidate(
            provider=self.provider,
            provider_id=str(kitsu_id) if kitsu_id is not None else "",
            media_type=MEDIA_TYPE_ANIME,
            title=title,
            year=year,
            poster_url=poster_url,
            imdb_id=imdb_id,
            runtime_in_seconds=runtime_in_seconds,
            genres=genres,
            overview=overview,
            raw=raw,
        )
