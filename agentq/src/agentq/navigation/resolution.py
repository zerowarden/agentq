"""Provider resolution: query applicable language providers, then fall back.

A provider returning no payload is never interpreted as complete: it becomes an
explicit ``unavailable`` result whose coverage cannot claim completeness.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from agentq.core import (
    LEXICAL,
    PARSE_ERROR,
    PROVIDER_ERROR,
    AgentQError,
    Budget,
    ContractError,
    Coverage,
    Diagnostic,
    ProviderResult,
    ProviderStatus,
    RequestContext,
    best_provenance,
    failed_result,
    merge_typed,
    typed_coverage,
    unavailable_result,
)

from .models import (
    NavigationPayload,
    NavigationProvider,
    NavigationRequest,
    ProviderMetadata,
    TypeScriptNav,
    payload_candidate_count,
    payload_coverage,
    payload_errors,
    payload_provenance,
)
from .providers.lexical import LexicalFallbackProvider
from .providers.python import PythonOverview, PythonProvider
from .providers.typescript import TypeScriptProvider

LANGUAGE_PROVIDERS: tuple[NavigationProvider, ...] = (
    TypeScriptProvider(),
    PythonProvider(),
)
LEXICAL_FALLBACK: NavigationProvider = LexicalFallbackProvider()


def _query(
    provider,
    request: NavigationRequest,
    include_references: bool,
) -> ProviderResult[NavigationPayload]:
    try:
        method = provider.overview if include_references else provider.locate
        payload = method(request)
    except (AgentQError, ContractError) as exc:
        # A missing runtime never reaches here: providers return None for it,
        # which maps to unavailable below. Everything else is a failure.
        return failed_result(provider.name, str(exc), provenance=provider.provenance)
    if payload is None:
        return unavailable_result(
            provider.name,
            "provider returned no payload for an applicable request",
            provenance=provider.provenance,
        )
    if not isinstance(payload, (PythonOverview, TypeScriptNav)) and not hasattr(
        payload, "hits"
    ):
        return failed_result(
            provider.name,
            "provider returned an unsupported payload",
            provenance=provider.provenance,
        )
    candidate_count = payload_candidate_count(payload)
    diagnostics = tuple(
        Diagnostic(
            message=message,
            code=(PARSE_ERROR if "parse" in message.lower() else PROVIDER_ERROR),
        )
        for message in payload_errors(payload)
    )
    return ProviderResult(
        provider=provider.name,
        status=ProviderStatus.EMPTY if candidate_count == 0 else ProviderStatus.OK,
        payload=payload,
        provenance=provider.provenance,
        candidate_count=candidate_count,
        coverage=payload_coverage(payload),
        diagnostics=diagnostics,
    )


@dataclass
class SymbolResolution:
    outcomes: tuple[ProviderResult[NavigationPayload], ...]
    fallback: ProviderResult[NavigationPayload] | None = None

    def _all(self) -> tuple[ProviderResult[NavigationPayload], ...]:
        return (
            *self.outcomes,
            *((self.fallback,) if self.fallback is not None else ()),
        )

    def entries(self) -> tuple[ProviderMetadata, ...]:
        return tuple(
            ProviderMetadata(
                provider=result.provider,
                available=result.status in {ProviderStatus.OK, ProviderStatus.EMPTY},
                candidate_count=payload_candidate_count(result.payload),
                provenance=payload_provenance(result.payload),
                coverage=result.coverage,
                errors=tuple(item.message for item in result.diagnostics),
            )
            for result in self._all()
        )

    def coverage(self) -> Coverage:
        blocks = [
            result.coverage
            for result in self._all()
            if result.status is not ProviderStatus.NOT_APPLICABLE
        ]
        if not blocks:
            return typed_coverage("unknown")
        return merge_typed(*blocks)

    def provenance(self) -> str:
        with_candidates = [
            payload_provenance(result.payload)
            for result in self._all()
            if payload_candidate_count(result.payload)
        ]
        return best_provenance(*with_candidates) or LEXICAL

    def payload(self, name: str) -> NavigationPayload | None:
        for outcome in self.outcomes:
            if outcome.provider == name:
                return outcome.payload
        return None


def resolve_symbol(
    root: Path,
    symbol: str,
    *,
    paths: list[str] | None = None,
    limit: int = 80,
    lang: str | None = None,
    context: int = 0,
    pick: int | None = None,
    include_references: bool = True,
    request_context: RequestContext | None = None,
    budget: Budget | None = None,
) -> SymbolResolution:
    """Query every applicable language provider, then fall back to lexical search."""
    request = NavigationRequest(
        root=root,
        symbol=symbol,
        paths=tuple(paths or ()),
        limit=limit,
        lang=lang,
        context=context,
        pick=pick,
        request_context=request_context or RequestContext(),
        budget=budget or Budget(),
    )
    outcomes = tuple(
        _query(provider, request, include_references)
        for provider in LANGUAGE_PROVIDERS
        if provider.supports(request)
    )
    resolution = SymbolResolution(outcomes=outcomes)
    if not any(payload_candidate_count(outcome.payload) for outcome in outcomes):
        resolution.fallback = _query(LEXICAL_FALLBACK, request, include_references)
    return resolution
