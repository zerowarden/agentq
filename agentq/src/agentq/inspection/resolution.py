"""Conservative target resolution.

Resolution either selects exactly one declaration (or accepts a direct
target), reports explicitly named ambiguity, or returns an unresolved result
with a reason. It never reinterprets a lexical hit as a resolved declaration.

Candidate identity is request-bound: a candidate id is a deterministic
selector over the declaration's provider, path, source version, span, and
signature. Revalidating the id during a later resolution detects staleness.
"""

from __future__ import annotations

from dataclasses import dataclass

from agentq.core import (
    PARTIAL,
    PROVIDER_ERROR,
    PROVIDER_UNAVAILABLE,
    SOURCE_UNSTABLE,
    UNKNOWN,
    Coverage,
    Diagnostic,
    merge_typed,
    typed_coverage,
)
from agentq.core.evidence import EXACT, LOWER_BOUND

from .capabilities import Capability
from .contracts import (
    AcquiredEvidence,
    AcquisitionRecord,
    AmbiguousTarget,
    CandidateTarget,
    CapabilityReport,
    CollectionStatus,
    DeclarationCandidate,
    DeclarationPayload,
    EvidenceRequest,
    InspectionContext,
    InspectionRequest,
    InspectionTarget,
    LocationTarget,
    ObservationKind,
    PathTarget,
    RangeTarget,
    ResolutionResult,
    ResolvedTarget,
    SelectionMethod,
    UnresolvedReason,
    UnresolvedTarget,
    make_declaration_candidate,
)
from .lifecycle import candidate_is_current

SUCCESSFUL = frozenset({CollectionStatus.COMPLETED, CollectionStatus.EMPTY})


def resolve(
    request: InspectionRequest,
    capabilities: CapabilityReport,
    context: InspectionContext,
) -> ResolutionResult:
    """Resolve one request against the applicable capabilities."""
    target = request.target
    if isinstance(target, (PathTarget, RangeTarget)):
        return ResolvedTarget(
            target=target,
            method=SelectionMethod.DIRECT_TARGET,
            candidate_coverage=typed_coverage(UNKNOWN),
        )
    if isinstance(target, LocationTarget):
        return _availability_blocker(
            target,
            capabilities,
            Capability.RESOLVE_LOCATION,
            message=(
                "location resolution is unavailable; resolve the entity by "
                "symbol or use an explicit source range"
            ),
        ) or _resolve_location(request, target, context)
    if isinstance(target, CandidateTarget):
        return _availability_blocker(
            target, capabilities, Capability.FIND_DECLARATIONS
        ) or _resolve_candidate(request, target, context)
    return _availability_blocker(
        target, capabilities, Capability.FIND_DECLARATIONS
    ) or _resolve_candidates(request, target, target.scopes, context)


def _availability_blocker(
    target: InspectionTarget,
    capabilities: CapabilityReport,
    capability: Capability,
    *,
    message: str | None = None,
) -> UnresolvedTarget | None:
    if capabilities.available(capability):
        return None
    diagnostics: list[Diagnostic] = []
    if message is not None:
        diagnostics.append(Diagnostic(message=message, code=PROVIDER_UNAVAILABLE))
    diagnostics.extend(_gap_diagnostics(capabilities, capability))
    return UnresolvedTarget(
        target=target,
        reason=UnresolvedReason.UNAVAILABLE,
        diagnostics=tuple(diagnostics),
    )


def _gap_diagnostics(
    capabilities: CapabilityReport, capability: Capability
) -> tuple[Diagnostic, ...]:
    gap = capabilities.gap_for(capability)
    if gap is None:
        return (
            Diagnostic(
                message=f"capability {capability.value} is unavailable for this request",
                code=PROVIDER_UNAVAILABLE,
            ),
        )
    return (
        Diagnostic(
            message=f"{capability.value} is {gap.status.value}: {gap.reason}",
            code=PROVIDER_UNAVAILABLE,
        ),
    )


def _acquire(
    capability: Capability,
    target: InspectionTarget,
    scope: tuple[str, ...],
    request: InspectionRequest,
    context: InspectionContext,
) -> tuple[AcquiredEvidence, ...]:
    if context.registry is None:
        return ()

    return context.registry.acquire(
        EvidenceRequest(
            request_id=f"{request.request_id}:{capability.value}",
            capability=capability,
            target=target,
            scope=scope,
            limit=context.limits.limit_for(capability),
        ),
        context,
    )


@dataclass(frozen=True)
class _Declarations:
    """One declaration acquisition parsed into selectable candidates."""

    acquisitions: tuple[AcquiredEvidence, ...]
    candidates: tuple[DeclarationCandidate, ...]
    diagnostics: tuple[Diagnostic, ...]
    coverage: Coverage
    blocker: UnresolvedReason | None
    skipped: bool


