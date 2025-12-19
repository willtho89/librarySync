from dataclasses import dataclass
from typing import Protocol, runtime_checkable


@dataclass(frozen=True)
class MediaCandidate:
    provider: str
    provider_id: str
    media_type: str
    title: str
    year: int | None
    poster_url: str | None
    imdb_id: str | None
    raw: dict


class MetadataProvider(Protocol):
    provider: str

    async def search(self, query: str, scope: str = "all") -> list[MediaCandidate]:
        raise NotImplementedError

    async def find_by_external_id(
        self, external_id: str, scope: str = "all"
    ) -> list[MediaCandidate]:
        raise NotImplementedError

    async def get_details(self, provider_id: str, media_type: str) -> MediaCandidate:
        raise NotImplementedError

    async def validate_credentials(self) -> None:
        raise NotImplementedError


@dataclass(frozen=True)
class SeasonSummary:
    season_number: int
    name: str | None
    episode_count: int | None
    air_date: str | None
    poster_url: str | None


@dataclass(frozen=True)
class EpisodeSummary:
    episode_number: int
    title: str | None
    provider_id: str | None
    air_date: str | None
    still_url: str | None


@runtime_checkable
class EpisodeProvider(Protocol):
    provider: str

    async def list_seasons(self, provider_id: str) -> list[SeasonSummary]:
        raise NotImplementedError

    async def list_episodes(
        self, provider_id: str, season_number: int
    ) -> list[EpisodeSummary]:
        raise NotImplementedError
