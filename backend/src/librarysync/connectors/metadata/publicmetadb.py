from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import httpx

from librarysync.connectors.metadata.base import (
    MEDIA_SCOPE_ALL,
    MediaCandidate,
    MetadataProvider,
    ProviderCapabilities,
    ProviderContext,
)
from librarysync.core.http_client import get_http_client

PUBLICMETADB_API_BASE = "https://publicmetadb.com"
DEFAULT_LOOKUP_LIMIT = 10
MEDIA_TYPE_MOVIE = "movie"
MEDIA_TYPE_TV = "tv"

ID_TYPE_ALIASES = {
    "imdb": "imdb",
    "imdb_id": "imdb",
    "tvdb": "tvdb",
    "tvdb_id": "tvdb",
    "mal": "mal",
    "myanimelist": "mal",
    "myanimelist_id": "mal",
    "anilist": "anilist",
    "anilist_id": "anilist",
    "kitsu": "kitsu",
    "kitsu_id": "kitsu",
    "trakt": "trakt",
    "trakt_id": "trakt",
    "anidb": "anidb",
    "anidb_id": "anidb",
}

MAPPING_ID_KEYS = {
    "imdb": "imdb_id",
    "tvdb": "tvdb_id",
    "tvmaze": "tvmaze_id",
    "kitsu": "kitsu_id",
    "myanimelist": "myanimelist_id",
    "mal": "myanimelist_id",
    "anilist": "anilist_id",
    "tmdb": "tmdb_id",
}


@dataclass(frozen=True)
class PublicMetaDbConfig:
    pass


@dataclass(frozen=True)
class PublicMetaDbSecrets:
    api_key: str


def _extract_year(value: str | int | None) -> int | None:
    if value is None:
        return None
    if isinstance(value, int):
        return value
    raw = str(value).strip()
    if len(raw) >= 4 and raw[:4].isdigit():
        return int(raw[:4])
    return None


def _normalize_media_type(value: str | None, fallback: str) -> str:
    if value in {MEDIA_TYPE_MOVIE, MEDIA_TYPE_TV}:
        return value
    return fallback if fallback in {MEDIA_TYPE_MOVIE, MEDIA_TYPE_TV} else MEDIA_TYPE_MOVIE


def _normalize_id_type(value: str | None) -> str | None:
    if not value:
        return None
    return ID_TYPE_ALIASES.get(value.strip().lower())


def _parse_external_id(value: str) -> tuple[str, str] | None:
    raw = value.strip()
    if not raw:
        return None
    if ":" in raw:
        prefix, _, remainder = raw.partition(":")
        id_type = _normalize_id_type(prefix)
        id_value = remainder.strip()
        if id_type and id_value:
            return id_type, id_value
    lowered = raw.lower()
    if lowered.startswith("tt") and lowered[2:].isdigit():
        return "imdb", lowered
    return None


def _first_mapping_value(value: Any) -> str | None:
    if isinstance(value, list):
        for item in value:
            if isinstance(item, dict):
                candidate = (
                    item.get("value")
                    or item.get("id_value")
                    or item.get("id")
                    or item.get("tmdb_id")
                )
                if candidate:
                    return str(candidate)
            elif item:
                return str(item)
        return None
    if isinstance(value, dict):
        candidate = (
            value.get("value")
            or value.get("id_value")
            or value.get("id")
            or value.get("tmdb_id")
        )
        if candidate:
            return str(candidate)
        return None
    if value:
        return str(value)
    return None


def _extract_mapping_ids(value: Any) -> dict[str, str]:
    if not isinstance(value, dict):
        return {}
    ids: dict[str, str] = {}
    for mapping_key, id_key in MAPPING_ID_KEYS.items():
        raw = _first_mapping_value(value.get(mapping_key))
        if raw:
            ids[id_key] = raw
    return ids


def _extract_tmdb_id(value: dict[str, Any]) -> str | None:
    for key in ("tmdb_id", "tmdbId", "tmdbID", "id"):
        raw = value.get(key)
        if raw is not None and str(raw).strip():
            return str(raw).strip()
    return None


