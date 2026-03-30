from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from librarysync.connectors.metadata.base import (
    EpisodeMetadataProvider,
    EpisodeSummary,
    MediaCandidate,
    ProviderCapabilities,
    ProviderContext,
    SeasonSummary,
)
from librarysync.core.http_client import get_http_client

TMDB_API_BASE = "https://api.themoviedb.org/3"
TMDB_IMAGE_BASE = "https://image.tmdb.org/t/p/w185"
DEFAULT_SEARCH_LIMIT = 10
MEDIA_TYPE_MOVIE = "movie"
MEDIA_TYPE_TV = "tv"


@dataclass(frozen=True)
class TmdbConfig:
    language: str | None = None
    region: str | None = None
    include_adult: bool = False


@dataclass(frozen=True)
class TmdbSecrets:
    api_key: str


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


def _title_for_type(raw: dict[str, Any], media_type: str) -> str | None:
    if media_type == MEDIA_TYPE_TV:
        return (
            raw.get("name")
            or raw.get("original_name")
            or raw.get("title")
            or raw.get("original_title")
            or None
        )
    return (
        raw.get("title")
        or raw.get("original_title")
        or raw.get("name")
        or raw.get("original_name")
        or None
    )


def _year_for_type(raw: dict[str, Any], media_type: str) -> int | None:
    date_key = "release_date" if media_type == MEDIA_TYPE_MOVIE else "first_air_date"
    return _extract_year(raw.get(date_key) or raw.get("release_date") or raw.get("first_air_date"))


class TmdbMetadataProvider(EpisodeMetadataProvider[TmdbConfig, TmdbSecrets]):
    provider = "tmdb"
    config_schema = TmdbConfig
    secrets_schema = TmdbSecrets
    capabilities = ProviderCapabilities(
        scopes={MEDIA_TYPE_MOVIE, MEDIA_TYPE_TV},
        supports_external_id=True,
        supports_search=True,
        supports_details=True,
        supports_episodes=True,
    )

    def __init__(
        self,
        config: TmdbConfig,
        secrets: TmdbSecrets | None,
        context: ProviderContext,
    ):
        super().__init__(config, secrets, context)
        if not secrets or not secrets.api_key:
            raise ValueError("TMDB API key is required")
        self._api_key = secrets.api_key
        self._language = config.language
        self._region = config.region
        self._include_adult = bool(config.include_adult)

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

    async def get_details(self, provider_id: str, media_type: str) -> MediaCandidate | None:
        normalized = _normalize_media_type(media_type, MEDIA_TYPE_MOVIE)
        path = f"/movie/{provider_id}" if normalized == MEDIA_TYPE_MOVIE else f"/tv/{provider_id}"
        payload = await self._get(
            path,
            {
                "language": self._language,
            },
        )
        candidate = self._normalize_candidate(payload, normalized)
        if not candidate.title:
            return None
        return candidate

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
        if self._language:
            fallback_payload = await self._get(f"/tv/{provider_id}", {})
            fallback_seasons = fallback_payload.get("seasons") or []
            if fallback_seasons:
                seen = {entry.get("season_number") for entry in seasons}
                seasons.extend(
                    entry for entry in fallback_seasons if entry.get("season_number") not in seen
                )
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

    async def list_episodes(self, provider_id: str, season_number: int) -> list[EpisodeSummary]:
        payload = await self._get(
            f"/tv/{provider_id}/season/{season_number}",
            {
                "language": self._language,
            },
        )
        episodes = payload.get("episodes") or []
        if self._language:
            fallback_payload = await self._get(f"/tv/{provider_id}/season/{season_number}", {})
            fallback_episodes = fallback_payload.get("episodes") or []
            if fallback_episodes:
                seen = {entry.get("episode_number") for entry in episodes}
                episodes.extend(
                    entry for entry in fallback_episodes if entry.get("episode_number") not in seen
                )
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
        async with get_http_client(base_url=TMDB_API_BASE, timeout=15.0) as client:
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

        # Extract runtime (convert minutes to seconds)
        runtime_in_seconds = None
        if normalized_type == MEDIA_TYPE_MOVIE and raw.get("runtime"):
            runtime_in_seconds = raw["runtime"] * 60
        elif normalized_type == MEDIA_TYPE_TV and raw.get("episode_run_time"):
            episode_run_times = raw["episode_run_time"]
            if episode_run_times and len(episode_run_times) > 0:
                runtime_in_seconds = episode_run_times[0] * 60

        # Extract genres
        genres = None
        if raw.get("genres"):
            genres = [genre["name"] for genre in raw["genres"] if genre.get("name")]

        # Extract overview
        overview = raw.get("overview")

        return MediaCandidate(
            provider=self.provider,
            provider_id=str(tmdb_id) if tmdb_id is not None else "",
            media_type=normalized_type,
            title=title,
            year=year,
            poster_url=poster_url,
            imdb_id=imdb_id,
            release_date=raw.get("release_date") if normalized_type == MEDIA_TYPE_MOVIE else None,
            first_air_date=raw.get("first_air_date") if normalized_type == MEDIA_TYPE_TV else None,
            last_air_date=raw.get("last_air_date") if normalized_type == MEDIA_TYPE_TV else None,
            runtime_in_seconds=runtime_in_seconds,
            genres=genres,
            overview=overview,
            raw=raw,
        )
