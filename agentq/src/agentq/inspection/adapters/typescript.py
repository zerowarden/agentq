"""TypeScript/JavaScript capability adapter over the language-service bridge.

The adapter normalizes bridge payloads into inspection contracts. It queries
validated exact locations for symbol-anchored evidence instead of reselecting a
candidate through a display index, and it can request a bounded batch of
operations in one bridge invocation. Project-context completeness is reported
from bridge metadata rather than assumed from an untruncated list.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path

from agentq.core import (
    COMPLETE,
    PROVIDER_ERROR,
    REFERENCE_LIMIT,
    RESULT_LIMIT,
    SAMPLED,
    SCAN_CAP,
    SEMANTIC,
    UNATTRIBUTED,
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
    TypeScriptBatch,
    TypeScriptBatchRequest,
    TypeScriptCandidateSearch,
    TypeScriptLocation,
    TypeScriptMeta,
    TypeScriptNav,
    TypeScriptNavRequest,
    TypeScriptOperation,
    TypeScriptProbe,
    ts_nav,
    ts_nav_batch,
    ts_nav_probe,
)

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
    LocationTarget,
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
    TS_JS_LANGUAGES,
    FileLister,
    SourceCache,
    code_point_column_to_utf16,
    failed_result,
    scoped_languages,
    status_for,
    symbol_of,
    target_scopes,
    utf16_column_to_code_points,
    with_skipped,
)

OPERATION_FOR_CAPABILITY = {
    Capability.SEMANTIC_REFERENCES: "references",
    Capability.IMPLEMENTATIONS: "implementations",
}
EXTERNAL_EXCLUDED = "external_excluded"


@dataclass(frozen=True)
class _Mapped:
    observations: tuple[Observation, ...] = ()
    variants: tuple[EvidenceVariant, ...] = ()
    diagnostics: tuple[Diagnostic, ...] = ()
    excluded: int = 0
    skipped: int = 0


class TypeScriptInspectionAdapter:
    """Capabilities backed by the TypeScript language-service bridge."""

    name: str = "typescript"
    provenance: str = SEMANTIC

    def __init__(
        self,
        *,
        locate: Callable[[TypeScriptNavRequest], TypeScriptNav] = ts_nav,
        batch: Callable[[TypeScriptBatchRequest], TypeScriptBatch] = ts_nav_batch,
        probe: Callable[[Path, tuple[str, ...]], TypeScriptProbe] = ts_nav_probe,
        lister: FileLister = list_repo_files,
    ) -> None:
        self._locate: Callable[[TypeScriptNavRequest], TypeScriptNav] = locate
        self._batch: Callable[[TypeScriptBatchRequest], TypeScriptBatch] = batch
        self._probe: Callable[[Path, tuple[str, ...]], TypeScriptProbe] = probe
        self._lister: FileLister = lister

    def capabilities(self) -> frozenset[Capability]:
        return frozenset(
            {
                Capability.FIND_DECLARATIONS,
                Capability.RESOLVE_LOCATION,
                Capability.SEMANTIC_REFERENCES,
                Capability.IMPLEMENTATIONS,
            }
        )

    def batch_capabilities(self) -> frozenset[Capability]:
        return frozenset(OPERATION_FOR_CAPABILITY)

    def applicable(
        self,
        target: InspectionTarget,
        context: InspectionContext,
        subject: DeclarationCandidate | None = None,
    ) -> bool:
        if subject is not None:
            language = language_id_for(subject.path)
            if language is not None:
                return language in TS_JS_LANGUAGES
        languages = self._target_languages(target, context)
        return languages is None or bool(languages & TS_JS_LANGUAGES)

    def availability(
        self,
        _capability: Capability,
        target: InspectionTarget,
        context: InspectionContext,
        subject: DeclarationCandidate | None = None,
    ) -> CapabilityAvailability:
        scopes = (subject.path,) if subject is not None else target_scopes(target)
        probe = self._probe_for(context, scopes)
        version = probe.meta.runtime.typescript if probe.meta is not None else None
        if not probe.available:
            return CapabilityAvailability(
                available=False,
                reason=probe.reason or "TypeScript runtime is unavailable",
                provider_version=version,
            )
        return CapabilityAvailability(available=True, provider_version=version)

    def acquire(
        self, request: EvidenceRequest, context: InspectionContext
    ) -> CapabilityResult:
        match request.capability:
            case Capability.FIND_DECLARATIONS:
                return self._find_declarations(request, context)
            case Capability.RESOLVE_LOCATION:
                return self._resolve_location(request, context)
            case Capability.SEMANTIC_REFERENCES | Capability.IMPLEMENTATIONS:
                return self._reference_evidence(
                    request, context, (request.capability,)
                )[0]
            case _:
                return failed_result(
                    f"unsupported capability: {request.capability.value}"
                )

    def acquire_batch(
        self,
        requests: Sequence[EvidenceRequest],
        context: InspectionContext,
    ) -> tuple[CapabilityResult, ...]:
        """One bridge invocation for several symbol-anchored operations."""
        if not requests or any(
            request.capability not in OPERATION_FOR_CAPABILITY for request in requests
        ):
            return tuple(self.acquire(request, context) for request in requests)
        first = requests[0]
        if any(request.subject != first.subject for request in requests):
            return tuple(self.acquire(request, context) for request in requests)
        return self._reference_evidence(
            first, context, tuple(request.capability for request in requests)
        )

    def _find_declarations(
        self, request: EvidenceRequest, context: InspectionContext
    ) -> CapabilityResult:
        symbol = symbol_of(request.target)
        if symbol is None:
            return failed_result(
                "find_declarations requires a symbol or candidate target"
            )
        nav = self._locate(
            TypeScriptNavRequest(
                root=context.root,
                action="locate",
                limit=request.limit,
                symbol=symbol,
                paths=target_scopes(request.target),
            )
        )
        if not isinstance(nav, TypeScriptCandidateSearch):
            return failed_result(
                "the TypeScript bridge returned an unexpected locate payload"
            )
        cache = self._source_cache(context)
        mapped = _map_evidence(nav.candidates, cache, DECLARATION_SHAPE)
        coverage = with_skipped(nav.coverage, mapped.skipped)
        return _result(
            mapped=mapped,
            coverage=coverage,
            meta=nav.meta,
            fallback_scope=request.scope,
        )

    def _resolve_location(
        self, request: EvidenceRequest, context: InspectionContext
    ) -> CapabilityResult:
        target = request.target
        if not isinstance(target, LocationTarget):
            return failed_result("resolve_location requires a location target")
        cache = self._source_cache(context)
        batch = self._batch(
            _batch_request(
                context.root,
                target.path,
                target.line,
                target.column,
                ("definition",),
                request.limit,
                cache,
            )
        )
        operation = batch.operation("definition")
        if operation is None or operation.failed:
            return failed_result(
                operation.error
                if operation is not None and operation.error
                else "the definition operation did not complete"
            )
        mapped = _map_evidence(operation.results, cache, DECLARATION_SHAPE)
        return _result(
            mapped=mapped,
            coverage=with_skipped(batch.coverage, mapped.skipped),
            meta=batch.meta,
            fallback_scope=request.scope,
        )

    def _reference_evidence(
        self,
        request: EvidenceRequest,
        context: InspectionContext,
        capabilities: tuple[Capability, ...],
    ) -> tuple[CapabilityResult, ...]:
        subject = request.subject
        if subject is None:
            return tuple(
                failed_result(
                    "semantic evidence requires a validated declaration subject"
                )
                for _ in capabilities
            )
        cache = self._source_cache(context)
        batch = self._batch(
            _batch_request(
                context.root,
                subject.path,
                subject.span.start_line,
                subject.span.start_column or 1,
                tuple(OPERATION_FOR_CAPABILITY[item] for item in capabilities),
                request.limit,
                cache,
            )
        )
        return tuple(
            self._operation_result(
                capability, batch, cache, request.scope, request.domain
            )
            for capability in capabilities
        )

    def _operation_result(
        self,
        capability: Capability,
        batch: TypeScriptBatch,
        cache: SourceCache,
        fallback_scope: tuple[str, ...],
        domain: str | None,
    ) -> CapabilityResult:
        operation = batch.operation(OPERATION_FOR_CAPABILITY[capability])
        if operation is None:
            return failed_result(
                f"the TypeScript bridge returned no {capability.value} operation"
            )
        if operation.failed:
            return failed_result(
                operation.error or f"{capability.value} did not complete"
            )
        match capability:
            case Capability.IMPLEMENTATIONS:
                shape = _EvidenceShape(
                    ObservationKind.IMPLEMENTATION, "implementation", domain
                )
            case _:
                shape = _EvidenceShape(
                    ObservationKind.SEMANTIC_REFERENCE, "reference", domain
                )
        mapped = _map_evidence(operation.results, cache, shape)
        return _result(
            mapped=mapped,
            coverage=with_skipped(
                _operation_coverage(operation, batch.meta), mapped.skipped
            ),
            meta=batch.meta,
            fallback_scope=fallback_scope,
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

    def _probe_for(
        self, context: InspectionContext, scopes: tuple[str, ...]
    ) -> TypeScriptProbe:
        return context.memo.get_or_create(
            ("probe", self, str(context.root), scopes),
            lambda: self._probe(context.root, scopes),
        )

    def _source_cache(self, context: InspectionContext) -> SourceCache:
        return context.memo.get_or_create(
            ("sources", str(context.root)),
            lambda: SourceCache(root=context.root),
        )


def _result(
    *,
    mapped: _Mapped,
    coverage: Coverage,
    meta: TypeScriptMeta | None,
    fallback_scope: tuple[str, ...],
) -> CapabilityResult:
    diagnostics = [*mapped.diagnostics]
    if meta is not None:
        diagnostics.extend(_meta_diagnostics(meta))
    if mapped.excluded:
        diagnostics.append(
            Diagnostic(
                message=(
                    f"{mapped.excluded} location(s) outside the repository were "
                    "excluded from repository evidence"
                ),
                code=EXTERNAL_EXCLUDED,
                severity="warning",
            )
        )
    version = meta.runtime.typescript if meta is not None else None
    return CapabilityResult(
        status=status_for(mapped.observations, coverage),
        observations=mapped.observations,
        variants=mapped.variants,
        coverage=coverage,
        diagnostics=tuple(diagnostics),
        provider_version=version,
        effective_scope=_effective_scope(meta, fallback_scope),
    )


def _operation_coverage(
    operation: TypeScriptOperation, meta: TypeScriptMeta | None
) -> Coverage:
    """One operation's own truncation, not the batch's aggregate outcome."""
    reasons: list[str] = []
    if operation.truncated:
        reasons.append(
            REFERENCE_LIMIT if operation.name == "references" else RESULT_LIMIT
        )
    if meta is not None and meta.discovery.truncated:
        reasons.append(SCAN_CAP)
    return typed_coverage(SAMPLED, *reasons) if reasons else typed_coverage(COMPLETE)


def _meta_diagnostics(meta: TypeScriptMeta) -> tuple[Diagnostic, ...]:
    return tuple(
        Diagnostic(message=error, code=PROVIDER_ERROR, severity="warning")
        for error in meta.discovery.errors
    )


def _effective_scope(
    meta: TypeScriptMeta | None, fallback: tuple[str, ...]
) -> tuple[str, ...]:
    if meta is not None and meta.project is not None and meta.project.root_dir:
        return (meta.project.root_dir,)
    return tuple(fallback)


def _batch_request(
    root: Path,
    path: str,
    line: int,
    column: int,
    operations: tuple[str, ...],
    limit: int,
    cache: SourceCache,
) -> TypeScriptBatchRequest:
    converted = column
    lines = cache.lines(path)
    if lines is not None and 1 <= line <= len(lines):
        converted = code_point_column_to_utf16(lines[line - 1], column)
    return TypeScriptBatchRequest(
        root=root,
        file=path,
        line=line,
        column=converted,
        operations=operations,
        limit=limit,
    )


def _declaration_span(location: TypeScriptLocation) -> SourceSpan | None:
    """The provider's full declaration extent, when it reported one."""
    span = location.declaration_span
    if span is None or span.start_line < 1 or span.end_line < span.start_line:
        return None
    return SourceSpan(start_line=span.start_line, end_line=span.end_line)


