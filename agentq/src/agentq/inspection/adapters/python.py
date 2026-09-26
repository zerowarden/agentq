"""Python capability adapter over stdlib-AST acquisition.

Declarations are syntax-aware AST definitions. Mentions are explicitly
syntactic name/attribute evidence and are never upgraded to semantic
references: they carry ``Binding.UNRESOLVED`` so scoring and assessment cannot
mistake them for binding-resolved use sites.
"""

from __future__ import annotations

import platform
from collections.abc import Callable, Sequence
from pathlib import Path

from agentq.core import (
    COMPLETE,
    PARSE_ERROR,
    PARTIAL,
    REFERENCE_LIMIT,
    RESULT_LIMIT,
    SAMPLED,
    SYNTACTIC,
    AgentQError,
    Coverage,
    Diagnostic,
    SourceRef,
    classify_path,
    typed_coverage,
)
from agentq.core.languages import language_id_for
from agentq.discovery import list_repo_files
from agentq.navigation import (
    PythonOverview,
    PythonReference,
    python_symbol_overview,
)
from agentq.syntax import OutlineSymbol

from ..contracts import (
    Binding,
    Capability,
    CapabilityAvailability,
    CapabilityResult,
    DeclarationCandidate,
    DeclarationPayload,
    EvidenceRequest,
    EvidenceVariant,
    Fidelity,
    InspectionContext,
    InspectionTarget,
    Observation,
    ObservationKind,
    ReferencePayload,
    RepresentationKind,
    SourceSpan,
    SourceVersion,
    make_observation,
    make_variant,
)
from ._shared import (
    PYTHON_LANGUAGES,
    FileLister,
    SourceCache,
    failed_result,
    scoped_languages,
    status_for,
    symbol_of,
    target_scopes,
    utf8_byte_column_to_code_points,
    with_skipped,
)

OverviewRunner = Callable[..., PythonOverview]


class PythonInspectionAdapter:
    """Capabilities backed by the stdlib Python AST acquisition."""

    name: str = "python"
    provenance: str = SYNTACTIC

    def __init__(
        self,
        *,
        overview: OverviewRunner = python_symbol_overview,
        lister: FileLister = list_repo_files,
    ) -> None:
        self._overview: OverviewRunner = overview
        self._lister: FileLister = lister

    def capabilities(self) -> frozenset[Capability]:
        return frozenset({Capability.FIND_DECLARATIONS, Capability.SYNTACTIC_MENTIONS})

    def batch_capabilities(self) -> frozenset[Capability]:
        return frozenset()

    def acquire_batch(
        self,
        requests: Sequence[EvidenceRequest],
        context: InspectionContext,
    ) -> tuple[CapabilityResult, ...]:
        return tuple(self.acquire(request, context) for request in requests)

    def applicable(
        self,
        target: InspectionTarget,
        context: InspectionContext,
        subject: DeclarationCandidate | None = None,
    ) -> bool:
        if subject is not None:
            language = language_id_for(subject.path)
            if language is not None:
                return language in PYTHON_LANGUAGES
        languages = self._target_languages(target, context)
        return languages is None or bool(languages & PYTHON_LANGUAGES)

    def availability(
        self,
        _capability: Capability,
        _target: InspectionTarget,
        _context: InspectionContext,
        _subject: DeclarationCandidate | None = None,
    ) -> CapabilityAvailability:
        return CapabilityAvailability(
            available=True, provider_version=platform.python_version()
        )

    def acquire(
        self, request: EvidenceRequest, context: InspectionContext
    ) -> CapabilityResult:
        symbol = symbol_of(request.target)
        if symbol is None:
            return failed_result(
                "Python evidence requires a symbol or candidate target",
                code=PARSE_ERROR,
            )
        scopes = target_scopes(request.target)
        cache = self._source_cache(context)
        if request.capability is Capability.FIND_DECLARATIONS:
            return self._declarations(request, context, symbol, scopes, cache)
        if request.capability is Capability.SYNTACTIC_MENTIONS:
            return self._mentions(
                request, context, symbol, request.scope or scopes, cache
            )
        return failed_result(
            f"unsupported capability: {request.capability.value}",
            code=PARSE_ERROR,
        )

    def _declarations(
        self,
        request: EvidenceRequest,
        context: InspectionContext,
        symbol: str,
        scopes: tuple[str, ...],
        cache: SourceCache,
    ) -> CapabilityResult:
        overview = self._overview(
            context.root, symbol, list(scopes), request.limit, include_references=False
        )
        observations: list[Observation] = []
        variants: list[EvidenceVariant] = []
        skipped = 0
        for item in overview.candidates:
            version = cache.version(item.file)
            if version is None:
                skipped += 1
                continue
            span = _definition_span(item, cache)
            signature = item.signature or item.name
            observation = make_observation(
                kind=ObservationKind.DECLARATION,
                payload=DeclarationPayload(
                    name=item.name,
                    kind=item.kind or "declaration",
                    signature=signature,
                    span=span,
                    scope=item.scope,
                ),
                source=SourceRef(
                    path=item.file,
                    start_line=span.start_line,
                    end_line=span.end_line,
                    symbol=item.name,
                ),
                source_versions=(SourceVersion(path=item.file, version=version),),
            )
            observations.append(observation)
            if signature:
                variants.append(
                    make_variant(
                        observation_id=observation.observation_id,
                        representation=RepresentationKind.SIGNATURE,
                        fidelity=Fidelity.SUMMARY,
                        source=observation.source,
                        text=signature,
                        span=span,
                    )
                )
        coverage = with_skipped(_declarations_coverage(overview), skipped)
        return CapabilityResult(
            status=status_for(tuple(observations), coverage),
            observations=tuple(observations),
            variants=tuple(variants),
            coverage=coverage,
            diagnostics=_parse_diagnostics(overview),
            provider_version=platform.python_version(),
            effective_scope=tuple(overview.paths) or tuple(request.scope),
        )

    def _mentions(
        self,
        request: EvidenceRequest,
        context: InspectionContext,
        symbol: str,
        scopes: tuple[str, ...],
        cache: SourceCache,
    ) -> CapabilityResult:
        overview = self._overview(
            context.root, symbol, list(scopes), request.limit, include_references=True
        )
        observations: list[Observation] = []
        variants: list[EvidenceVariant] = []
        skipped = 0
        for item in overview.references.results:
            item_domain = "test" if classify_path(item.path) == "test" else None
            if request.domain is not None and item_domain != request.domain:
                continue
            version = cache.version(item.path)
            if version is None:
                skipped += 1
                continue
            span = _reference_span(item, cache)
            text = item.preview
            observation = make_observation(
                kind=ObservationKind.SYNTACTIC_MENTION,
                payload=ReferencePayload(
                    relationship="syntactic_mention",
                    text=text,
                    binding=Binding.UNRESOLVED,
                    domain=item_domain,
                ),
                source=SourceRef(
                    path=item.path,
                    start_line=span.start_line,
                    end_line=span.end_line,
                    symbol=symbol,
                ),
                source_versions=(SourceVersion(path=item.path, version=version),),
            )
            observations.append(observation)
            variants.append(
                make_variant(
                    observation_id=observation.observation_id,
                    representation=RepresentationKind.REFERENCE,
                    fidelity=Fidelity.BOUNDED,
                    source=observation.source,
                    text=text,
                    span=span,
                )
            )
        coverage = with_skipped(_mentions_coverage(overview), skipped)
        return CapabilityResult(
            status=status_for(tuple(observations), coverage),
            observations=tuple(observations),
            variants=tuple(variants),
            coverage=coverage,
            diagnostics=_parse_diagnostics(overview),
            provider_version=platform.python_version(),
            effective_scope=tuple(overview.paths) or tuple(request.scope),
        )

    def _target_languages(
        self, target: InspectionTarget, context: InspectionContext
    ) -> frozenset[str] | None:
        scopes = target_scopes(target)
        if not scopes:
            return None
        return context.memo.get_or_create(
            ("languages", self, str(context.root), scopes),
            lambda: self._scan_languages(context.root, scopes),
        )

    def _scan_languages(
        self, root: Path, scopes: tuple[str, ...]
    ) -> frozenset[str] | None:
        try:
            return scoped_languages(root, scopes, lister=self._lister)
        except (AgentQError, OSError):
            return None

    def _source_cache(self, context: InspectionContext) -> SourceCache:
        return context.memo.get_or_create(
            ("sources", str(context.root)),
            lambda: SourceCache(root=context.root),
        )


