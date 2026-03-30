"""AniList metadata provider for anime content."""

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
    # Prefer extraLarge, then large, then medium
    return cover_image.get("extraLarge") or cover_image.get("large") or cover_image.get("medium")


def _normalize_title(title: dict[str, Any] | None) -> str | None:
    """Extract title from AniList title object."""
    if not title or not isinstance(title, dict):
        return None
    # Prefer English when available, then romaji, then native
    return title.get("english") or title.get("romaji") or title.get("native") or None


class AniListMetadataProvider(MetadataProvider[AniListConfig, None]):
    """AniList metadata provider for anime."""

    provider = "anilist"
    config_schema = AniListConfig
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
                        extraLarge
                        large
                        medium
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

    async def get_details(self, provider_item_id: str, media_type: str) -> MediaCandidate | None:
        """Get anime details by AniList ID."""
        # AniList only supports anime, ignore media_type parameter
        if media_type not in (MEDIA_TYPE_ANIME, "all"):
            raise ValueError(f"AniList only supports anime scope, got {media_type!r}")

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
                    extraLarge
                    large
                    medium
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

        candidate = self._normalize_candidate(media)
        if not candidate.title:
            return None
        return candidate

    async def validate_credentials(self) -> None:
        """AniList metadata operations don't require authentication."""
        # Public API - no credentials to validate
        # Perform a simple test query to verify API is accessible
        try:
            await self.search("test", "anime")
        except Exception as exc:
            raise ValueError(f"AniList API is not accessible: {exc}") from exc

    async def find_by_external_id(
        self, external_id: str, scope: str = "all"
    ) -> list[MediaCandidate]:
        if scope not in (MEDIA_TYPE_ANIME, MEDIA_SCOPE_ALL):
            return []
        mal_id = _parse_mal_id(external_id)
        if mal_id is None:
            return []
        graphql_query = """
        query ($idMal: Int) {
            Media(idMal: $idMal, type: ANIME) {
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
                    extraLarge
                    large
                    medium
                }
                format
                episodes
            }
        }
        """
        data = await self._post_graphql(graphql_query, {"idMal": mal_id})
        media = data.get("Media")
        if not media:
            return []
        return [self._normalize_candidate(media)]

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

        # Extract runtime (convert minutes to seconds)
        runtime_in_seconds = None
        if raw.get("duration"):
            runtime_in_seconds = int(raw["duration"]) * 60

        # Extract genres
        genres = None
        if raw.get("genres"):
            genres = raw["genres"]

        # Extract overview
        overview = raw.get("description")

        return MediaCandidate(
            provider=self.provider,
            provider_id=anilist_id,
            media_type=MEDIA_TYPE_ANIME,
            title=title,
            year=year,
            poster_url=poster,
            imdb_id=None,
            runtime_in_seconds=runtime_in_seconds,
            genres=genres,
            overview=overview,
            raw=enriched_raw,
        )

    async def _post_graphql(self, query: str, variables: dict[str, Any]) -> dict[str, Any]:
        """Execute GraphQL query against AniList API."""
        async with get_http_client(
            timeout=30.0,
            headers={
                "Content-Type": "application/json",
                "Accept": "application/json",
            },
        ) as client:
            response = await client.post(
                ANILIST_API_URL,
                json={"query": query, "variables": variables},
            )
            if response.status_code >= 400:
                body = response.text.strip()
                if len(body) > 300:
                    body = f"{body[:300]}..."
                raise ValueError(
                    f"AniList API error: {response.status_code}"
                    + (f" (body={body})" if body else "")
                )
            result = response.json()

            if "errors" in result:
                errors = result["errors"]
                error_messages = [e.get("message", str(e)) for e in errors]
                raise ValueError(f"AniList API errors: {', '.join(error_messages)}")

            return result.get("data", {})


def _parse_mal_id(external_id: str) -> int | None:
    if not external_id:
        return None
    cleaned = external_id.strip().lower()
    if cleaned.startswith("mal:"):
        cleaned = cleaned[4:]
    if not cleaned.isdigit():
        return None
    try:
        return int(cleaned)
    except ValueError:
        return None
