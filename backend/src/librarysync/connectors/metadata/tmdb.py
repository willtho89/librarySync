from __future__ import annotations

from typing import Any

import httpx

from librarysync.connectors.metadata.base import EpisodeSummary, MediaCandidate, SeasonSummary

TMDB_API_BASE = "https://api.themoviedb.org/3"
TMDB_IMAGE_BASE = "https://image.tmdb.org/t/p/w185"
DEFAULT_SEARCH_LIMIT = 10
MEDIA_TYPE_MOVIE = "movie"
MEDIA_TYPE_TV = "tv"


def _extract_year(date_value: str | None) -> int | None:
    if not date_value:
        return None
    try:
        return int(date_value.split("-", 1)[0])
    except (ValueError, TypeError):
        return None


def _poster_url(path: str | None) -> str | None:
    if not path:
        return None
    return f"{TMDB_IMAGE_BASE}{path}"


def _normalize_media_type(value: str | None, fallback: str) -> str:
    if value in {MEDIA_TYPE_MOVIE, MEDIA_TYPE_TV}:
        return value
    return fallback if fallback in {MEDIA_TYPE_MOVIE, MEDIA_TYPE_TV} else MEDIA_TYPE_MOVIE


def _title_for_type(raw: dict[str, Any], media_type: str) -> str:
    if media_type == MEDIA_TYPE_TV:
        return (
            raw.get("name")
            or raw.get("original_name")
            or raw.get("title")
            or raw.get("original_title")
            or "Unknown title"
        )
    return (
        raw.get("title")
        or raw.get("original_title")
        or raw.get("name")
        or raw.get("original_name")
        or "Unknown title"
    )


def _year_for_type(raw: dict[str, Any], media_type: str) -> int | None:
    date_key = "release_date" if media_type == MEDIA_TYPE_MOVIE else "first_air_date"
    return _extract_year(raw.get(date_key) or raw.get("release_date") or raw.get("first_air_date"))


