"""Language-provider layer for semantic symbol navigation.

Providers own everything language-specific (process launches, parsers,
runtimes); orchestrators such as `inspect` only consume typed provider results.
This is the seam a persistent language daemon can later plug into without
changing command semantics (AQ-019).

A provider returning no payload is never interpreted as complete: it becomes an
explicit ``unavailable`` result whose coverage cannot claim completeness.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

from .common import AgentQError
from .contracts._base import ContractError
from .contracts.request import Budget, RequestContext
from .contracts.result import (
    ProviderResult,
    ProviderStatus,
    failed_result,
    unavailable_result,
)
from .evidence import (
    LEXICAL,
    PARSE_ERROR,
    PROVIDER_ERROR,
    SEMANTIC,
    SYNTACTIC,
    Diagnostic,
    best_provenance,
    merge_typed,
    typed_from_wire,
)
from .pythonnav import python_symbol_overview
from .search import search_data
from .tsnav import ts_nav_data


@dataclass(frozen=True)
class NavigationRequest:
    """Typed navigation request; host identity and budgets are separate facts."""

    root: Path
    symbol: str
    paths: tuple[str, ...] = ()
    limit: int = 80
    lang: str | None = None
    context: int = 0
    pick: int | None = None
    request_context: RequestContext = field(default_factory=RequestContext)
    budget: Budget = field(default_factory=Budget)

    def __post_init__(self) -> None:
        if not isinstance(self.symbol, str) or not self.symbol:
            raise ContractError("navigation symbol must be a non-empty string")
        if self.lang not in {None, "typescript", "python"}:
            raise ContractError(f"unsupported navigation language: {self.lang!r}")
        if self.pick is not None and (
            isinstance(self.pick, bool)
            or not isinstance(self.pick, int)
            or self.pick < 1
        ):
            raise ContractError("navigation pick must be a positive integer")
        if (
            isinstance(self.limit, bool)
            or not isinstance(self.limit, int)
            or self.limit < 1
        ):
            raise ContractError("navigation limit must be a positive integer")
        if (
            isinstance(self.context, bool)
            or not isinstance(self.context, int)
            or self.context < 0
        ):
            raise ContractError("navigation context must be a non-negative integer")
        if not isinstance(self.paths, tuple) or not all(
            isinstance(item, str) and item for item in self.paths
        ):
            raise ContractError("navigation paths must be a tuple of non-empty strings")


@runtime_checkable
class NavigationProvider(Protocol):
    name: str
    provenance: str
    candidates_key: str

    def supports(self, request: NavigationRequest) -> bool: ...
    def locate(self, request: NavigationRequest) -> dict[str, Any] | None: ...
    def overview(self, request: NavigationRequest) -> dict[str, Any] | None: ...
    def result_errors(self, result: dict[str, Any]) -> list[str]: ...


def _candidate_count(result: ProviderResult[Any]) -> int:
    """The producer-declared candidate count; unknown counts are zero here."""
    return result.candidate_count or 0


def _metadata(result: ProviderResult[Any]) -> dict[str, Any]:
    return {
        "provider": result.provider,
        "available": result.status in {ProviderStatus.OK, ProviderStatus.EMPTY},
        "candidate_count": _candidate_count(result),
        "provenance": result.provenance or LEXICAL,
        "coverage": result.coverage.to_wire(),
        "errors": [item.message for item in result.diagnostics],
    }


def _declared_candidate_count(payload: dict[str, Any], key: str) -> int:
    """Producer-declared total; a retained-sample limit must not shrink it."""
    declared = payload.get("candidate_count")
    if isinstance(declared, bool):
        declared = None
    if isinstance(declared, int) and declared >= 0:
        return declared
    candidates = payload.get(key)
    return len(candidates) if isinstance(candidates, list) else 0


def _query(
    provider: NavigationProvider,
    request: NavigationRequest,
    include_references: bool,
) -> ProviderResult[Any]:
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
    if not isinstance(payload, dict):
        return failed_result(
            provider.name,
            "provider returned a non-object payload",
            provenance=provider.provenance,
        )
    candidate_count = _declared_candidate_count(payload, provider.candidates_key)
    coverage = payload.get("coverage")
    messages = list(provider.result_errors(payload))
    # Preserve the full parse-error count even when detailed messages are
    # sampled to a bounded list: bounded diagnostics never imply only the
    # visible errors occurred.
    parse_total = payload.get("parse_error_count")
    visible_parses = payload.get("parse_errors")
    if (
        isinstance(parse_total, int)
        and isinstance(visible_parses, list)
        and parse_total > len(visible_parses)
    ):
        messages.append(
            f"{parse_total - len(visible_parses)} additional parse errors omitted "
            f"({parse_total} total)"
        )
    diagnostics = tuple(
        Diagnostic(
            message=message,
            code=(PARSE_ERROR if "parse" in message.lower() else PROVIDER_ERROR),
        )
        for message in messages
    )
    return ProviderResult(
        provider=provider.name,
        status=ProviderStatus.EMPTY if candidate_count == 0 else ProviderStatus.OK,
        payload=payload,
        provenance=provider.provenance,
        candidate_count=candidate_count,
        coverage=(
            typed_from_wire(coverage)
            if coverage is not None
            else typed_from_wire("unknown")
        ),
        diagnostics=diagnostics,
    )


class TypeScriptProvider:
    name = "typescript"
    provenance = SEMANTIC
    candidates_key = "candidates"

    def supports(self, request: NavigationRequest) -> bool:
        return request.lang in {None, "typescript"}

    @staticmethod
    def _runtime_available() -> bool:
        from .common import find_executable

        return find_executable("node") is not None

    def locate(self, request: NavigationRequest) -> dict[str, Any] | None:
        if not self._runtime_available():
            return None
        return ts_nav_data(
            request.root,
            "locate",
            None,
            None,
            None,
            request.limit,
            symbol=request.symbol,
            paths=list(request.paths),
            pick=request.pick,
        )

    def overview(self, request: NavigationRequest) -> dict[str, Any] | None:
        if not self._runtime_available():
            return None
        return ts_nav_data(
            request.root,
            "overview",
            None,
            None,
            None,
            request.limit,
            symbol=request.symbol,
            paths=list(request.paths),
            pick=request.pick,
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
            request.root,
            request.symbol,
            list(request.paths),
            request.limit,
            include_references=False,
        )

    def overview(self, request: NavigationRequest) -> dict[str, Any] | None:
        return python_symbol_overview(
            request.root,
            request.symbol,
            list(request.paths),
            request.limit,
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
            request.root,
            request.symbol,
            list(request.paths),
            mode="fixed",
            word=False,
            case="smart",
            limit=request.limit,
            per_file=8,
            context=request.context,
            max_chars=240,
            include_sensitive=False,
            view="auto",
            max_files=40,
        )

    def locate(self, request: NavigationRequest) -> dict[str, Any] | None:
        return self._search(request)

    def overview(self, request: NavigationRequest) -> dict[str, Any] | None:
        return self._search(request)

    def result_errors(self, result: dict[str, Any]) -> list[str]:
        return []


LANGUAGE_PROVIDERS: tuple[NavigationProvider, ...] = (
    TypeScriptProvider(),
    PythonProvider(),
)
LEXICAL_FALLBACK = LexicalFallbackProvider()


@dataclass
class SymbolResolution:
    outcomes: list[ProviderResult[Any]]
    fallback: ProviderResult[Any] | None = None

    def _all(self) -> list[ProviderResult[Any]]:
        return [*self.outcomes, *([self.fallback] if self.fallback else [])]

    def entries(self) -> list[dict[str, Any]]:
        return [_metadata(result) for result in self._all()]

    def coverage(self) -> dict[str, Any]:
        blocks = [
            result.coverage
            for result in self._all()
            if result.status is not ProviderStatus.NOT_APPLICABLE
        ]
        if not blocks:
            return typed_from_wire({"status": "unknown"}).to_wire()
        return merge_typed(*blocks).to_wire()

    def provenance(self) -> str:
        with_candidates = [
            result.provenance or LEXICAL
            for result in self._all()
            if _candidate_count(result)
        ]
        return best_provenance(*with_candidates) or LEXICAL

    def result(self, name: str) -> dict[str, Any] | None:
        for outcome in self.outcomes:
            if outcome.provider == name:
                payload = outcome.payload
                return payload if isinstance(payload, dict) else None
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
    outcomes = [
        _query(provider, request, include_references)
        for provider in LANGUAGE_PROVIDERS
        if provider.supports(request)
    ]
    resolution = SymbolResolution(outcomes=outcomes)
    if not any(_candidate_count(outcome) for outcome in outcomes):
        resolution.fallback = _query(LEXICAL_FALLBACK, request, include_references)
    return resolution
