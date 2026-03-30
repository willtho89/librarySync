from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Generic, Mapping, TypeVar

from pydantic import BaseModel, Field, field_validator

MEDIA_SCOPE_ALL = "all"
MEDIA_SCOPE_MOVIE = "movie"
MEDIA_SCOPE_TV = "tv"
MEDIA_SCOPE_ANIME = "anime"


class MediaCandidate(BaseModel):
    provider: str
    provider_id: str
    media_type: str
    title: str
    year: int | None
    poster_url: str | None
    imdb_id: str | None
    release_date: str | None = None
    first_air_date: str | None = None
    last_air_date: str | None = None
    runtime_in_seconds: int | None = None
    genres: list[str] | None = None
    overview: str | None = None
    raw: dict = Field(default_factory=dict)

    @field_validator("genres", mode="before")
    @classmethod
    def normalize_genres(cls, v):
        if v is None:
            return None
        if isinstance(v, list):
            return [g["name"] if isinstance(g, dict) and "name" in g else str(g) for g in v if g]
        return None


@dataclass(frozen=True)
class ProviderContext:
    user_id: str
    include_adult: bool = False


@dataclass(frozen=True)
class ProviderCapabilities:
    scopes: set[str] = field(default_factory=set)
    supports_external_id: bool = True
    supports_search: bool = True
    supports_details: bool = True
    supports_episodes: bool = False

    def supports_scope(self, scope: str) -> bool:
        if scope == MEDIA_SCOPE_ALL:
            return True
        return scope in self.scopes


@dataclass(frozen=True)
class ProviderConfig:
    pass


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


ConfigT = TypeVar("ConfigT")
SecretsT = TypeVar("SecretsT")


class MetadataProvider(ABC, Generic[ConfigT, SecretsT]):
    provider: str
    capabilities = ProviderCapabilities()
    config_schema: type[ConfigT] | None = ProviderConfig
    secrets_schema: type[SecretsT] | None = None

    def __init__(self, config: ConfigT, secrets: SecretsT | None, context: ProviderContext):
        self._config = config
        self._secrets = secrets
        self._context = context

    @property
    def config(self) -> ConfigT:
        return self._config

    @property
    def secrets(self) -> SecretsT | None:
        return self._secrets

    @property
    def context(self) -> ProviderContext:
        return self._context

    def supports_scope(self, scope: str) -> bool:
        return self.capabilities.supports_scope(scope)

    @classmethod
    def from_settings(
        cls,
        config: Mapping[str, Any] | None,
        secrets: Mapping[str, Any] | None,
        context: ProviderContext,
    ) -> "MetadataProvider":
        config_obj = cls._build_schema(cls.config_schema, config or {}, "config")
        secrets_obj = None
        if cls.secrets_schema is not None:
            secrets_obj = cls._build_schema(cls.secrets_schema, secrets or {}, "secrets")
        return cls(config_obj, secrets_obj, context)

    @staticmethod
    def _build_schema(schema: type[Any] | None, data: Mapping[str, Any], label: str) -> Any:
        if schema is None:
            return None
        try:
            return schema(**data)
        except TypeError as exc:
            raise ValueError(f"{label} settings are invalid: {exc}") from exc

    @abstractmethod
    async def search(self, query: str, scope: str = MEDIA_SCOPE_ALL) -> list[MediaCandidate]:
        raise NotImplementedError

    async def find_by_external_id(
        self, external_id: str, scope: str = MEDIA_SCOPE_ALL
    ) -> list[MediaCandidate]:
        return []

    @abstractmethod
    async def get_details(self, provider_id: str, media_type: str) -> MediaCandidate | None:
        raise NotImplementedError

    @abstractmethod
    async def validate_credentials(self) -> None:
        raise NotImplementedError


class EpisodeMetadataProvider(MetadataProvider[ConfigT, SecretsT], ABC):
    @abstractmethod
    async def list_seasons(self, provider_id: str) -> list[SeasonSummary]:
        raise NotImplementedError

    @abstractmethod
    async def list_episodes(self, provider_id: str, season_number: int) -> list[EpisodeSummary]:
        raise NotImplementedError
