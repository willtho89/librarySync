"""AniList service connector for anime tracking."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.parse import urlencode

import httpx

DEFAULT_ANILIST_API_URL = "https://graphql.anilist.co"
ANILIST_OAUTH_AUTHORIZE_URL = "https://anilist.co/api/v2/oauth/authorize"
ANILIST_OAUTH_TOKEN_URL = "https://anilist.co/api/v2/oauth/token"
ANILIST_REQUIRED_FIELDS = ("access_token",)


def convert_rating_to_anilist_scale(rating: float | None) -> float | None:
    """Convert librarySync rating (0.5-5.0) to AniList scale (0-10).
    
    Args:
        rating: Rating in 0.5-5.0 scale (or None)
        
    Returns:
        Rating in 0-10 scale (or None if input is None)
    """
    if rating is None:
        return None
    return min(10.0, max(0.0, float(rating) * 2.0))


@dataclass(frozen=True)
class AniListToken:
    access_token: str
    refresh_token: str | None = None
    expires_at: datetime | None = None
    token_type: str | None = None


class AniListError(RuntimeError):
    def __init__(
        self,
        message: str,
        status_code: int | None = None,
        response_body: str | None = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.response_body = response_body


def has_required_anilist_fields(values: Mapping[str, object]) -> bool:
    """Check if token response contains required fields."""
    for field in ANILIST_REQUIRED_FIELDS:
        value = values.get(field)
        if not isinstance(value, str) or not value:
            return False
    return True


def parse_expires_at(value: object) -> datetime | None:
    """Parse expiry timestamp from various formats."""
    if value is None:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(float(value), tz=timezone.utc)
    if isinstance(value, str):
        cleaned = value.strip()
        if not cleaned:
            return None
        if cleaned.isdigit():
            try:
                return datetime.fromtimestamp(float(cleaned), tz=timezone.utc)
            except ValueError:
                return None
        try:
            if cleaned.endswith("Z"):
                cleaned = f"{cleaned[:-1]}+00:00"
            parsed = datetime.fromisoformat(cleaned)
            return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
        except ValueError:
            return None
    return None


def is_token_expired(expires_at: datetime | None, skew_seconds: int = 60) -> bool:
    """Check if token has expired or will expire soon."""
    if expires_at is None:
        return False
    now = datetime.now(timezone.utc)
    return expires_at <= (now + timedelta(seconds=skew_seconds))


def normalize_token_payload(payload: Mapping[str, Any]) -> AniListToken:
    """Parse and normalize OAuth token response from AniList."""
    error = payload.get("error")
    if isinstance(error, str) and error.strip():
        description = payload.get("error_description")
        detail = f": {description}" if isinstance(description, str) and description else ""
        raise AniListError(f"AniList token response error {error}{detail}")

    access_token = str(payload.get("access_token") or "").strip()
    if not access_token:
        available = ", ".join(sorted(str(key) for key in payload.keys()))
        raise AniListError(
            "AniList token response missing access_token"
            + (f" (keys={available})" if available else "")
        )

    refresh_token = str(payload.get("refresh_token") or "").strip() or None
    expires_in = payload.get("expires_in")
    expires_at: datetime | None = None

    if isinstance(expires_in, (int, float)) and expires_in > 0:
        expires_at = datetime.now(timezone.utc) + timedelta(seconds=int(expires_in))

    token_type = str(payload.get("token_type") or "Bearer").strip()

    return AniListToken(
        access_token=access_token,
        refresh_token=refresh_token,
        expires_at=expires_at,
        token_type=token_type,
    )


def token_to_secret_payload(token: AniListToken) -> dict[str, str]:
    """Convert AniListToken to secret storage format."""
    payload = {"access_token": token.access_token}
    if token.refresh_token:
        payload["refresh_token"] = token.refresh_token
    if token.expires_at:
        payload["expires_at"] = token.expires_at.isoformat()
    if token.token_type:
        payload["token_type"] = token.token_type
    return payload


class AniListClient:
    """Client for AniList GraphQL API."""

    def __init__(
        self,
        access_token: str,
        api_url: str = DEFAULT_ANILIST_API_URL,
        timeout: float = 30.0,
    ) -> None:
        self._access_token = access_token
        self._api_url = api_url
        self._timeout = timeout

    async def get_viewer(self) -> dict[str, Any]:
        """Get authenticated user information."""
        query = """
        query {
            Viewer {
                id
                name
                avatar {
                    medium
                }
            }
        }
        """
        result = await self._post_graphql(query, {})
        viewer = result.get("Viewer")
        if not viewer:
            raise AniListError("Failed to get viewer info")
        return viewer

    async def add_media_list_entry(
        self,
        media_id: int,
        status: str = "COMPLETED",
        score: float | None = None,
        progress: int | None = None,
        started_at: datetime | None = None,
        completed_at: datetime | None = None,
    ) -> dict[str, Any]:
        """Add or update a media list entry (anime in user's list)."""
        mutation = """
        mutation (
            $mediaId: Int
            $status: MediaListStatus
            $score: Float
            $progress: Int
            $startedAt: FuzzyDateInput
            $completedAt: FuzzyDateInput
        ) {
            SaveMediaListEntry(
                mediaId: $mediaId
                status: $status
                score: $score
                progress: $progress
                startedAt: $startedAt
                completedAt: $completedAt
            ) {
                id
                mediaId
                status
                score
                progress
            }
        }
        """

        variables: dict[str, Any] = {
            "mediaId": media_id,
            "status": status,
        }

        if score is not None:
            # Score should already be in 0-10 scale
            variables["score"] = min(10.0, max(0.0, float(score)))

        if progress is not None:
            variables["progress"] = progress

        if started_at:
            variables["startedAt"] = self._date_to_fuzzy(started_at)

        if completed_at:
            variables["completedAt"] = self._date_to_fuzzy(completed_at)

        result = await self._post_graphql(mutation, variables)
        return result.get("SaveMediaListEntry", {})

    async def delete_media_list_entry(self, entry_id: int) -> bool:
        """Delete a media list entry."""
        mutation = """
        mutation ($id: Int) {
            DeleteMediaListEntry(id: $id) {
                deleted
            }
        }
        """

        variables = {"id": entry_id}
        result = await self._post_graphql(mutation, variables)
        return result.get("DeleteMediaListEntry", {}).get("deleted", False)

    async def get_media_list_entry(
        self, media_id: int, user_id: int
    ) -> dict[str, Any] | None:
        """Get a specific media list entry for a user."""
        query = """
        query ($mediaId: Int, $userId: Int) {
            MediaList(mediaId: $mediaId, userId: $userId) {
                id
                mediaId
                status
                score
                progress
                startedAt {
                    year
                    month
                    day
                }
                completedAt {
                    year
                    month
                    day
                }
            }
        }
        """

        variables = {"mediaId": media_id, "userId": user_id}
        result = await self._post_graphql(query, variables)
        return result.get("MediaList")

    async def list_media_entries(
        self,
        *,
        user_id: int | None = None,
        user_name: str | None = None,
        status: str | None = None,
        media_type: str = "ANIME",
        per_page: int = 50,
        max_pages: int | None = None,
        sort: str | None = "UPDATED_TIME_DESC",
    ) -> list[dict[str, Any]]:
        """Fetch AniList media list entries for a user."""
        if not user_id and not user_name:
            raise AniListError("AniList list fetch requires user_id or user_name")
        if user_id:
            user_name = None

        query = """
        query (
            $page: Int
            $perPage: Int
            $userId: Int
            $userName: String
            $status: MediaListStatus
            $type: MediaType
            $sort: [MediaListSort]
        ) {
            Page(page: $page, perPage: $perPage) {
                pageInfo {
                    hasNextPage
                }
                mediaList(
                    userId: $userId
                    userName: $userName
                    status: $status
                    type: $type
                    sort: $sort
                ) {
                    id
                    status
                    score(format: POINT_10)
                    progress
                    updatedAt
                    startedAt {
                        year
                        month
                        day
                    }
                    completedAt {
                        year
                        month
                        day
                    }
                    media {
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
        }
        """

        entries: list[dict[str, Any]] = []
        page = 1
        while True:
            variables: dict[str, Any] = {
                "page": page,
                "perPage": per_page,
                "userId": user_id,
                "userName": user_name,
                "status": status,
                "type": media_type,
                "sort": [sort] if sort else None,
            }
            result = await self._post_graphql(query, variables)
            page_data = result.get("Page") or {}
            raw_entries = page_data.get("mediaList") or []
            if isinstance(raw_entries, list):
                entries.extend([entry for entry in raw_entries if isinstance(entry, dict)])
            page_info = page_data.get("pageInfo") or {}
            if not page_info.get("hasNextPage"):
                break
            page += 1
            if max_pages is not None and page > max_pages:
                break
        return entries

    def _date_to_fuzzy(self, dt: datetime) -> dict[str, int]:
        """Convert datetime to AniList FuzzyDate format."""
        return {
            "year": dt.year,
            "month": dt.month,
            "day": dt.day,
        }

    async def _post_graphql(
        self, query: str, variables: dict[str, Any]
    ) -> dict[str, Any]:
        """Execute GraphQL query with authentication."""
        async with httpx.AsyncClient(timeout=self._timeout) as client:
            response = await client.post(
                self._api_url,
                json={"query": query, "variables": variables},
                headers={
                    "Authorization": f"Bearer {self._access_token}",
                    "Content-Type": "application/json",
                    "Accept": "application/json",
                    "User-Agent": "librarysync/service",
                },
            )

            if response.status_code >= 400:
                body = response.text
                raise AniListError(
                    f"AniList API error: {response.status_code}",
                    status_code=response.status_code,
                    response_body=body,
                )

            result = response.json()

            if "errors" in result:
                errors = result["errors"]
                error_messages = [e.get("message", str(e)) for e in errors]
                raise AniListError(f"AniList GraphQL errors: {', '.join(error_messages)}")

            return result.get("data", {})


async def exchange_code_for_token(
    code: str,
    client_id: str,
    client_secret: str,
    redirect_uri: str,
) -> AniListToken:
    """Exchange OAuth authorization code for access token."""
    async with httpx.AsyncClient(timeout=30.0) as client:
        response = await client.post(
            ANILIST_OAUTH_TOKEN_URL,
            headers={
                "Content-Type": "application/json",
                "Accept": "application/json",
                "User-Agent": "librarysync/service",
            },
            json={
                "grant_type": "authorization_code",
                "client_id": client_id,
                "client_secret": client_secret,
                "redirect_uri": redirect_uri,
                "code": code,
            },
        )

        if response.status_code >= 400:
            body = response.text
            raise AniListError(
                f"AniList token exchange failed: {response.status_code}",
                status_code=response.status_code,
                response_body=body,
            )

        payload = response.json()
        return normalize_token_payload(payload)


def build_oauth_url(
    client_id: str,
    redirect_uri: str,
    state: str,
) -> str:
    """Build AniList OAuth authorization URL."""
    params = {
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "state": state,
    }
    return f"{ANILIST_OAUTH_AUTHORIZE_URL}?{urlencode(params)}"
