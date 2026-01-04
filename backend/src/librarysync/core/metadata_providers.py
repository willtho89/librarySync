from __future__ import annotations

import json
from dataclasses import dataclass, fields
from typing import Any, Callable, Iterable

from pydantic import BaseModel, field_validator
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from librarysync.connectors.metadata.anilist import AniListMetadataProvider
from librarysync.connectors.metadata.base import MetadataProvider, ProviderContext
from librarysync.connectors.metadata.imdb import ImdbMetadataProvider
from librarysync.connectors.metadata.kitsu import KitsuMetadataProvider
from librarysync.connectors.metadata.myanimelist import MyAnimeListMetadataProvider
from librarysync.connectors.metadata.tmdb import TmdbMetadataProvider
from librarysync.connectors.metadata.tvdb import TvdbMetadataProvider
from librarysync.connectors.metadata.tvmaze import TvmazeMetadataProvider
from librarysync.core.integrations import load_integration_with_secrets
from librarysync.core.security import encrypt_value
from librarysync.db.models import Integration, IntegrationSecret, User


def _normalize_optional_str(value: Any) -> Any:
    if not isinstance(value, str):
        return value
    stripped = value.strip()
    return stripped or None


class TmdbProviderSettings(BaseModel):
    enabled: bool = True
    api_key: str | None = None
    language: str | None = None
    region: str | None = None

    @field_validator("api_key", "language", "region", mode="before")
    @classmethod
    def _normalize_fields(cls, value: Any) -> Any:
        return _normalize_optional_str(value)


class TvdbProviderSettings(BaseModel):
    enabled: bool = True
    api_key: str | None = None
    pin: str | None = None
    language: str | None = None

    @field_validator("api_key", "pin", "language", mode="before")
    @classmethod
    def _normalize_fields(cls, value: Any) -> Any:
        return _normalize_optional_str(value)


class KitsuProviderSettings(BaseModel):
    enabled: bool = True
    language: str | None = None

    @field_validator("language", mode="before")
    @classmethod
    def _normalize_fields(cls, value: Any) -> Any:
        return _normalize_optional_str(value)


class TvmazeProviderSettings(BaseModel):
    enabled: bool = True


class ImdbProviderSettings(BaseModel):
    enabled: bool = True


class MyAnimeListProviderSettings(BaseModel):
    enabled: bool = True


class AniListProviderSettings(BaseModel):
    enabled: bool = True


@dataclass(frozen=True)
class ProviderSettingsUpdate:
    config: dict[str, Any]
    secrets: dict[str, Any]
    should_update_secrets: bool
    has_required_secrets: bool


@dataclass(frozen=True)
class ProviderState:
    provider: str
    enabled: bool
    config: dict[str, Any]
    has_credentials: bool


@dataclass(frozen=True)
class ProviderDefinition:
    provider: str
    settings_model: type[BaseModel]
    provider_class: type[MetadataProvider]
    secret_fields: frozenset[str] = frozenset()
    required_secrets: frozenset[str] = frozenset()
    secret_update_fields: frozenset[str] | None = None
    supports_episodes: bool = False
    config_adapter: Callable[[dict[str, Any], ProviderContext], dict[str, Any]] | None = None

    def uses_secrets(self) -> bool:
        return bool(self.secret_fields)

    def normalize_config(
        self, config: dict[str, Any], context: ProviderContext
    ) -> dict[str, Any]:
        normalized = dict(config)
        normalized = self._filter_config_fields(normalized)
        if self.config_adapter:
            normalized = self.config_adapter(normalized, context)
        return normalized

    def _filter_config_fields(self, config: dict[str, Any]) -> dict[str, Any]:
        schema = self.provider_class.config_schema
        if not schema:
            return {}
        allowed = {field.name for field in fields(schema)}
        return {key: value for key, value in config.items() if key in allowed}

    def extract_update(self, payload: BaseModel) -> ProviderSettingsUpdate:
        data = payload.model_dump()
        config = {key: value for key, value in data.items() if key not in self.secret_fields}
        fields_set = payload.model_fields_set
        update_fields = self.secret_update_fields or self.required_secrets or self.secret_fields
        should_update = bool(update_fields.intersection(fields_set)) if update_fields else False
        secrets: dict[str, Any] = {}
        has_required = False
        if should_update and self.secret_fields:
            for field in self.secret_fields:
                secrets[field] = data.get(field)
            if self.required_secrets:
                has_required = all(secrets.get(field) for field in self.required_secrets)
        return ProviderSettingsUpdate(
            config=config,
            secrets=secrets,
            should_update_secrets=should_update,
            has_required_secrets=has_required,
        )


