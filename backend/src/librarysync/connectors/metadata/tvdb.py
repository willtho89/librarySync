from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import httpx

from librarysync.connectors.metadata.base import (
    MediaCandidate,
    MetadataProvider,
    ProviderCapabilities,
    ProviderContext,
)
from librarysync.core.http_client import get_http_client

TVDB_API_BASE = "https://api4.thetvdb.com/v4"
DEFAULT_SEARCH_LIMIT = 10
MEDIA_TYPE_MOVIE = "movie"
MEDIA_TYPE_TV = "tv"


@dataclass(frozen=True)
class TvdbConfig:
    language: str | None = None


@dataclass(frozen=True)
class TvdbSecrets:
    api_key: str
    pin: str | None = None


def _extract_year(value: str | None) -> int | None:
    if not value:
        return None
    try:
        return int(value.split("-", 1)[0])
    except (ValueError, TypeError):
        return None


def _normalize_media_type(value: str | None, fallback: str) -> str:
    if value in {MEDIA_TYPE_MOVIE, MEDIA_TYPE_TV}:
        return value
    return fallback if fallback in {MEDIA_TYPE_MOVIE, MEDIA_TYPE_TV} else MEDIA_TYPE_MOVIE


def _normalize_tvdb_type(value: str | None) -> str | None:
    if not value:
        return None
    lowered = value.lower()
    if lowered in {"movie", "movies"}:
        return MEDIA_TYPE_MOVIE
    if lowered in {"series", "tv", "show"}:
        return MEDIA_TYPE_TV
    return None


def _normalize_tvdb_id(value: str | int | None) -> str | None:
    if value is None:
        return None
    if isinstance(value, int):
        return str(value)
    raw = str(value).strip()
    if not raw:
        return None
    if "-" in raw:
        tail = raw.split("-", 1)[1]
        if tail.isdigit():
            return tail
    return raw


def _poster_url(raw: dict[str, Any]) -> str | None:
    return raw.get("image") or raw.get("image_url") or raw.get("imageUrl")


def _extract_imdb_id(raw: dict[str, Any]) -> str | None:
    for key in ("imdb_id", "imdbId", "imdbID"):
        value = raw.get(key)
        if value:
            return value
    remote_ids = raw.get("remoteIds") or raw.get("remote_ids") or []
    if isinstance(remote_ids, list):
        for entry in remote_ids:
            if not isinstance(entry, dict):
                continue
            source_name = str(entry.get("sourceName") or "").lower()
            entry_type = entry.get("type")
            entry_source = str(entry.get("source") or "").lower()
            if source_name == "imdb":
                imdb_value = entry.get("id") or entry.get("value")
                if imdb_value:
                    return imdb_value
            if entry_source == "imdb":
                imdb_value = entry.get("id") or entry.get("value")
                if imdb_value:
                    return imdb_value
            if str(entry_type).lower() == "imdb" or entry_type == 2:
                imdb_value = entry.get("id") or entry.get("value")
                if imdb_value:
                    return imdb_value
    return None


def _extract_tmdb_id(raw: dict[str, Any]) -> str | None:
    for key in ("tmdb_id", "tmdbId", "tmdbID"):
        value = raw.get(key)
        if value:
            return str(value)
    remote_ids = raw.get("remoteIds") or raw.get("remote_ids") or []
    if isinstance(remote_ids, list):
        for entry in remote_ids:
            if not isinstance(entry, dict):
                continue
            source_name = str(entry.get("sourceName") or "").lower()
            entry_type = entry.get("type")
            entry_source = str(entry.get("source") or "").lower()
            if "themoviedb" in source_name or source_name == "tmdb":
                tmdb_value = entry.get("id") or entry.get("value")
                if tmdb_value:
                    return str(tmdb_value)
            if entry_source == "tmdb":
                tmdb_value = entry.get("id") or entry.get("value")
                if tmdb_value:
                    return str(tmdb_value)
            if str(entry_type).lower() == "tmdb" or entry_type == 10:
                tmdb_value = entry.get("id") or entry.get("value")
                if tmdb_value:
                    return str(tmdb_value)
    return None


