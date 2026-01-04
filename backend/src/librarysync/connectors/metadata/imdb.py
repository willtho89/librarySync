from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from urllib.parse import quote

from librarysync.connectors.metadata.base import (
    MediaCandidate,
    MetadataProvider,
    ProviderCapabilities,
    ProviderConfig,
)
from librarysync.core.http_client import get_http_client

IMDB_SUGGESTION_BASE = "https://v2.sg.media-imdb.com/suggestion"
DEFAULT_SEARCH_LIMIT = 10
MEDIA_TYPE_MOVIE = "movie"
MEDIA_TYPE_TV = "tv"


@dataclass(frozen=True)
class ImdbConfig(ProviderConfig):
    pass


def _extract_year(value: Any) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (ValueError, TypeError):
        return None


def _poster_url(image: Any) -> str | None:
    if not isinstance(image, dict):
        return None
    return image.get("imageUrl") or image.get("url")


def _first_alnum(value: str) -> str:
    for char in value:
        if char.isalnum():
            return char.lower()
    return "a"


def _normalize_media_type(raw: dict[str, Any]) -> str:
    type_value = str(
        raw.get("qid") or raw.get("q") or raw.get("type") or raw.get("typeId") or ""
    ).lower()
    if "tv" in type_value or "series" in type_value or "episode" in type_value:
        return MEDIA_TYPE_TV
    if "movie" in type_value or "feature" in type_value or "film" in type_value:
        return MEDIA_TYPE_MOVIE
    if "video game" in type_value:
        return MEDIA_TYPE_MOVIE
    return MEDIA_TYPE_MOVIE


class ImdbMetadataProvider(MetadataProvider[ImdbConfig, None]):
    provider = "imdb"
    config_schema = ImdbConfig
    secrets_schema = None
    capabilities = ProviderCapabilities(
        scopes={MEDIA_TYPE_MOVIE, MEDIA_TYPE_TV},
        supports_external_id=True,
        supports_search=True,
        supports_details=True,
        supports_episodes=False,
    )

    async def search(self, query: str, scope: str = "all") -> list[MediaCandidate]:
        payload = await self._get_suggestions(query)
        items = payload.get("d") or []
        candidates: list[MediaCandidate] = []
        for item in items:
            if not isinstance(item, dict):
                continue
            candidate = self._normalize_candidate(item)
            if not candidate.provider_id:
                continue
            if scope == "movie" and candidate.media_type != MEDIA_TYPE_MOVIE:
                continue
            if scope == "tv" and candidate.media_type != MEDIA_TYPE_TV:
                continue
            if scope == "anime":
                continue
            candidates.append(candidate)
            if len(candidates) >= DEFAULT_SEARCH_LIMIT:
                break
        return candidates

    async def find_by_external_id(
        self, external_id: str, scope: str = "all"
    ) -> list[MediaCandidate]:
        if not external_id.lower().startswith("tt"):
            return []
        payload = await self._get_suggestions(external_id)
        items = payload.get("d") or []
        for item in items:
            if not isinstance(item, dict):
                continue
            if str(item.get("id") or "").lower() != external_id.lower():
                continue
            candidate = self._normalize_candidate(item)
            if scope == "movie" and candidate.media_type != MEDIA_TYPE_MOVIE:
                return []
            if scope == "tv" and candidate.media_type != MEDIA_TYPE_TV:
                return []
            if scope == "anime":
                return []
            return [candidate]
        return []

    async def get_details(self, provider_id: str, media_type: str) -> MediaCandidate:
        payload = await self._get_suggestions(provider_id)
        items = payload.get("d") or []
        for item in items:
            if not isinstance(item, dict):
                continue
            if str(item.get("id") or "").lower() != provider_id.lower():
                continue
            return self._normalize_candidate(item)
        return MediaCandidate(
            provider=self.provider,
            provider_id=provider_id,
            media_type=_normalize_media_type({"qid": media_type}),
            title="Unknown title",
            year=None,
            poster_url=None,
            imdb_id=provider_id,
            raw={"id": provider_id},
        )

    async def validate_credentials(self) -> None:
        await self._get_suggestions("matrix")

    async def _get_suggestions(self, query: str) -> dict[str, Any]:
        normalized = query.strip().lower()
        if not normalized:
            return {"d": []}
        first = _first_alnum(normalized)
        path = f"/{first}/{quote(normalized)}.json"
        async with get_http_client(base_url=IMDB_SUGGESTION_BASE, timeout=15.0) as client:
            response = await client.get(path)
            response.raise_for_status()
            data = response.json()
            if isinstance(data, dict):
                return data
            return {"d": []}

    def _normalize_candidate(self, raw: dict[str, Any]) -> MediaCandidate:
        imdb_id = raw.get("id") or ""
        title = raw.get("l") or raw.get("title") or "Unknown title"
        year = _extract_year(raw.get("y") or raw.get("year"))
        poster_url = _poster_url(raw.get("i"))
        media_type = _normalize_media_type(raw)
        return MediaCandidate(
            provider=self.provider,
            provider_id=str(imdb_id) if imdb_id is not None else "",
            media_type=media_type,
            title=title,
            year=year,
            poster_url=poster_url,
            imdb_id=str(imdb_id) if imdb_id is not None else None,
            raw=raw,
        )
