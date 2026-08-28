"""Language-provider layer for semantic symbol navigation.

Providers own everything language-specific (process launches, parsers,
runtimes); orchestrators such as `inspect` only consume provider outcomes.
This is the seam a persistent language daemon can later plug into without
changing command semantics (AQ-019).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

from .evidence import (
    LEXICAL, PARTIAL, PROVIDER_UNAVAILABLE, SEMANTIC, SYNTACTIC,
    best_provenance, complete as complete_coverage, coverage as coverage_block, merge_coverage,
)
from .pythonnav import python_symbol_overview
from .search import search_data
from .tsnav import ts_nav_data


@dataclass(frozen=True)
class NavigationRequest:
    root: Path
    symbol: str
    paths: tuple[str, ...]
    limit: int
    lang: str | None = None  # explicit language restriction: typescript | python
    context: int = 0         # snippet context for the lexical fallback only


@runtime_checkable
class NavigationProvider(Protocol):
    name: str
    provenance: str

    def supports(self, request: NavigationRequest) -> bool: ...
    def locate(self, request: NavigationRequest) -> dict[str, Any] | None: ...
    def overview(self, request: NavigationRequest) -> dict[str, Any] | None: ...
    def result_errors(self, result: dict[str, Any]) -> list[str]: ...


@dataclass
class ProviderOutcome:
    """One provider query: its payload, or the error that prevented one."""

    provider: NavigationProvider
    result: dict[str, Any] | None
    error: str | None = None
    _candidates: list[dict[str, Any]] = field(default_factory=list, repr=False)

    @property
    def name(self) -> str:
        return self.provider.name

    @property
    def provenance(self) -> str:
        return self.provider.provenance

    def candidates(self) -> list[dict[str, Any]]:
        return self._candidates

    def coverage(self) -> dict[str, Any]:
        if self.error is not None:
            return coverage_block(PARTIAL, PROVIDER_UNAVAILABLE)
        if self.result is None:
            return complete_coverage()
        return self.result.get("coverage") or complete_coverage()

    def metadata(self) -> dict[str, Any]:
        errors = self.provider.result_errors(self.result) if self.result else []
        if self.error is not None:
            errors = [self.error]
        return {
            "provider": self.name,
            "available": self.error is None,
            "candidate_count": len(self._candidates),
            "provenance": self.provenance,
            "coverage": self.coverage(),
            "errors": errors,
        }


def _outcome(provider: NavigationProvider, request: NavigationRequest, result: dict[str, Any] | None) -> ProviderOutcome:
    candidates: list[dict[str, Any]] = []
    if result is not None:
        value = result.get(provider.candidates_key)
        if isinstance(value, list):
            candidates = value
    return ProviderOutcome(provider=provider, result=result, _candidates=candidates)


def _query(provider: NavigationProvider, request: NavigationRequest, include_references: bool) -> ProviderOutcome:
    from .common import AgentQError

    try:
        method = provider.overview if include_references else provider.locate
        return _outcome(provider, request, method(request))
    except AgentQError as exc:  # provider failures degrade to recorded diagnostics
        return ProviderOutcome(provider=provider, result=None, error=str(exc))


class TypeScriptProvider:
    name = "typescript"
    provenance = SEMANTIC
    candidates_key = "candidates"

    def supports(self, request: NavigationRequest) -> bool:
        return request.lang in {None, "typescript"}

    def locate(self, request: NavigationRequest) -> dict[str, Any] | None:
        return ts_nav_data(
            request.root, "locate", None, None, None, request.limit,
            symbol=request.symbol, paths=list(request.paths), pick=None,
        )

    def overview(self, request: NavigationRequest) -> dict[str, Any] | None:
        return ts_nav_data(
            request.root, "overview", None, None, None, request.limit,
            symbol=request.symbol, paths=list(request.paths), pick=None,
        )

    def result_errors(self, result: dict[str, Any]) -> list[str]:
        return []


class PythonProvider:
    name = "python"
    provenance = SYNTACTIC
    candidates_key = "candidates"

    def supports(self, request: NavigationRequest) -> bool:
        return request.lang in {None, "python"}

    def locate(self, request: NavigationRequest) -> dict[str, Any] | None:
        return python_symbol_overview(
            request.root, request.symbol, list(request.paths), request.limit,
            include_references=False,
        )

    def overview(self, request: NavigationRequest) -> dict[str, Any] | None:
        return python_symbol_overview(
            request.root, request.symbol, list(request.paths), request.limit,
            include_references=True,
        )

    def result_errors(self, result: dict[str, Any]) -> list[str]:
        return [
            f"{item.get('path', '?')}: {item.get('error', 'unparseable')}"
            for item in result.get("parse_errors") or []
        ]


class LexicalFallbackProvider:
    name = "lexical"
    provenance = LEXICAL
    candidates_key = "hits"

    def supports(self, request: NavigationRequest) -> bool:
        return True

    def _search(self, request: NavigationRequest) -> dict[str, Any]:
        return search_data(
            request.root, request.symbol, list(request.paths),
            mode="fixed", word=False, case="smart", limit=request.limit,
            per_file=8, context=request.context, max_chars=240,
            include_sensitive=False, view="auto", max_files=40,
        )

    def locate(self, request: NavigationRequest) -> dict[str, Any] | None:
        return self._search(request)

    def overview(self, request: NavigationRequest) -> dict[str, Any] | None:
        return self._search(request)

    def result_errors(self, result: dict[str, Any]) -> list[str]:
        return []


LANGUAGE_PROVIDERS: tuple[NavigationProvider, ...] = (TypeScriptProvider(), PythonProvider())
LEXICAL_FALLBACK = LexicalFallbackProvider()


@dataclass
class SymbolResolution:
    outcomes: list[ProviderOutcome]
    fallback: ProviderOutcome | None = None

    def entries(self) -> list[dict[str, Any]]:
        outcomes = list(self.outcomes) + ([self.fallback] if self.fallback else [])
        return [outcome.metadata() for outcome in outcomes]

    def coverage(self) -> dict[str, Any]:
        blocks = [outcome.coverage() for outcome in self.outcomes]
        if self.fallback is not None:
            blocks.append(self.fallback.coverage())
        return merge_coverage(*blocks)

    def provenance(self) -> str:
        outcomes = list(self.outcomes)
        if self.fallback is not None:
            outcomes.append(self.fallback)
        with_candidates = [outcome.provenance for outcome in outcomes if outcome.candidates()]
        return best_provenance(*with_candidates) or LEXICAL

    def result(self, name: str) -> dict[str, Any] | None:
        for outcome in self.outcomes:
            if outcome.name == name:
                return outcome.result
        return None


def resolve_symbol(
    root: Path,
    symbol: str,
    *,
    paths: list[str] | None = None,
    limit: int = 80,
    lang: str | None = None,
    context: int = 0,
    include_references: bool = True,
) -> SymbolResolution:
    """Query every applicable language provider, then fall back to lexical search."""
    request = NavigationRequest(
        root=root, symbol=symbol, paths=tuple(paths or ()), limit=limit,
        lang=lang, context=context,
    )
    outcomes = [
        _query(provider, request, include_references)
        for provider in LANGUAGE_PROVIDERS
        if provider.supports(request)
    ]
    resolution = SymbolResolution(outcomes=outcomes)
    if not any(outcome.candidates() for outcome in outcomes):
        resolution.fallback = _query(LEXICAL_FALLBACK, request, include_references)
    return resolution