def _read_declarations(
    acquisitions: tuple[AcquiredEvidence, ...],
) -> _Declarations:
    records = tuple(acquired.record for acquired in acquisitions)
    candidates, skipped_diagnostics, skipped = _collect_candidates(acquisitions)
    return _Declarations(
        acquisitions=acquisitions,
        candidates=candidates,
        diagnostics=_diagnostics_from(acquisitions, skipped_diagnostics),
        coverage=_candidate_coverage(acquisitions, skipped=skipped),
        blocker=_blocking_reason(records),
        skipped=skipped,
    )


def _terminal_unresolved(
    target: InspectionTarget, declarations: _Declarations
) -> UnresolvedTarget | None:
    """Failures and unattributed evidence end resolution before selection."""
    reason: UnresolvedReason | None = declarations.blocker
    if reason is None and declarations.skipped:
        reason = UnresolvedReason.INCOMPLETE
    if reason is None:
        return None
    return UnresolvedTarget(
        target=target,
        reason=reason,
        diagnostics=declarations.diagnostics,
        candidate_coverage=declarations.coverage,
    )


def _collect_candidates(
    acquisitions: tuple[AcquiredEvidence, ...],
) -> tuple[tuple[DeclarationCandidate, ...], tuple[Diagnostic, ...], bool]:
    """Canonical candidates, attribution diagnostics, and whether any were skipped."""
    candidates: list[DeclarationCandidate] = []
    diagnostics: list[Diagnostic] = []
    skipped = False
    seen: set[str] = set()
    for acquired in acquisitions:
        for observation in acquired.observations:
            if observation.kind is not ObservationKind.DECLARATION:
                continue
            payload = observation.payload
            if not isinstance(payload, DeclarationPayload):
                continue
            path = observation.source.path
            version = observation.version_of(path) if path is not None else None
            if path is None or version is None:
                skipped = True
                diagnostics.append(
                    Diagnostic(
                        message=(
                            "declaration observation carried no source version and "
                            "cannot be selected"
                        ),
                        code="unattributed",
                        path=path,
                    )
                )
                continue
            candidate = make_declaration_candidate(
                provider=acquired.record.provider,
                path=path,
                source_version=version,
                kind=payload.kind,
                span=payload.span,
                signature=payload.signature,
                scope=payload.scope,
            )
            if candidate.candidate_id in seen:
                continue
            seen.add(candidate.candidate_id)
            candidates.append(candidate)
    return tuple(candidates), tuple(diagnostics), skipped


def _candidate_coverage(
    acquisitions: tuple[AcquiredEvidence, ...], *, skipped: bool
) -> Coverage:
    if not acquisitions:
        return Coverage()
    records = tuple(acquired.record for acquired in acquisitions)
    merged = merge_typed(*(record.coverage for record in records))
    if skipped:
        return merged.with_failure("unattributed", status=PARTIAL)
    return merged


def _blocking_reason(
    records: tuple[AcquisitionRecord, ...],
) -> UnresolvedReason | None:
    """Why no candidate list can be considered complete, if it cannot."""
    if not records:
        return UnresolvedReason.UNAVAILABLE
    if any(record.status is CollectionStatus.FAILED for record in records):
        return UnresolvedReason.FAILED
    if any(record.status is CollectionStatus.UNAVAILABLE for record in records):
        return UnresolvedReason.UNAVAILABLE
    if any(record.status not in SUCCESSFUL for record in records):
        return UnresolvedReason.INCOMPLETE
    if not all(record.coverage.is_complete() for record in records):
        return UnresolvedReason.INCOMPLETE
    return None


def _diagnostics_from(
    acquisitions: tuple[AcquiredEvidence, ...],
    extra: tuple[Diagnostic, ...] = (),
) -> tuple[Diagnostic, ...]:
    found: list[Diagnostic] = list(extra)
    for acquired in acquisitions:
        found.extend(acquired.record.diagnostics)
    return tuple(found)


def _stale(
    target: InspectionTarget,
    candidate: DeclarationCandidate,
    declarations: _Declarations,
    message: str,
) -> UnresolvedTarget:
    return UnresolvedTarget(
        target=target,
        reason=UnresolvedReason.STALE_CANDIDATE,
        diagnostics=(
            *_diagnostics_from(declarations.acquisitions, declarations.diagnostics),
            Diagnostic(message=message, code=SOURCE_UNSTABLE, path=candidate.path),
        ),
        candidate_coverage=declarations.coverage,
    )