class PublicMetaDbMetadataProvider(MetadataProvider[PublicMetaDbConfig, PublicMetaDbSecrets]):
    provider = "publicmetadb"
    config_schema = PublicMetaDbConfig
    secrets_schema = PublicMetaDbSecrets
    capabilities = ProviderCapabilities(
        scopes={MEDIA_TYPE_MOVIE, MEDIA_TYPE_TV},
        supports_external_id=True,
        supports_search=False,
        supports_details=True,
        supports_episodes=False,
    )

    def __init__(
        self,
        config: PublicMetaDbConfig,
        secrets: PublicMetaDbSecrets | None,
        context: ProviderContext,
    ) -> None:
        super().__init__(config, secrets, context)
        if not secrets or not secrets.api_key:
            raise ValueError("PublicMetaDB API key is required")
        self._api_key = secrets.api_key

    async def search(self, query: str, scope: str = "all") -> list[MediaCandidate]:
        return []

    async def find_by_external_id(
        self, external_id: str, scope: str = MEDIA_SCOPE_ALL
    ) -> list[MediaCandidate]:
        if scope not in {MEDIA_SCOPE_ALL, MEDIA_TYPE_MOVIE, MEDIA_TYPE_TV}:
            return []
        parsed = _parse_external_id(external_id)
        if not parsed:
            return []
        id_type, id_value = parsed
        params: dict[str, Any] = {
            "id_type": id_type,
            "id_value": id_value,
        }
        if scope in {MEDIA_TYPE_MOVIE, MEDIA_TYPE_TV}:
            params["media_type"] = scope
        payload = await self._get("/api/external/mappings/lookup", params)
        results = payload.get("results") if isinstance(payload, dict) else []
        if not isinstance(results, list):
            return []

        candidates: list[MediaCandidate] = []
        seen: set[tuple[str, str]] = set()
        for result in results[:DEFAULT_LOOKUP_LIMIT]:
            if not isinstance(result, dict):
                continue
            tmdb_id = _extract_tmdb_id(result)
            if not tmdb_id:
                continue
            media_type = _normalize_media_type(result.get("media_type"), MEDIA_TYPE_MOVIE)
            dedupe_key = (tmdb_id, media_type)
            if dedupe_key in seen:
                continue
            seen.add(dedupe_key)

            mapping_payload: dict[str, Any] = {}
            try:
                mapping_payload = await self._get_mappings(tmdb_id, media_type)
            except httpx.HTTPStatusError as exc:
                if exc.response.status_code != 404:
                    raise
            candidates.append(
                self._build_candidate(
                    tmdb_id,
                    media_type,
                    mapping_payload,
                    external_lookup=(id_type, id_value),
                )
            )
        return candidates

    async def get_details(self, provider_id: str, media_type: str) -> MediaCandidate:
        normalized_type = _normalize_media_type(media_type, MEDIA_TYPE_MOVIE)
        payload: dict[str, Any] = {}
        try:
            payload = await self._get_mappings(provider_id, normalized_type)
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code != 404:
                raise
        return self._build_candidate(provider_id, normalized_type, payload)

    async def validate_credentials(self) -> None:
        await self._get(
            "/api/external/mappings/lookup",
            {"id_type": "imdb", "id_value": "tt0944947", "media_type": MEDIA_TYPE_TV},
        )

    async def _get_mappings(self, tmdb_id: str, media_type: str) -> dict[str, Any]:
        return await self._get(
            "/api/external/mappings",
            {"tmdb_id": tmdb_id, "media_type": media_type},
        )

    async def _get(self, path: str, params: dict[str, Any]) -> dict[str, Any]:
        headers = {
            "Authorization": f"Bearer {self._api_key}",
            "Accept": "application/json",
        }
        async with get_http_client(
            base_url=PUBLICMETADB_API_BASE,
            timeout=15.0,
            headers=headers,
        ) as client:
            response = await client.get(path, params=params)
            response.raise_for_status()
            data = response.json()
            return data if isinstance(data, dict) else {}

    def _build_candidate(
        self,
        tmdb_id: str,
        media_type: str,
        payload: dict[str, Any],
        external_lookup: tuple[str, str] | None = None,
    ) -> MediaCandidate:
        ids = _extract_mapping_ids(payload.get("mappings"))
        ids.setdefault("tmdb_id", str(tmdb_id))
        imdb_id = ids.get("imdb_id")
        if external_lookup and external_lookup[0] == "imdb" and not imdb_id:
            imdb_id = external_lookup[1].lower()
            ids["imdb_id"] = imdb_id

        title = (
            payload.get("title")
            or payload.get("name")
            or payload.get("original_title")
            or payload.get("original_name")
            or f"TMDB {tmdb_id}"
        )
        poster_url = payload.get("poster_url") or payload.get("poster")
        release_date = payload.get("release_date")
        first_air_date = payload.get("first_air_date")
        year = _extract_year(payload.get("year") or release_date or first_air_date)

        raw = {
            "tmdb_id": str(tmdb_id),
            "media_type": media_type,
            "ids": ids,
            "mappings": payload.get("mappings"),
        }
        if release_date:
            raw["release_date"] = release_date
        if first_air_date:
            raw["first_air_date"] = first_air_date

        return MediaCandidate(
            provider=self.provider,
            provider_id=str(tmdb_id),
            media_type=media_type,
            title=title,
            year=year,
            poster_url=str(poster_url) if poster_url else None,
            imdb_id=imdb_id,
            release_date=release_date if media_type == MEDIA_TYPE_MOVIE else None,
            first_air_date=first_air_date if media_type == MEDIA_TYPE_TV else None,
            raw=raw,
        )