def _span_for(location: TypeScriptLocation, cache: SourceCache) -> SourceSpan:
    lines = cache.lines(location.path)
    if lines is None:
        return SourceSpan(
            start_line=max(1, location.line),
            end_line=max(max(1, location.line), location.end_line),
        )
    start_line = max(1, min(location.line, len(lines)))
    end_line = max(start_line, min(location.end_line or start_line, len(lines)))
    start_column = utf16_column_to_code_points(lines[start_line - 1], location.column)
    end_column = (
        utf16_column_to_code_points(lines[end_line - 1], location.end_column)
        if location.end_column
        else None
    )
    if end_column is not None and end_line == start_line and end_column <= start_column:
        end_column = None
    return SourceSpan(
        start_line=start_line,
        end_line=end_line,
        start_column=start_column,
        end_column=end_column,
    )


@dataclass(frozen=True)
class _EvidenceShape:
    """How one provider location becomes an observation and its variant."""

    kind: ObservationKind
    relationship: str | None = None
    domain: str | None = None


DECLARATION_SHAPE = _EvidenceShape(kind=ObservationKind.DECLARATION)


def _map_evidence(
    locations: tuple[TypeScriptLocation, ...],
    cache: SourceCache,
    shape: _EvidenceShape,
) -> _Mapped:
    observations: list[Observation] = []
    variants: list[EvidenceVariant] = []
    diagnostics: list[Diagnostic] = []
    excluded = 0
    skipped = 0
    seen: set[tuple[str, int, int]] = set()
    for item in locations:
        if item.external:
            excluded += 1
            continue
        item_domain = "test" if classify_path(item.path) == "test" else None
        if shape.domain is not None and item_domain != shape.domain:
            continue
        key = (item.path, item.line, item.column)
        if key in seen:
            continue
        seen.add(key)
        if not item.path:
            skipped += 1
            continue
        version = cache.version(item.path)
        if version is None:
            skipped += 1
            diagnostics.append(
                Diagnostic(
                    message=(
                        f"{shape.kind.value} location could not be versioned "
                        "from repository source"
                    ),
                    code=UNATTRIBUTED,
                    path=item.path,
                    severity="warning",
                )
            )
            continue
        span = _span_for(item, cache)
        observation = _shaped_observation(item, span, version, shape, item_domain)
        observations.append(observation)
        variant = _shaped_variant(item, span, observation, shape)
        if variant is not None:
            variants.append(variant)
    return _Mapped(
        observations=tuple(observations),
        variants=tuple(variants),
        diagnostics=tuple(diagnostics),
        excluded=excluded,
        skipped=skipped,
    )


