from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass

from librarysync.connectors.metadata.base import (
    MEDIA_SCOPE_ALL,
    MediaCandidate,
    MetadataProvider,
)

DEFAULT_SCOPE_ORDER = ("movie", "tv", "anime")


@dataclass(frozen=True)
class LookupRequest:
    query: str
    query_type: str
    scope: str


class LookupStrategy(ABC):
    @abstractmethod
    def supports(self, provider: MetadataProvider, request: LookupRequest) -> bool:
        raise NotImplementedError

    @abstractmethod
    async def lookup(
        self, provider: MetadataProvider, request: LookupRequest
    ) -> list[MediaCandidate]:
        raise NotImplementedError


class ExternalIdLookupStrategy(LookupStrategy):
    def supports(self, provider: MetadataProvider, request: LookupRequest) -> bool:
        if request.query_type != "imdb":
            return False
        return provider.capabilities.supports_external_id and provider.supports_scope(
            request.scope
        )

    async def lookup(
        self, provider: MetadataProvider, request: LookupRequest
    ) -> list[MediaCandidate]:
        return await provider.find_by_external_id(request.query, request.scope)


class ProviderIdLookupStrategy(LookupStrategy):
    def supports(self, provider: MetadataProvider, request: LookupRequest) -> bool:
        if request.query_type != provider.provider:
            return False
        return provider.capabilities.supports_details

    async def lookup(
        self, provider: MetadataProvider, request: LookupRequest
    ) -> list[MediaCandidate]:
        scopes = self._resolve_scopes(provider, request.scope)
        last_error: Exception | None = None
        for scope in scopes:
            try:
                candidate = await provider.get_details(request.query, scope)
            except Exception as exc:
                last_error = exc
                continue
            if candidate.provider_id:
                return [candidate]
        if last_error:
            raise last_error
        return []

    def _resolve_scopes(self, provider: MetadataProvider, scope: str) -> list[str]:
        if scope != MEDIA_SCOPE_ALL:
            return [scope] if provider.supports_scope(scope) else []
        if provider.capabilities.scopes:
            return [value for value in DEFAULT_SCOPE_ORDER if value in provider.capabilities.scopes]
        return list(DEFAULT_SCOPE_ORDER)


class TitleLookupStrategy(LookupStrategy):
    def supports(self, provider: MetadataProvider, request: LookupRequest) -> bool:
        if request.query_type != "title":
            return False
        return provider.capabilities.supports_search and provider.supports_scope(request.scope)

    async def lookup(
        self, provider: MetadataProvider, request: LookupRequest
    ) -> list[MediaCandidate]:
        return await provider.search(request.query, request.scope)


class MetadataLookupEngine:
    def __init__(
        self,
        strategies: list[LookupStrategy] | None = None,
        detail_limit: int = 5,
    ) -> None:
        self._strategies = strategies or [
            ExternalIdLookupStrategy(),
            ProviderIdLookupStrategy(),
            TitleLookupStrategy(),
        ]
        self._detail_limit = detail_limit

    async def lookup(
        self, provider: MetadataProvider, request: LookupRequest
    ) -> list[MediaCandidate]:
        for strategy in self._strategies:
            if not strategy.supports(provider, request):
                continue
            candidates = await strategy.lookup(provider, request)
            return await self._enrich_candidates(provider, candidates)
        return []

    async def _enrich_candidates(
        self, provider: MetadataProvider, candidates: list[MediaCandidate]
    ) -> list[MediaCandidate]:
        if not provider.capabilities.supports_details:
            return candidates
        enriched: list[MediaCandidate] = []
        for idx, candidate in enumerate(candidates):
            if idx < self._detail_limit and candidate.provider_id:
                try:
                    enriched.append(
                        await provider.get_details(candidate.provider_id, candidate.media_type)
                    )
                    continue
                except Exception:
                    pass
            enriched.append(candidate)
        return enriched
