"""Provider resolution: query applicable language providers, then fall back.

A provider returning no evidence is never interpreted as complete: it becomes
an explicit ``unavailable`` result whose coverage cannot claim completeness.
Orchestration consumes canonical
:class:`~agentq.navigation.models.SymbolEvidence` only; concrete provider
payloads never cross this seam.
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
from agentq.core.languages import language_id_for
from agentq.discovery import list_repo_files

from .models import (
    NavigationProvider,
    NavigationRequest,
    ProviderMetadata,
    SymbolEvidence,
)
from .providers.lexical import LexicalFallbackProvider
from .providers.python import PythonProvider
from .providers.typescript import TypeScriptProvider

LANGUAGE_PROVIDERS: tuple[NavigationProvider, ...] = (
    TypeScriptProvider(),
    PythonProvider(),
)
LEXICAL_FALLBACK: NavigationProvider = LexicalFallbackProvider()

_PROVIDER_LANGUAGES: dict[str, frozenset[str]] = {
    "typescript": frozenset({"typescript", "tsx", "javascript", "jsx"}),
    "python": frozenset({"python"}),
}


def _scope_languages(root: Path, paths: tuple[str, ...]) -> frozenset[str] | None:
    """Languages present in explicit scopes; ``None`` when not enumerable.

    A provider whose language is absent from every scope is not applicable, so
    its unavailable runtime cannot downgrade resolution for the scopes.
    """
    if not paths:
        return None
    try:
        files = list_repo_files(root)
    except (AgentQError, OSError):
        return None
    languages: set[str] = set()
    for scope in paths:
        prefix = scope.rstrip("/") + "/"
        scoped = [item for item in files if item == scope or item.startswith(prefix)]
        if not scoped:
            return None
        languages.update(
            language for item in scoped if (language := language_id_for(item))
        )
    return frozenset(languages)


def _applicable_providers(request: NavigationRequest) -> tuple[NavigationProvider, ...]:
    scoped = _scope_languages(request.root, request.paths)
    return tuple(
        provider
        for provider in LANGUAGE_PROVIDERS
        if provider.supports(request)
        and (
            scoped is None
            or request.lang is not None
            or bool(_PROVIDER_LANGUAGES.get(provider.name, frozenset()) & scoped)
        )
    )


def query_provider(
    provider: NavigationProvider,
    request: NavigationRequest,
    include_references: bool,
) -> ProviderResult[SymbolEvidence]:
    try:
        evidence = provider.inspect_symbol(
            request, include_references=include_references
        )
    except (AgentQError, ContractError) as exc:
        # A missing runtime never reaches here: providers return None for it,
        # which maps to unavailable below. Everything else is a failure.
        return failed_result(provider.name, str(exc), provenance=provider.provenance)
    if evidence is None:
        return unavailable_result(
            provider.name,
            "provider returned no evidence for an applicable request",
            provenance=provider.provenance,
        )
    diagnostics = tuple(
        Diagnostic(
            message=message,
            code=(PARSE_ERROR if "parse" in message.lower() else PROVIDER_ERROR),
        )
        for message in evidence.diagnostics
    )
    return ProviderResult(
        provider=provider.name,
        status=(
            ProviderStatus.EMPTY if evidence.candidate_count == 0 else ProviderStatus.OK
        ),
        payload=evidence,
        provenance=provider.provenance,
        candidate_count=evidence.candidate_count,
        coverage=evidence.coverage,
        diagnostics=diagnostics,
    )


@dataclass
class SymbolResolution:
    outcomes: tuple[ProviderResult[SymbolEvidence], ...]
    fallback: ProviderResult[SymbolEvidence] | None = None

    def _all(self) -> tuple[ProviderResult[SymbolEvidence], ...]:
        return (
            *self.outcomes,
            *((self.fallback,) if self.fallback is not None else ()),
        )

    def entries(self) -> tuple[ProviderMetadata, ...]:
        return tuple(
            ProviderMetadata(
                provider=result.provider,
                available=result.status in {ProviderStatus.OK, ProviderStatus.EMPTY},
                candidate_count=result.candidate_count or 0,
                provenance=result.provenance or LEXICAL,
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
            result.provenance for result in self._all() if result.candidate_count
        ]
        return best_provenance(*with_candidates) or LEXICAL

    def evidence(self, name: str) -> SymbolEvidence | None:
        for outcome in self.outcomes:
            if outcome.provider == name:
                payload = outcome.payload
                return payload if isinstance(payload, SymbolEvidence) else None
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
        query_provider(provider, request, include_references)
        for provider in _applicable_providers(request)
    )
    resolution = SymbolResolution(outcomes=outcomes)
    if not any(outcome.candidate_count for outcome in outcomes):
        resolution.fallback = query_provider(
            LEXICAL_FALLBACK, request, include_references
        )
    return resolution