class TvdbMetadataProvider(MetadataProvider[TvdbConfig, TvdbSecrets]):
    provider = "tvdb"
    config_schema = TvdbConfig
    secrets_schema = TvdbSecrets
    capabilities = ProviderCapabilities(
        scopes={MEDIA_TYPE_MOVIE, MEDIA_TYPE_TV},
        supports_external_id=True,
        supports_search=True,
        supports_details=True,
        supports_episodes=False,
    )

    def __init__(
        self,
        config: TvdbConfig,
        secrets: TvdbSecrets | None,
        context: ProviderContext,
    ) -> None:
        super().__init__(config, secrets, context)
        if not secrets or not secrets.api_key:
            raise ValueError("TVDB API key is required")
        self._api_key = secrets.api_key
        self._pin = secrets.pin
        self._language = config.language
        self._token: str | None = None

    async def search(self, query: str, scope: str = "all") -> list[MediaCandidate]:
        if scope == "anime":
            return []
        normalized_scope = scope if scope in {MEDIA_TYPE_MOVIE, MEDIA_TYPE_TV} else "all"
        if normalized_scope == MEDIA_TYPE_MOVIE:
            return await self._search(query, "movie")
        if normalized_scope == MEDIA_TYPE_TV:
            return await self._search(query, "series")
        return await self._search(query, None)

    async def find_by_external_id(
        self, external_id: str, scope: str = "all"
    ) -> list[MediaCandidate]:
        if scope == "anime":
            return []
        normalized_scope = scope if scope in {MEDIA_TYPE_MOVIE, MEDIA_TYPE_TV} else "all"
        candidates = await self._search(external_id, None)
        if normalized_scope == "all":
            return candidates
        return [candidate for candidate in candidates if candidate.media_type == normalized_scope]

    async def get_details(self, provider_id: str, media_type: str) -> MediaCandidate:
        normalized = _normalize_media_type(media_type, MEDIA_TYPE_TV)
        normalized_id = _normalize_tvdb_id(provider_id) or provider_id
        if normalized == MEDIA_TYPE_MOVIE:
            data = await self._get_details_data(normalized_id, "movies")
        else:
            data = await self._get_details_data(normalized_id, "series")
        return self._normalize_candidate(data, normalized)

    async def validate_credentials(self) -> None:
        await self._get("/languages", {})

    async def _get_details_data(self, provider_id: str, resource: str) -> dict[str, Any]:
        try:
            payload = await self._get(f"/{resource}/{provider_id}/extended", {})
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code != 404:
                raise
            payload = await self._get(f"/{resource}/{provider_id}", {})
        data = payload.get("data")
        if isinstance(data, dict):
            return data
        return payload

    async def _search(self, query: str, record_type: str | None) -> list[MediaCandidate]:
        params: dict[str, Any] = {"query": query}
        if record_type:
            params["type"] = record_type
        payload = await self._get("/search", params)
        items = payload.get("data") or []
        if not items and self._language:
            payload = await self._get("/search", params, include_language=False)
            items = payload.get("data") or []
        candidates: list[MediaCandidate] = []
        for item in items[:DEFAULT_SEARCH_LIMIT]:
            media_type = _normalize_tvdb_type(item.get("type") or item.get("recordType"))
            if media_type is None:
                continue
            candidates.append(self._normalize_candidate(item, media_type))
        return candidates

    async def _get(
        self, path: str, params: dict[str, Any], include_language: bool = True
    ) -> dict[str, Any]:
        token = await self._ensure_token()
        headers = {"Authorization": f"Bearer {token}"}
        if self._language and include_language:
            headers["Accept-Language"] = self._language
        async with get_http_client(base_url=TVDB_API_BASE, timeout=15.0, headers=headers) as client:
            response = await client.get(path, params=params)
            if response.status_code == 401:
                self._token = None
                token = await self._ensure_token()
                headers["Authorization"] = f"Bearer {token}"
                async with get_http_client(
                    base_url=TVDB_API_BASE, timeout=15.0, headers=headers
                ) as retry_client:
                    response = await retry_client.get(path, params=params)
            response.raise_for_status()
            return response.json()

    async def _ensure_token(self) -> str:
        if self._token:
            return self._token
        payload: dict[str, Any] = {"apikey": self._api_key}
        if self._pin:
            payload["pin"] = self._pin
        async with get_http_client(base_url=TVDB_API_BASE, timeout=15.0) as client:
            response = await client.post("/login", json=payload)
            response.raise_for_status()
            data = response.json().get("data") or {}
            token = data.get("token") or response.json().get("token")
            if not token:
                raise httpx.HTTPError("TVDB login did not return a token")
            self._token = token
            return token

    def _normalize_candidate(self, raw: dict[str, Any], media_type: str) -> MediaCandidate:
        tvdb_id = _normalize_tvdb_id(raw.get("tvdb_id") or raw.get("id"))
        normalized = _normalize_media_type(media_type, MEDIA_TYPE_TV)
        title = raw.get("name") or raw.get("title") or raw.get("slug") or "Unknown title"
        year = _extract_year(
            raw.get("year")
            or raw.get("firstAired")
            or raw.get("first_aired")
            or raw.get("releaseDate")
        )
        poster_url = _poster_url(raw)
        imdb_id = _extract_imdb_id(raw)
        tmdb_id = _extract_tmdb_id(raw)
        normalized_raw = dict(raw)
        if tmdb_id:
            normalized_raw.setdefault("tmdb_id", tmdb_id)
        return MediaCandidate(
            provider=self.provider,
            provider_id=tvdb_id or "",
            media_type=normalized,
            title=title,
            year=year,
            poster_url=poster_url,
            imdb_id=imdb_id,
            raw=normalized_raw,
        )
