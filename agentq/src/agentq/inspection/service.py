"""Inspection pipeline orchestration.

The service is a direct composition of independently replaceable stages. It
selects no evidence itself, branches on no language name, and imports no
concrete adapter: the capability registry arrives through the context from the
composition boundary. Post-acquisition decisions are delegated to
:func:`agentq.inspection.decision.decide_evidence`, the same function replay
uses. Enabling debug records a bounded trace and changes nothing else.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import replace

from agentq.core import ContractError, Diagnostic

from .acquisition import acquire, plan_collection
from .budgeting import DELIVERY_BUDGET_CODE, DELIVERY_TERMINATOR_CHARS
from .capabilities import empty_capability_report
from .contracts import (
    AmbiguousTarget,
    CapabilityGap,
    CapabilityReport,
    DecisionFailure,
    DecisionInput,
    DeclarationCandidate,
    InspectionBundle,
    InspectionContext,
    InspectionRequest,
    ResolutionResult,
    ResolvedTarget,
    UnresolvedTarget,
    describe_target,
    has_selected_target,
    inspection_request_identity,
    with_request_id,
)
from .debug import TraceRecorder
from .decision import DecisionConfig, decide_evidence
from .execution import ExecutionLedger
from .lifecycle import apply_unstable, unstable_observations
from .policy import compile_policy
from .rendering import attach_render, delivery_overflow
from .resolution import resolve
from .scoring import DEFAULT_SCORING, ScoringProfile
from .selection import DEFAULT_SELECTION, SelectionProfile


def inspect(
    request: InspectionRequest,
    context: InspectionContext,
    *,
    scoring: ScoringProfile = DEFAULT_SCORING,
    selection: SelectionProfile = DEFAULT_SELECTION,
    on_prepared: Callable[[DecisionInput], None] | None = None,
) -> InspectionBundle:
    """Run one inspection request end to end.

    ``on_prepared`` is the explicit, opt-in capture hook at the prepared-input
    boundary: it receives the complete DecisionInput once acquisition and
    stability assessment are finished. It is never triggered by ``debug`` and
    never changes the decision.
    """
    context = _execution_context(context)
    recorder = context.trace if context.trace is not None else TraceRecorder()
    with recorder.stage("normalize") as span:
        normalized = normalize_request(request, context)
        span.note(
            target=describe_target(normalized.target),
            target_kind=normalized.target.kind.value,
            intent=normalized.intent.value,
        )

    with recorder.stage("capabilities") as span:
        report = describe_capabilities(normalized, context)
        span.note(
            available=list(report.available_providers()),
            gaps=_gap_fields(report.gaps()),
        )

    with recorder.stage("resolution") as span:
        resolution = resolve(normalized, report, context)
        span.note(
            outcome=_outcome_name(resolution),
            candidates=_candidate_count(resolution),
            coverage=resolution.candidate_coverage.status,
        )

    if not has_selected_target(resolution):
        bundle = build_resolution_bundle(normalized, resolution)
        with recorder.stage("render") as span:
            bundle = fit_resolution_delivery(bundle, context)
            span.note(
                format=context.presentation.output_format,
                chars=bundle.render.chars if bundle.render is not None else 0,
            )
        return bundle

    with recorder.stage("policy") as span:
        policy = compile_policy(normalized, resolution)
        span.note(
            profile=policy.profile,
            requirements=[item.requirement_id for item in policy.requirements],
        )

    with recorder.stage("collection") as span:
        # Symbol-anchored capabilities are selected for the resolved subject,
        # not merely for the original target: a mixed-language scope must not
        # let one language's adapter answer for another language's declaration.
        subject = _selected_declaration(resolution)
        collection_capabilities = (
            report
            if subject is None
            else describe_capabilities(normalized, context, subject=subject)
        )
        plan = plan_collection(
            policy,
            resolution,
            collection_capabilities,
            context.limits,
            request_id=normalized.request_id,
            evidence_scopes=normalized.evidence_scopes,
        )
        pool = acquire(plan, context, prior=resolution.candidate_evidence)
        if context.source_versions is not None:
            unstable = unstable_observations(pool.observations, context.source_versions)
            pool = apply_unstable(pool, unstable)
        span.note(
            requests=len(plan.requests),
            omissions=len(plan.omissions),
            acquisitions=len(pool.acquisitions),
            observations=len(pool.observations),
            unstable=list(pool.unstable_observation_ids),
        )

    decision_input = DecisionInput(
        request=normalized,
        resolution=resolution,
        policy=policy,
        collection=plan,
        pool=pool,
    )
    if on_prepared is not None:
        on_prepared(decision_input)
    outcome = decide_evidence(
        decision_input,
        DecisionConfig(
            scoring=scoring,
            selection=selection,
            delivery=context.delivery,
            output_format=context.presentation.output_format,
        ),
        trace=recorder,
    )
    if isinstance(outcome, DecisionFailure):
        raise ContractError(outcome.detail)
    return outcome.bundle


def fit_resolution_delivery(
    bundle: InspectionBundle, context: InspectionContext
) -> InspectionBundle:
    """Bound a resolution-only response by retaining fewer candidates.

    An ambiguous result has no evidence to reduce: its size is the candidate
    list itself. When the complete projection exceeds the delivery ceiling the
    lowest-priority candidates are removed; the retained and total counts stay
    truthful and one explicit gap records the reduction.
    """
    output_format = context.presentation.output_format
    bundle = attach_render(bundle, output_format)
    while delivery_overflow(bundle, context.delivery) > 0:
        resolution = bundle.resolution
        if not isinstance(resolution, AmbiguousTarget) or not resolution.candidates:
            assert bundle.render is not None
            raise ContractError(
                "the delivery budget cannot hold the inspection response: "
                f"{bundle.render.chars + DELIVERY_TERMINATOR_CHARS} characters "
                f"exceed max_chars={context.delivery.max_chars}"
            )
        bundle = replace(
            bundle,
            resolution=replace(resolution, candidates=resolution.candidates[:-1]),
            gaps=_candidate_reduction_gaps(bundle.gaps),
        )
        bundle = attach_render(bundle, output_format)
    return bundle


def _candidate_reduction_gaps(gaps: tuple[Diagnostic, ...]) -> tuple[Diagnostic, ...]:
    if any(item.code == DELIVERY_BUDGET_CODE for item in gaps):
        return gaps
    return (
        *gaps,
        Diagnostic(
            message=(
                "the ambiguous candidate list was reduced to fit the delivery "
                "budget; narrow the target to inspect a specific declaration"
            ),
            code=DELIVERY_BUDGET_CODE,
            severity="warning",
        ),
    )


def _execution_context(context: InspectionContext) -> InspectionContext:
    """One execution ledger per inspection, shared by resolution and collection."""
    if context.execution is not None:
        return context
    return replace(context, execution=ExecutionLedger(context.limits))


def normalize_request(
    request: InspectionRequest, context: InspectionContext
) -> InspectionRequest:
    """Bind the request to a stable semantic identity."""
    request_id = inspection_request_identity(request, context.root)
    return with_request_id(request, request_id)


def describe_capabilities(
    request: InspectionRequest,
    context: InspectionContext,
    *,
    subject: DeclarationCandidate | None = None,
) -> CapabilityReport:
    if context.registry is None:
        return empty_capability_report(request.request_id)
    return context.registry.describe(request, context, subject)


def _selected_declaration(
    resolution: ResolutionResult,
) -> DeclarationCandidate | None:
    if isinstance(resolution, ResolvedTarget):
        return resolution.declaration
    return None


def build_resolution_bundle(
    request: InspectionRequest, resolution: ResolutionResult
) -> InspectionBundle:
    """A valid response for ambiguous or unresolved requests."""
    gaps = resolution.diagnostics if isinstance(resolution, UnresolvedTarget) else ()
    return InspectionBundle(
        request=request,
        resolution=resolution,
        gaps=gaps,
        coverage=resolution.candidate_coverage,
    )


def _outcome_name(resolution: ResolutionResult) -> str:
    match resolution:
        case ResolvedTarget():
            return "resolved"
        case AmbiguousTarget():
            return "ambiguous"
        case _:
            return "unresolved"


def _candidate_count(resolution: ResolutionResult) -> int:
    if isinstance(resolution, AmbiguousTarget):
        return len(resolution.candidates)
    if isinstance(resolution, ResolvedTarget) and resolution.declaration is not None:
        return 1
    return 0


def _gap_fields(gaps: tuple[CapabilityGap, ...]) -> dict[str, str]:
    return {gap.capability.value: gap.reason or gap.status.value for gap in gaps}