class MetadataProviderRegistry:
    def __init__(self, definitions: Iterable[ProviderDefinition]):
        self._definitions: dict[str, ProviderDefinition] = {}
        for definition in definitions:
            self.register(definition)

    def get(self, provider: str) -> ProviderDefinition | None:
        return self._definitions.get(provider)

    def list(self) -> list[ProviderDefinition]:
        return list(self._definitions.values())

    @property
    def providers(self) -> tuple[str, ...]:
        return tuple(self._definitions.keys())

    def register(self, definition: ProviderDefinition) -> None:
        self._definitions[definition.provider] = definition


class MetadataProviderFactory:
    def __init__(self, registry: MetadataProviderRegistry):
        self._registry = registry

    def build(
        self,
        definition: ProviderDefinition,
        config: dict[str, Any],
        secrets: dict[str, Any] | None,
        context: ProviderContext,
    ) -> MetadataProvider:
        normalized = definition.normalize_config(config, context)
        return definition.provider_class.from_settings(normalized, secrets, context)


def _tmdb_config_adapter(config: dict[str, Any], context: ProviderContext) -> dict[str, Any]:
    normalized = dict(config)
    normalized["include_adult"] = context.include_adult
    return normalized


METADATA_PROVIDER_REGISTRY = MetadataProviderRegistry(
    [
        ProviderDefinition(
            provider="tmdb",
            settings_model=TmdbProviderSettings,
            provider_class=TmdbMetadataProvider,
            secret_fields=frozenset({"api_key"}),
            required_secrets=frozenset({"api_key"}),
            secret_update_fields=frozenset({"api_key"}),
            supports_episodes=True,
            config_adapter=_tmdb_config_adapter,
        ),
        ProviderDefinition(
            provider="tvdb",
            settings_model=TvdbProviderSettings,
            provider_class=TvdbMetadataProvider,
            secret_fields=frozenset({"api_key", "pin"}),
            required_secrets=frozenset({"api_key"}),
            secret_update_fields=frozenset({"api_key"}),
        ),
        ProviderDefinition(
            provider="tvmaze",
            settings_model=TvmazeProviderSettings,
            provider_class=TvmazeMetadataProvider,
        ),
        ProviderDefinition(
            provider="imdb",
            settings_model=ImdbProviderSettings,
            provider_class=ImdbMetadataProvider,
        ),
        ProviderDefinition(
            provider="kitsu",
            settings_model=KitsuProviderSettings,
            provider_class=KitsuMetadataProvider,
        ),
        ProviderDefinition(
            provider="myanimelist",
            settings_model=MyAnimeListProviderSettings,
            provider_class=MyAnimeListMetadataProvider,
        ),
        ProviderDefinition(
            provider="anilist",
            settings_model=AniListProviderSettings,
            provider_class=AniListMetadataProvider,
        ),
    ]
)