def _resolve_candidates(
    request: InspectionRequest,
    target: InspectionTarget,
    scope: tuple[str, ...],
    context: InspectionContext,
) -> ResolutionResult:
    declarations = _read_declarations(
        _acquire(Capability.FIND_DECLARATIONS, target, scope, request, context)
    )
    terminal = _terminal_unresolved(target, declarations)
    if terminal is not None:
        return terminal
    if len(declarations.candidates) == 0:
        return UnresolvedTarget(
            target=target,
            reason=UnresolvedReason.NOT_FOUND,
            diagnostics=declarations.diagnostics,
            candidate_coverage=declarations.coverage,
        )
    if len(declarations.candidates) > 1:
        return AmbiguousTarget(
            target=target,
            candidates=declarations.candidates,
            candidate_total=len(declarations.candidates),
            count_quality=_count_quality(declarations.coverage),
            candidate_coverage=declarations.coverage,
        )
    candidate = declarations.candidates[0]
    if context.source_versions is not None and not candidate_is_current(
        candidate, context.source_versions
    ):
        return _stale(
            target,
            candidate,
            declarations,
            "the selected declaration no longer matches the source version it "
            "was observed at",
        )
    return ResolvedTarget(
        target=target,
        method=SelectionMethod.UNIQUE_CANDIDATE,
        declaration=candidate,
        candidate_coverage=declarations.coverage,
        candidate_evidence=declarations.acquisitions,
    )


def _count_quality(coverage: Coverage) -> str:
    return EXACT if coverage.is_complete() else LOWER_BOUND


def _resolve_candidate(
    request: InspectionRequest,
    target: CandidateTarget,
    context: InspectionContext,
) -> ResolutionResult:
    declarations = _read_declarations(
        _acquire(Capability.FIND_DECLARATIONS, target, target.scopes, request, context)
    )
    terminal = _terminal_unresolved(target, declarations)
    if terminal is not None:
        return terminal
    selected = next(
        (
            candidate
            for candidate in declarations.candidates
            if candidate.candidate_id == target.candidate_id
        ),
        None,
    )
    if selected is None:
        reason = (
            UnresolvedReason.STALE_CANDIDATE
            if declarations.candidates
            else UnresolvedReason.NOT_FOUND
        )
        detail = (
            f"candidate {target.candidate_id} is not among the declarations "
            "re-acquired for this request; the declaration may have changed"
            if declarations.candidates
            else "no declaration was re-acquired for this request"
        )
        return UnresolvedTarget(
            target=target,
            reason=reason,
            diagnostics=(
                *declarations.diagnostics,
                Diagnostic(message=detail, code=SOURCE_UNSTABLE),
            ),
            candidate_coverage=declarations.coverage,
        )
    if context.source_versions is not None and not candidate_is_current(
        selected, context.source_versions
    ):
        return _stale(
            target,
            selected,
            declarations,
            "the explicitly selected candidate no longer matches its recorded "
            "source version",
        )
    return ResolvedTarget(
        target=target,
        method=SelectionMethod.EXPLICIT_CANDIDATE,
        declaration=selected,
        candidate_coverage=declarations.coverage,
        candidate_evidence=declarations.acquisitions,
    )


def _resolve_location(
    request: InspectionRequest,
    target: LocationTarget,
    context: InspectionContext,
) -> ResolutionResult:
    declarations = _read_declarations(
        _acquire(Capability.RESOLVE_LOCATION, target, (), request, context)
    )
    terminal = _terminal_unresolved(target, declarations)
    if terminal is not None:
        return terminal
    if len(declarations.candidates) == 0:
        return UnresolvedTarget(
            target=target,
            reason=UnresolvedReason.NOT_FOUND,
            diagnostics=declarations.diagnostics,
            candidate_coverage=declarations.coverage,
        )
    if len(declarations.candidates) > 1:
        return UnresolvedTarget(
            target=target,
            reason=UnresolvedReason.INCOMPLETE,
            diagnostics=(
                *declarations.diagnostics,
                Diagnostic(
                    message=(
                        "the requested location resolved to several declarations; "
                        "narrow the position or name a candidate explicitly"
                    ),
                    code=PROVIDER_ERROR,
                ),
            ),
            candidate_coverage=declarations.coverage,
        )
    candidate = declarations.candidates[0]
    if context.source_versions is not None and not candidate_is_current(
        candidate, context.source_versions
    ):
        return _stale(
            target,
            candidate,
            declarations,
            "the entity at the requested location no longer matches its recorded "
            "source version",
        )
    return ResolvedTarget(
        target=target,
        method=SelectionMethod.EXACT_LOCATION,
        declaration=candidate,
        candidate_coverage=declarations.coverage,
        candidate_evidence=declarations.acquisitions,
    )
