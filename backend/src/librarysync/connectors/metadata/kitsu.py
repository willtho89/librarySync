from __future__ import annotations

from typing import Any

import httpx

from librarysync.connectors.metadata.base import MediaCandidate

KITSU_API_BASE = "https://kitsu.io/api/edge"
DEFAULT_SEARCH_LIMIT = 10
MEDIA_TYPE_ANIME = "anime"


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
        poster.get("small")
        or poster.get("medium")
        or poster.get("tiny")
        or poster.get("original")
    )


def _normalize_title(raw: dict[str, Any], language: str | None) -> str:
    attributes = raw.get("attributes") or {}
    titles = attributes.get("titles") or {}
    if language:
        key = language.replace("-", "_").lower()
        value = titles.get(key)
        if value:
            return value
    return (
        attributes.get("canonicalTitle")
        or titles.get("en")
        or titles.get("en_jp")
        or titles.get("ja_jp")
        or attributes.get("slug")
        or "Unknown title"
    )


class KitsuMetadataProvider:
    provider = "kitsu"

    def __init__(self, language: str | None = None) -> None:
        self._language = language

    async def search(self, query: str, scope: str = "all") -> list[MediaCandidate]:
        if scope != MEDIA_TYPE_ANIME:
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
        if scope != MEDIA_TYPE_ANIME:
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

    async def get_details(self, provider_id: str, media_type: str) -> MediaCandidate:
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
        async with httpx.AsyncClient(base_url=KITSU_API_BASE, timeout=15.0) as client:
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
        return MediaCandidate(
            provider=self.provider,
            provider_id=str(kitsu_id) if kitsu_id is not None else "",
            media_type=MEDIA_TYPE_ANIME,
            title=title,
            year=year,
            poster_url=poster_url,
            imdb_id=imdb_id,
            raw=raw,
        )