def _declarations_coverage(overview: PythonOverview) -> Coverage:
    reasons = (
        [RESULT_LIMIT] if len(overview.candidates) < overview.candidate_count else []
    )
    if overview.parse_error_count:
        return typed_coverage(PARTIAL, PARSE_ERROR, *reasons)
    return typed_coverage(SAMPLED, *reasons) if reasons else typed_coverage(COMPLETE)


def _mentions_coverage(overview: PythonOverview) -> Coverage:
    reasons = [REFERENCE_LIMIT] if overview.references.truncated else []
    if overview.parse_error_count:
        return typed_coverage(PARTIAL, PARSE_ERROR, *reasons)
    return typed_coverage(SAMPLED, *reasons) if reasons else typed_coverage(COMPLETE)


def _parse_diagnostics(overview: PythonOverview) -> tuple[Diagnostic, ...]:
    diagnostics = [
        Diagnostic(
            message=f"{item.path}: {item.error}",
            code=PARSE_ERROR,
            path=item.path,
            severity="warning",
        )
        for item in overview.parse_errors
    ]
    if overview.parse_error_count > len(overview.parse_errors):
        diagnostics.append(
            Diagnostic(
                message=(
                    f"{overview.parse_error_count} files failed to parse; "
                    f"{len(overview.parse_errors)} are reported"
                ),
                code=PARSE_ERROR,
                severity="warning",
            )
        )
    return tuple(diagnostics)


def _definition_span(item: OutlineSymbol, cache: SourceCache) -> SourceSpan:
    start_line = max(1, item.line or 1)
    end_line = max(start_line, item.end_line or start_line)
    column = _python_column(item.file, start_line, item.column, cache)
    return SourceSpan(start_line=start_line, end_line=end_line, start_column=column)


def _reference_span(item: PythonReference, cache: SourceCache) -> SourceSpan:
    line = max(1, item.line)
    column = _python_column(item.path, line, item.column, cache)
    return SourceSpan(start_line=line, end_line=line, start_column=column)


def _python_column(
    path: str, line: int, column: int | None, cache: SourceCache
) -> int | None:
    """Convert the AST's one-based UTF-8 byte column to a code-point column."""
    if column is None:
        return None
    lines = cache.lines(path)
    if lines is None or not 1 <= line <= len(lines):
        return None
    return utf8_byte_column_to_code_points(lines[line - 1], max(0, column - 1))