class TmdbMetadataProvider:
    provider = "tmdb"

    def __init__(
        self,
        api_key: str,
        language: str | None = None,
        region: str | None = None,
        include_adult: bool = False,
    ):
        if not api_key:
            raise ValueError("TMDB API key is required")
        self._api_key = api_key
        self._language = language
        self._region = region
        self._include_adult = bool(include_adult)

    async def search(self, query: str, scope: str = "all") -> list[MediaCandidate]:
        if scope == "anime":
            return []
        normalized_scope = scope if scope in {MEDIA_TYPE_MOVIE, MEDIA_TYPE_TV} else "all"
        if normalized_scope == MEDIA_TYPE_MOVIE:
            return await self._search_movie(query)
        if normalized_scope == MEDIA_TYPE_TV:
            return await self._search_tv(query)
        return await self._search_multi(query)

    async def find_by_external_id(
        self, external_id: str, scope: str = "all"
    ) -> list[MediaCandidate]:
        if scope == "anime":
            return []
        normalized_scope = scope if scope in {MEDIA_TYPE_MOVIE, MEDIA_TYPE_TV} else "all"
        payload = await self._get(
            f"/find/{external_id}",
            {
                "external_source": "imdb_id",
                "language": self._language,
            },
        )
        candidates: list[MediaCandidate] = []
        if normalized_scope in ("all", MEDIA_TYPE_MOVIE):
            results = payload.get("movie_results") or []
            candidates.extend(
                [self._normalize_candidate(item, MEDIA_TYPE_MOVIE) for item in results]
            )
        if normalized_scope in ("all", MEDIA_TYPE_TV):
            results = payload.get("tv_results") or []
            candidates.extend([self._normalize_candidate(item, MEDIA_TYPE_TV) for item in results])
        if candidates or not self._language:
            return candidates
        payload = await self._get(
            f"/find/{external_id}",
            {
                "external_source": "imdb_id",
            },
        )
        if normalized_scope in ("all", MEDIA_TYPE_MOVIE):
            results = payload.get("movie_results") or []
            candidates.extend(
                [self._normalize_candidate(item, MEDIA_TYPE_MOVIE) for item in results]
            )
        if normalized_scope in ("all", MEDIA_TYPE_TV):
            results = payload.get("tv_results") or []
            candidates.extend([self._normalize_candidate(item, MEDIA_TYPE_TV) for item in results])
        return candidates

    async def get_details(self, provider_id: str, media_type: str) -> MediaCandidate:
        normalized = _normalize_media_type(media_type, MEDIA_TYPE_MOVIE)
        path = f"/movie/{provider_id}" if normalized == MEDIA_TYPE_MOVIE else f"/tv/{provider_id}"
        payload = await self._get(
            path,
            {
                "language": self._language,
            },
        )
        return self._normalize_candidate(payload, normalized)

    async def validate_credentials(self) -> None:
        await self._get("/configuration", {})

    async def list_seasons(self, provider_id: str) -> list[SeasonSummary]:
        payload = await self._get(
            f"/tv/{provider_id}",
            {
                "language": self._language,
            },
        )
        seasons = payload.get("seasons") or []
        if not seasons and self._language:
            payload = await self._get(f"/tv/{provider_id}", {})
            seasons = payload.get("seasons") or []
        summaries: list[SeasonSummary] = []
        for entry in seasons:
            season_number = entry.get("season_number")
            if season_number is None:
                continue
            summaries.append(
                SeasonSummary(
                    season_number=int(season_number),
                    name=entry.get("name"),
                    episode_count=entry.get("episode_count"),
                    air_date=entry.get("air_date"),
                    poster_url=_poster_url(entry.get("poster_path")),
                )
            )
        return summaries

    async def list_episodes(
        self, provider_id: str, season_number: int
    ) -> list[EpisodeSummary]:
        payload = await self._get(
            f"/tv/{provider_id}/season/{season_number}",
            {
                "language": self._language,
            },
        )
        episodes = payload.get("episodes") or []
        if not episodes and self._language:
            payload = await self._get(f"/tv/{provider_id}/season/{season_number}", {})
            episodes = payload.get("episodes") or []
        summaries: list[EpisodeSummary] = []
        for entry in episodes:
            episode_number = entry.get("episode_number")
            if episode_number is None:
                continue
            summaries.append(
                EpisodeSummary(
                    episode_number=int(episode_number),
                    title=entry.get("name"),
                    provider_id=str(entry.get("id")) if entry.get("id") is not None else None,
                    air_date=entry.get("air_date"),
                    still_url=_poster_url(entry.get("still_path")),
                )
            )
        return summaries

    async def _get(self, path: str, params: dict[str, Any]) -> dict[str, Any]:
        filtered = {key: value for key, value in params.items() if value is not None}
        filtered["api_key"] = self._api_key
        async with httpx.AsyncClient(base_url=TMDB_API_BASE, timeout=15.0) as client:
            response = await client.get(path, params=filtered)
            response.raise_for_status()
            return response.json()

    async def _search_movie(self, query: str) -> list[MediaCandidate]:
        include_adult = "true" if self._include_adult else "false"
        payload = await self._get(
            "/search/movie",
            {
                "query": query,
                "include_adult": include_adult,
                "page": 1,
                "language": self._language,
                "region": self._region,
            },
        )
        results = payload.get("results") or []
        if not results and self._language:
            payload = await self._get(
                "/search/movie",
                {
                    "query": query,
                    "include_adult": include_adult,
                    "page": 1,
                    "region": self._region,
                },
            )
            results = payload.get("results") or []
        return [
            self._normalize_candidate(item, MEDIA_TYPE_MOVIE)
            for item in results[:DEFAULT_SEARCH_LIMIT]
        ]

    async def _search_tv(self, query: str) -> list[MediaCandidate]:
        include_adult = "true" if self._include_adult else "false"
        payload = await self._get(
            "/search/tv",
            {
                "query": query,
                "include_adult": include_adult,
                "page": 1,
                "language": self._language,
            },
        )
        results = payload.get("results") or []
        if not results and self._language:
            payload = await self._get(
                "/search/tv",
                {
                    "query": query,
                    "include_adult": include_adult,
                    "page": 1,
                },
            )
            results = payload.get("results") or []
        return [
            self._normalize_candidate(item, MEDIA_TYPE_TV)
            for item in results[:DEFAULT_SEARCH_LIMIT]
        ]

    async def _search_multi(self, query: str) -> list[MediaCandidate]:
        include_adult = "true" if self._include_adult else "false"
        payload = await self._get(
            "/search/multi",
            {
                "query": query,
                "include_adult": include_adult,
                "page": 1,
                "language": self._language,
            },
        )
        results = [
            item
            for item in (payload.get("results") or [])
            if item.get("media_type") in {MEDIA_TYPE_MOVIE, MEDIA_TYPE_TV}
        ]
        if not results and self._language:
            payload = await self._get(
                "/search/multi",
                {
                    "query": query,
                    "include_adult": include_adult,
                    "page": 1,
                },
            )
            results = [
                item
                for item in (payload.get("results") or [])
                if item.get("media_type") in {MEDIA_TYPE_MOVIE, MEDIA_TYPE_TV}
            ]
        candidates: list[MediaCandidate] = []
        for item in results[:DEFAULT_SEARCH_LIMIT]:
            media_type = _normalize_media_type(item.get("media_type"), MEDIA_TYPE_MOVIE)
            candidates.append(self._normalize_candidate(item, media_type))
        return candidates

    def _normalize_candidate(self, raw: dict[str, Any], media_type: str) -> MediaCandidate:
        tmdb_id = raw.get("id")
        normalized_type = _normalize_media_type(raw.get("media_type"), media_type)
        title = _title_for_type(raw, normalized_type)
        year = _year_for_type(raw, normalized_type)
        poster_url = _poster_url(raw.get("poster_path"))
        imdb_id = raw.get("imdb_id")
        return MediaCandidate(
            provider=self.provider,
            provider_id=str(tmdb_id) if tmdb_id is not None else "",
            media_type=normalized_type,
            title=title,
            year=year,
            poster_url=poster_url,
            imdb_id=imdb_id,
            raw=raw,
        )
