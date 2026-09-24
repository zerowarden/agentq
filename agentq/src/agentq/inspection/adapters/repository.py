"""Language-agnostic repository capability adapter.

Wraps the existing read, outline, lexical search, and ownership primitives so
every language adapter can delegate source content, structure, mentions, and
package ownership to one place. It makes no semantic declaration or reference
claims: its evidence is source text, structure, lexical mentions, and manifests.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass, replace

from agentq.core import (
    COMPLETE,
    LINE_CAP,
    PARSE_ERROR,
    PARTIAL,
    PROVIDER_ERROR,
    RESULT_LIMIT,
    Diagnostic,
    SourceRef,
    typed_coverage,
)
from agentq.core.languages import ecosystem_for_language, language_id_for
from agentq.discovery import (
    OutlineRequest,
    OutlineResult,
    PackageManifest,
    ReadItem,
    ReadRequest,
    ReadResult,
    SearchHit,
    SearchRequest,
    SearchResult,
    outline,
    read,
    search,
)
from agentq.tooling import find_executable
from agentq.workspace import nearest_manifest

from ..contracts import (
    CandidateTarget,
    Capability,
    CapabilityAvailability,
    CapabilityResult,
    CollectionStatus,
    DeclarationCandidate,
    EvidenceRequest,
    EvidenceVariant,
    Fidelity,
    InspectionContext,
    InspectionTarget,
    LocationTarget,
    MentionPayload,
    Observation,
    ObservationKind,
    OutlinePayload,
    OutlineSymbolRef,
    PackagePayload,
    PathTarget,
    RangeTarget,
    RepresentationKind,
    SourceSpan,
    SourceVersion,
    SourceWindowPayload,
    SymbolTarget,
    make_observation,
    make_variant,
)
from ._shared import SourceCache, failed_result, status_for

LEXICAL_ROLE = "test"


class RepositoryInspectionAdapter:
    """Capabilities backed by repository reads, outlines, search, and manifests."""

    name: str = "repository"

    def __init__(
        self,
        *,
        reader: Callable[[ReadRequest], ReadResult] = read,
        outliner: Callable[[OutlineRequest], OutlineResult] = outline,
        searcher: Callable[[SearchRequest], SearchResult] = search,
        manifest: Callable[..., PackageManifest | None] = nearest_manifest,
        which: Callable[[str], str | None] = find_executable,
    ) -> None:
        self._reader: Callable[[ReadRequest], ReadResult] = reader
        self._outliner: Callable[[OutlineRequest], OutlineResult] = outliner
        self._searcher: Callable[[SearchRequest], SearchResult] = searcher
        self._manifest: Callable[..., PackageManifest | None] = manifest
        self._which: Callable[[str], str | None] = which

    def capabilities(self) -> frozenset[Capability]:
        return frozenset(
            {
                Capability.READ_SOURCE,
                Capability.OUTLINE,
                Capability.LEXICAL_MENTIONS,
                Capability.OWNING_PACKAGE,
            }
        )

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
        _target: InspectionTarget,
        _context: InspectionContext,
        _subject: DeclarationCandidate | None = None,
    ) -> bool:
        return True

    def availability(
        self,
        capability: Capability,
        _target: InspectionTarget,
        _context: InspectionContext,
        _subject: DeclarationCandidate | None = None,
    ) -> CapabilityAvailability:
        if capability is Capability.LEXICAL_MENTIONS and self._which("rg") is None:
            return CapabilityAvailability(
                available=False,
                reason="ripgrep is required for lexical repository search",
            )
        return CapabilityAvailability(available=True)

    def acquire(
        self, request: EvidenceRequest, context: InspectionContext
    ) -> CapabilityResult:
        match request.capability:
            case Capability.READ_SOURCE:
                return self._read_source(request, context)
            case Capability.OUTLINE:
                return self._outline(request, context)
            case Capability.LEXICAL_MENTIONS:
                return self._lexical_mentions(request, context)
            case Capability.OWNING_PACKAGE:
                return self._owning_package(request, context)
            case _:
                return failed_result(
                    f"unsupported repository capability: {request.capability.value}"
                )

    # -- source ------------------------------------------------------------

    def _read_source(
        self, request: EvidenceRequest, context: InspectionContext
    ) -> CapabilityResult:
        view = _target_view(request.target)
        path, ranges = view.path, view.ranges
        if path is None:
            return failed_result(
                "read_source requires a path, location, or range target"
            )
        result = self._reader(
            ReadRequest(
                root=context.root,
                specs=(path,),
                line_ranges=tuple(ranges),
                context=0,
                max_lines=max(1, request.limit),
                max_chars=context.limits.max_source_line_chars,
                include_sensitive=False,
                budget=0,
                output_format="text",
            )
        )
        observations: list[Observation] = []
        variants: list[EvidenceVariant] = []
        diagnostics: list[Diagnostic] = []
        refused = False
        exact = True
        for item in result.items:
            if item.refused:
                refused = True
                diagnostics.append(
                    Diagnostic(
                        message=item.reason or f"source read refused for {path}",
                        code=PROVIDER_ERROR,
                        path=path,
                        severity="error",
                    )
                )
                continue
            mapped = _source_observation(item, path, ranges)
            if mapped is None:
                continue
            observation, variant = mapped
            observations.append(observation)
            variants.append(variant)
            if variant.fidelity is not Fidelity.EXACT:
                exact = False
        if refused:
            return CapabilityResult(
                status=CollectionStatus.FAILED,
                coverage=typed_coverage(PARTIAL, PROVIDER_ERROR),
                diagnostics=tuple(diagnostics),
                effective_scope=(path,),
            )
        if not observations:
            return CapabilityResult(
                status=CollectionStatus.EMPTY,
                coverage=typed_coverage(COMPLETE),
                effective_scope=(path,),
            )
        coverage = result.coverage
        if not exact and coverage.is_complete():
            coverage = typed_coverage(PARTIAL, RESULT_LIMIT)
        if not coverage.is_complete():
            diagnostics.append(
                Diagnostic(
                    message=f"source read for {path} was truncated by read limits",
                    code=LINE_CAP,
                    path=path,
                    severity="warning",
                )
            )
        return CapabilityResult(
            status=CollectionStatus.COMPLETED,
            observations=tuple(observations),
            variants=tuple(variants),
            coverage=coverage,
            diagnostics=tuple(diagnostics),
            effective_scope=(path,),
        )

    # -- outline -----------------------------------------------------------

    def _outline(
        self, request: EvidenceRequest, context: InspectionContext
    ) -> CapabilityResult:
        view = _target_view(request.target)
        paths = (view.path,) if view.path else tuple(request.scope)
        if not paths:
            return failed_result("outline requires a path target or an explicit scope")
        result = self._outliner(
            OutlineRequest(root=context.root, paths=paths, limit=request.limit)
        )
        symbols = tuple(
            OutlineSymbolRef(
                name=item.name,
                kind=item.kind or "symbol",
                span=SourceSpan(
                    start_line=max(1, item.line or 1),
                    end_line=max(
                        max(1, item.line or 1), item.end_line or item.line or 1
                    ),
                ),
            )
            for item in result.symbols
        )
        text = _outline_text(result, symbols)
        observation = make_observation(
            kind=ObservationKind.OUTLINE,
            payload=OutlinePayload(symbols=symbols, truncated=result.truncated),
            source=SourceRef(
                path=paths[0] if len(paths) == 1 else None, symbol=paths[0]
            ),
        )
        variant = make_variant(
            observation_id=observation.observation_id,
            representation=RepresentationKind.OUTLINE,
            fidelity=Fidelity.SUMMARY,
            source=observation.source,
            text=text,
        )
        diagnostics = tuple(
            Diagnostic(
                message=f"{item.path}: {item.error}",
                code=PARSE_ERROR,
                path=item.path,
                severity="warning",
            )
            for item in result.parse_errors
        )
        coverage = result.coverage
        return CapabilityResult(
            status=CollectionStatus.COMPLETED,
            observations=(observation,),
            variants=(variant,),
            coverage=coverage,
            diagnostics=diagnostics,
            effective_scope=paths,
        )

    # -- lexical mentions --------------------------------------------------

    def _lexical_mentions(
        self, request: EvidenceRequest, context: InspectionContext
    ) -> CapabilityResult:
        view = _target_view(request.target)
        if not view.symbol:
            return failed_result(
                "lexical mentions require a symbol or candidate target"
            )
        domain = "test" if request.domain == "test" else None
        scopes = tuple(request.scope) or view.scopes
        result = self._searcher(
            SearchRequest(
                root=context.root,
                query=view.symbol,
                scopes=scopes,
                mode="fixed",
                word=True,
                limit=request.limit,
                per_file=4,
                context=0,
                max_chars=240,
                include_sensitive=False,
                view="auto",
                max_files=12,
                roles=(LEXICAL_ROLE,) if domain == "test" else (),
            )
        )
        cache = self._source_cache(context)
        observations: list[Observation] = []
        variants: list[EvidenceVariant] = []
        kind = (
            ObservationKind.TEST_MENTION
            if domain == "test"
            else ObservationKind.LEXICAL_MENTION
        )
        for hit in result.hits:
            mapped = _mention_observation(hit, kind, domain, cache)
            if mapped is None:
                continue
            observation, variant = mapped
            observations.append(observation)
            variants.append(variant)
        coverage = result.coverage
        if domain == "test":
            coverage = replace(
                coverage, domain="lexical_test_mentions", scope="path_role:test"
            )
        return CapabilityResult(
            status=status_for(tuple(observations), coverage),
            observations=tuple(observations),
            variants=tuple(variants),
            coverage=coverage,
            diagnostics=(),
            effective_scope=scopes,
        )

    # -- ownership ---------------------------------------------------------

    def _owning_package(
        self, request: EvidenceRequest, context: InspectionContext
    ) -> CapabilityResult:
        view = _target_view(request.target)
        path = request.subject.path if request.subject is not None else view.path
        if path is None:
            return failed_result("owning_package requires a file or declaration path")
        ecosystem = ecosystem_for_language(language_id_for(path))
        manifest = self._manifest(
            context.root, context.root / path, ecosystem=ecosystem
        )
        if manifest is None:
            return CapabilityResult(
                status=CollectionStatus.EMPTY,
                coverage=typed_coverage(COMPLETE),
                effective_scope=(path,),
            )
        cache = self._source_cache(context)
        version = cache.version(manifest.path)
        observation = make_observation(
            kind=ObservationKind.OWNING_PACKAGE,
            payload=PackagePayload(
                path=manifest.path,
                kind=manifest.kind,
                name=manifest.name,
                scripts=tuple(manifest.scripts or ()),
            ),
            source=SourceRef(path=manifest.path),
            source_versions=(
                (SourceVersion(path=manifest.path, version=version),)
                if version is not None
                else ()
            ),
        )
        variant = make_variant(
            observation_id=observation.observation_id,
            representation=RepresentationKind.PACKAGE,
            fidelity=Fidelity.SUMMARY,
            source=observation.source,
            text=_package_text(manifest),
        )
        return CapabilityResult(
            status=CollectionStatus.COMPLETED,
            observations=(observation,),
            variants=(variant,),
            coverage=typed_coverage(COMPLETE),
            effective_scope=(path,),
        )

    def _source_cache(self, context: InspectionContext) -> SourceCache:
        return context.memo.get_or_create(
            ("sources", str(context.root)),
            lambda: SourceCache(root=context.root),
        )


# ---------------------------------------------------------------------------
# Mapping helpers
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _TargetView:
    """One target projected into the shapes repository capabilities need."""

    path: str | None = None
    ranges: tuple[tuple[int, int], ...] = ()
    scopes: tuple[str, ...] = ()
    symbol: str | None = None


def _target_view(target: InspectionTarget) -> _TargetView:
    match target:
        case SymbolTarget():
            return _TargetView(scopes=target.scopes, symbol=target.name)
        case CandidateTarget():
            return _TargetView(scopes=target.scopes, symbol=target.symbol)
        case RangeTarget():
            return _TargetView(
                path=target.path,
                ranges=tuple(
                    (span.start_line, span.end_line) for span in target.ranges
                ),
                scopes=(target.path,),
            )
        case PathTarget() | LocationTarget():
            return _TargetView(path=target.path, scopes=(target.path,))


def _read_ranges_covered(item: ReadItem, ranges: tuple[tuple[int, int], ...]) -> bool:
    """Full coverage and full fidelity: a compacted line is not exact source."""
    if item.truncated or any(line.truncated for line in item.lines):
        return False
    if not ranges:
        return True
    return all(item.start <= start and item.end >= end for start, end in ranges)


def _source_observation(
    item: ReadItem,
    path: str,
    ranges: tuple[tuple[int, int], ...],
) -> tuple[Observation, EvidenceVariant] | None:
    if not item.lines:
        return None
    text = "\n".join(line.text for line in item.lines)
    start_line = item.lines[0].line
    end_line = item.lines[-1].line
    span = SourceSpan(start_line=start_line, end_line=max(start_line, end_line))
    exact = _read_ranges_covered(item, ranges)
    observation = make_observation(
        kind=ObservationKind.SOURCE_WINDOW,
        payload=SourceWindowPayload(text=text, span=span, truncated=not exact),
        source=SourceRef(path=path, start_line=start_line, end_line=end_line),
        source_versions=(
            (SourceVersion(path=path, version=item.version),)
            if item.version is not None
            else ()
        ),
    )
    variant = make_variant(
        observation_id=observation.observation_id,
        representation=RepresentationKind.EXACT_SOURCE,
        fidelity=Fidelity.EXACT if exact else Fidelity.BOUNDED,
        source=observation.source,
        text=text,
        span=span,
    )
    return observation, variant


def _mention_observation(
    hit: SearchHit,
    kind: ObservationKind,
    domain: str | None,
    cache: SourceCache,
) -> tuple[Observation, EvidenceVariant] | None:
    if not hit.path:
        return None
    version = cache.version(hit.path)
    span = SourceSpan(
        start_line=max(1, hit.line),
        end_line=max(1, hit.line),
        start_column=max(1, hit.column),
    )
    observation = make_observation(
        kind=kind,
        payload=MentionPayload(text=hit.text, domain=domain),
        source=SourceRef(
            path=hit.path,
            start_line=span.start_line,
            end_line=span.end_line,
            symbol=hit.declared_symbol,
        ),
        source_versions=(
            (SourceVersion(path=hit.path, version=version),)
            if version is not None
            else ()
        ),
    )
    variant = make_variant(
        observation_id=observation.observation_id,
        representation=RepresentationKind.REFERENCE,
        fidelity=Fidelity.BOUNDED,
        source=observation.source,
        text=hit.text,
        span=span,
    )
    return observation, variant


def _outline_text(result: OutlineResult, symbols: tuple[OutlineSymbolRef, ...]) -> str:
    if result.lines:
        return "\n".join(result.lines)
    lines = [
        f"{symbol.kind} {symbol.name} ({symbol.span.start_line}-{symbol.span.end_line})"
        for symbol in symbols
    ]
    if result.engine:
        lines.insert(0, f"engine: {result.engine}")
    return "\n".join(lines)


def _package_text(manifest: PackageManifest) -> str:
    name = manifest.name or manifest.path
    return f"{manifest.kind} package {name} ({manifest.path})"
