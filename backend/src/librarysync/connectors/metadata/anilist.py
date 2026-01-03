"""AniList metadata provider for anime content."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import httpx

from librarysync.connectors.metadata.base import (
    MediaCandidate,
    MetadataProvider,
    ProviderCapabilities,
    ProviderConfig,
    ProviderContext,
)

ANILIST_API_URL = "https://graphql.anilist.co"
MEDIA_TYPE_ANIME = "anime"


@dataclass(frozen=True)
class AniListConfig(ProviderConfig):
    """AniList provider configuration (no API key required for read operations)."""
    pass


def _extract_year(start_date: dict[str, Any] | None) -> int | None:
    """Extract year from AniList date object."""
    if not start_date or not isinstance(start_date, dict):
        return None
    year = start_date.get("year")
    return int(year) if year else None


def _poster_url(cover_image: dict[str, Any] | None) -> str | None:
    """Extract poster URL from AniList cover image object."""
    if not cover_image or not isinstance(cover_image, dict):
        return None
    # Prefer large, then medium, then small
    return (
        cover_image.get("large")
        or cover_image.get("medium")
        or cover_image.get("small")
    )


def _normalize_title(title: dict[str, Any] | None) -> str:
    """Extract title from AniList title object."""
    if not title or not isinstance(title, dict):
        return "Unknown title"
    # Prefer romaji, then english, then native
    return (
        title.get("romaji")
        or title.get("english")
        or title.get("native")
        or "Unknown title"
    )


class AniListMetadataProvider(MetadataProvider[AniListConfig, None]):
    """AniList metadata provider for anime."""

    provider = "anilist"
    config_schema = AniListConfig
    secrets_schema = None
    capabilities = ProviderCapabilities(
        scopes={MEDIA_TYPE_ANIME},
        supports_external_id=False,
        supports_search=True,
        supports_details=True,
        supports_episodes=False,
    )

    def __init__(
        self,
        config: AniListConfig,
        secrets: None,
        context: ProviderContext,
    ) -> None:
        self._config = config
        self._context = context

    async def search(self, query: str, scope: str = "all") -> list[MediaCandidate]:
        """Search for anime by title."""
        if scope != MEDIA_TYPE_ANIME and scope != "all":
            return []

        graphql_query = """
        query ($search: String, $page: Int, $perPage: Int) {
            Page(page: $page, perPage: $perPage) {
                media(search: $search, type: ANIME) {
                    id
                    idMal
                    title {
                        romaji
                        english
                        native
                    }
                    startDate {
                        year
                        month
                        day
                    }
                    coverImage {
                        large
                        medium
                        small
                    }
                    format
                    episodes
                }
            }
        }
        """

        variables = {
            "search": query,
            "page": 1,
            "perPage": 10,
        }

        data = await self._post_graphql(graphql_query, variables)
        page = data.get("Page", {})
        media_list = page.get("media", [])

        return [self._normalize_candidate(item) for item in media_list]

    async def get_details(
        self, provider_item_id: str, scope: str = "all"
    ) -> MediaCandidate:
        """Get anime details by AniList ID."""
        if scope != MEDIA_TYPE_ANIME and scope != "all":
            raise ValueError(f"AniList only supports anime scope, got {scope}")

        graphql_query = """
        query ($id: Int) {
            Media(id: $id, type: ANIME) {
                id
                idMal
                title {
                    romaji
                    english
                    native
                }
                startDate {
                    year
                    month
                    day
                }
                coverImage {
                    large
                    medium
                    small
                }
                format
                episodes
            }
        }
        """

        variables = {"id": int(provider_item_id)}

        data = await self._post_graphql(graphql_query, variables)
        media = data.get("Media")
        if not media:
            raise ValueError(f"AniList ID {provider_item_id} not found")

        return self._normalize_candidate(media)

    async def find_by_external_id(
        self, external_id: str, scope: str = "all"
    ) -> list[MediaCandidate]:
        """AniList does not support IMDb external ID lookup."""
        return []

    def _normalize_candidate(self, raw: dict[str, Any]) -> MediaCandidate:
        """Convert AniList API response to MediaCandidate."""
        anilist_id = str(raw.get("id", ""))
        mal_id = raw.get("idMal")
        title_obj = raw.get("title", {})
        title = _normalize_title(title_obj)
        year = _extract_year(raw.get("startDate"))
        poster = _poster_url(raw.get("coverImage"))

        # Store MAL ID in raw metadata for cross-referencing
        enriched_raw = {
            "anilist_id": anilist_id,
            "mal_id": str(mal_id) if mal_id else None,
            "format": raw.get("format"),
            "episodes": raw.get("episodes"),
            "title": title_obj,
            "type": "anime",
        }

        return MediaCandidate(
            provider=self.provider,
            provider_id=anilist_id,
            media_type=MEDIA_TYPE_ANIME,
            title=title,
            year=year,
            poster_url=poster,
            imdb_id=None,
            tmdb_id=None,
            tvdb_id=None,
            raw=enriched_raw,
        )

    async def _post_graphql(
        self, query: str, variables: dict[str, Any]
    ) -> dict[str, Any]:
        """Execute GraphQL query against AniList API."""
        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.post(
                ANILIST_API_URL,
                json={"query": query, "variables": variables},
                headers={
                    "Content-Type": "application/json",
                    "Accept": "application/json",
                },
            )
            response.raise_for_status()
            result = response.json()

            if "errors" in result:
                errors = result["errors"]
                error_messages = [e.get("message", str(e)) for e in errors]
                raise ValueError(f"AniList API errors: {', '.join(error_messages)}")

            return result.get("data", {})