class MetadataProviderService:
    def __init__(
        self,
        db: AsyncSession,
        user_id: str,
        registry: MetadataProviderRegistry | None = None,
    ) -> None:
        self._db = db
        self._user_id = user_id
        self._registry = registry or METADATA_PROVIDER_REGISTRY
        self._factory = MetadataProviderFactory(self._registry)
        self._context: ProviderContext | None = None

    async def list_provider_states(self) -> list[ProviderState]:
        result = await self._db.execute(
            select(Integration).where(
                Integration.user_id == self._user_id,
                Integration.provider.in_(self._registry.providers),
            )
        )
        integrations = {
            integration.provider: integration for integration in result.scalars().all()
        }
        integration_ids = [integration.id for integration in integrations.values()]
        if integration_ids:
            result = await self._db.execute(
                select(IntegrationSecret.integration_id).where(
                    IntegrationSecret.integration_id.in_(integration_ids)
                )
            )
            secret_ids = set(result.scalars().all())
        else:
            secret_ids = set()

        states: list[ProviderState] = []
        for definition in self._registry.list():
            integration = integrations.get(definition.provider)
            config = integration.config if integration and integration.config else {}
            enabled = bool(config.get("enabled")) if integration else False
            has_credentials = False
            if integration:
                has_credentials = integration.id in secret_ids or not definition.uses_secrets()
            states.append(
                ProviderState(
                    provider=definition.provider,
                    enabled=enabled,
                    config=config,
                    has_credentials=has_credentials,
                )
            )
        return states

    async def save_provider_settings(
        self, provider: str, payload: BaseModel
    ) -> ProviderState:
        definition = self._registry.get(provider)
        if not definition:
            raise ValueError(f"Unknown provider: {provider}")

        result = await self._db.execute(
            select(Integration).where(
                Integration.user_id == self._user_id, Integration.provider == provider
            )
        )
        integration = result.scalars().first()
        if not integration:
            integration = Integration(user_id=self._user_id, provider=provider)

        update = definition.extract_update(payload)
        config = dict(integration.config or {})
        config.update(update.config)
        integration.config = config
        integration.status = "enabled" if config.get("enabled") else "disabled"
        self._db.add(integration)
        await self._db.flush()

        has_credentials = False
        if update.should_update_secrets and definition.uses_secrets():
            result = await self._db.execute(
                select(IntegrationSecret).where(
                    IntegrationSecret.integration_id == integration.id
                )
            )
            secret = result.scalars().first()
            if update.has_required_secrets:
                encrypted = encrypt_value(json.dumps(update.secrets))
                if not secret:
                    secret = IntegrationSecret(
                        integration_id=integration.id,
                        secret_data=encrypted,
                    )
                else:
                    secret.secret_data = encrypted
                self._db.add(secret)
                has_credentials = True
            else:
                if secret:
                    await self._db.delete(secret)
                has_credentials = False
        else:
            if definition.uses_secrets():
                result = await self._db.execute(
                    select(IntegrationSecret.integration_id).where(
                        IntegrationSecret.integration_id == integration.id
                    )
                )
                has_credentials = result.scalar_one_or_none() is not None
            else:
                has_credentials = True

        await self._db.commit()
        return ProviderState(
            provider=provider,
            enabled=bool(config.get("enabled")),
            config=config,
            has_credentials=has_credentials,
        )

    async def load_provider(self, provider: str) -> MetadataProvider | None:
        definition = self._registry.get(provider)
        if not definition:
            return None
        integration, secret_data = await load_integration_with_secrets(
            self._db, self._user_id, provider
        )
        if not integration or not integration.config:
            return None
        if not integration.config.get("enabled"):
            return None
        if definition.uses_secrets() and not secret_data:
            return None
        context = await self._load_context()
        try:
            return self._factory.build(
                definition,
                dict(integration.config or {}),
                secret_data,
                context,
            )
        except ValueError:
            return None

    async def load_enabled_providers(self) -> list[MetadataProvider]:
        providers: list[MetadataProvider] = []
        for definition in self._registry.list():
            provider = await self.load_provider(definition.provider)
            if provider:
                providers.append(provider)
        return providers

    async def _load_context(self) -> ProviderContext:
        if self._context:
            return self._context
        result = await self._db.execute(
            select(User.include_adult_in_search).where(User.id == self._user_id)
        )
        include_adult = bool(result.scalar_one_or_none())
        self._context = ProviderContext(user_id=self._user_id, include_adult=include_adult)
        return self._context