def _shaped_observation(
    item: TypeScriptLocation,
    span: SourceSpan,
    version: str,
    shape: _EvidenceShape,
    item_domain: str | None,
) -> Observation:
    source = SourceRef(
        path=item.path,
        start_line=span.start_line,
        end_line=span.end_line,
        symbol=item.name or item.display,
    )
    versions = (SourceVersion(path=item.path, version=version),)
    match shape.kind:
        case ObservationKind.DECLARATION:
            return make_observation(
                kind=ObservationKind.DECLARATION,
                payload=DeclarationPayload(
                    name=item.name or item.display or "",
                    kind=item.kind or "declaration",
                    signature=item.preview or item.name or item.display or "",
                    span=span,
                    scope=item.container or None,
                    declaration_span=_declaration_span(item),
                ),
                source=source,
                source_versions=versions,
            )
        case _:
            return make_observation(
                kind=shape.kind,
                payload=ReferencePayload(
                    relationship=shape.relationship or shape.kind.value,
                    text=item.preview or item.display or item.name or "",
                    binding=Binding.RESOLVED,
                    domain=item_domain,
                    configuration=item.config,
                ),
                source=source,
                source_versions=versions,
            )


def _shaped_variant(
    item: TypeScriptLocation,
    span: SourceSpan,
    observation: Observation,
    shape: _EvidenceShape,
) -> EvidenceVariant | None:
    match shape.kind:
        case ObservationKind.DECLARATION:
            signature = item.preview or item.name or item.display or ""
            if not signature:
                return None
            return make_variant(
                observation_id=observation.observation_id,
                representation=RepresentationKind.SIGNATURE,
                fidelity=Fidelity.SUMMARY,
                source=observation.source,
                text=signature,
                span=span,
            )
        case _:
            return make_variant(
                observation_id=observation.observation_id,
                representation=RepresentationKind.REFERENCE,
                fidelity=Fidelity.BOUNDED,
                source=observation.source,
                text=item.preview or item.display or item.name or "",
                span=span,
            )
