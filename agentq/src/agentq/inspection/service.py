"""Inspection pipeline orchestration.

The service is a direct composition of independently replaceable stages. It
selects no evidence itself, branches on no language name, and imports no
concrete adapter: the capability registry arrives through the context from the
composition boundary. Enabling debug records a bounded trace and changes
nothing else.
"""

from __future__ import annotations

from dataclasses import replace

from agentq.core import ContractError, Diagnostic, merge_typed

from .acquisition import acquire, plan_collection
from .budgeting import DELIVERY_BUDGET_CODE, DELIVERY_TERMINATOR_CHARS
from .capabilities import empty_capability_report
from .contracts import (
    AmbiguousTarget,
    CapabilityGap,
    CapabilityReport,
    CollectionPlan,
    DeclarationCandidate,
    EvidencePolicy,
    EvidencePool,
    InspectionBundle,
    InspectionContext,
    InspectionRequest,
    OmittedEvidence,
    PolicyAssessment,
    ResolutionResult,
    ResolvedTarget,
    SelectionPlan,
    UnresolvedTarget,
    describe_target,
    has_selected_target,
    inspection_request_identity,
    with_request_id,
)
from .debug import TraceRecorder
from .execution import ExecutionLedger
from .features import extract_features
from .lifecycle import apply_unstable, unstable_observations
from .policy import compile_policy
from .rendering import render_bundle
from .resolution import resolve
from .scoring import DEFAULT_SCORING, ScoringProfile, score_evidence
from .selection import (
    DEFAULT_SELECTION,
    SelectionProfile,
    assess_selected_evidence,
    reduce_selection,
    select_evidence,
)


def inspect(
    request: InspectionRequest,
    context: InspectionContext,
    *,
    scoring: ScoringProfile = DEFAULT_SCORING,
    selection: SelectionProfile = DEFAULT_SELECTION,
) -> InspectionBundle:
    """Run one inspection request end to end."""
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

    with recorder.stage("scoring") as span:
        features = extract_features(pool)
        scores = score_evidence(features, scoring, intent=normalized.intent)
        span.note(
            profile=scoring.profile,
            scored=len(scores),
            scores={item.observation_id: item.score.total for item in scores},
            contributions={
                item.observation_id: ",".join(
                    f"{contribution.name}={contribution.value}"
                    for contribution in item.score.contributions
                )
                for item in scores
                if item.score.contributions
            },
        )

    with recorder.stage("selection") as span:
        selection_plan = select_evidence(
            pool,
            scores,
            policy,
            context.delivery,
            output_format=context.presentation.output_format,
            profile=selection,
        )
        assessment = assess_selected_evidence(policy, plan, pool, selection_plan)
        span.note(
            profile=selection_plan.profile,
            acquired=len(pool.observations),
            selected=len(selection_plan.selected),
            omitted=_omission_fields(selection_plan.omitted),
            required_missing=[
                item.requirement_id
                for item in assessment.unsatisfied(required_only=True)
            ],
            output_chars=selection_plan.measured_cost,
        )

    with recorder.stage("assessment") as span:
        span.note(
            satisfied=sum(
                1
                for item in assessment.requirements
                if item.status.value == "satisfied"
            ),
            unsatisfied=len(assessment.unsatisfied()),
            required_missing=[
                item.requirement_id
                for item in assessment.unsatisfied(required_only=True)
            ],
        )

    bundle = build_bundle(
        normalized, resolution, policy, plan, pool, selection_plan, assessment
    )
    with recorder.stage("render") as span:
        bundle = fit_delivery(bundle, context, policy=policy, plan=plan, pool=pool)
        span.note(
            format=context.presentation.output_format,
            chars=bundle.render.chars if bundle.render is not None else 0,
            selected=len(bundle.selection.selected) if bundle.selection else 0,
        )
    return bundle


def fit_delivery(
    bundle: InspectionBundle,
    context: InspectionContext,
    *,
    policy: EvidencePolicy,
    plan: CollectionPlan,
    pool: EvidencePool,
) -> InspectionBundle:
    """Reduce the complete rendered bundle until it respects the ceiling.

    Selection bounds evidence text alone; the delivered result also carries
    request, resolution, policy, collection, assessment, gaps, and compacted
    omission metadata. The whole serialized projection is measured, and the
    lowest-priority selected representations are dropped until the response,
    transport terminator included, fits. A budget that cannot hold even the
    response envelope is a configuration error, not an oversized delivery.
    """
    if bundle.selection is None:
        raise ContractError("fit_delivery requires a resolved bundle with a selection")
    output_format = context.presentation.output_format
    bundle = attach_render(bundle, context)
    while (overflow := _delivery_overflow(bundle, context)) > 0:
        assert bundle.selection is not None
        reduced = reduce_selection(
            bundle.selection, overflow_chars=overflow, output_format=output_format
        )
        if reduced is None:
            assert bundle.render is not None
            raise ContractError(
                "the delivery budget cannot hold the inspection response: "
                f"{bundle.render.chars + DELIVERY_TERMINATOR_CHARS} characters "
                f"exceed max_chars={context.delivery.max_chars}"
            )
        bundle = replace(
            bundle,
            selection=reduced,
            assessment=assess_selected_evidence(policy, plan, pool, reduced),
        )
        bundle = attach_render(bundle, context)
    return bundle


def fit_resolution_delivery(
    bundle: InspectionBundle, context: InspectionContext
) -> InspectionBundle:
    """Bound a resolution-only response by retaining fewer candidates.

    An ambiguous result has no evidence to reduce: its size is the candidate
    list itself. When the complete projection exceeds the delivery ceiling the
    lowest-priority candidates are removed; the retained and total counts stay
    truthful and one explicit gap records the reduction.
    """
    bundle = attach_render(bundle, context)
    while _delivery_overflow(bundle, context) > 0:
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
        bundle = attach_render(bundle, context)
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


def _delivery_overflow(bundle: InspectionBundle, context: InspectionContext) -> int:
    render = bundle.render
    if render is None:
        return 0
    return max(0, render.chars - context.delivery.payload_capacity())


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


def build_bundle(
    request: InspectionRequest,
    resolution: ResolutionResult,
    policy: EvidencePolicy,
    plan: CollectionPlan,
    pool: EvidencePool,
    selection_plan: SelectionPlan,
    assessment: PolicyAssessment,
) -> InspectionBundle:
    coverage = merge_typed(resolution.candidate_coverage, pool.coverage)
    return InspectionBundle(
        request=request,
        resolution=resolution,
        policy=policy,
        collection=plan,
        selection=selection_plan,
        assessment=assessment,
        gaps=pool.limitations,
        coverage=coverage,
    )


def attach_render(
    bundle: InspectionBundle, context: InspectionContext
) -> InspectionBundle:
    """Serialize the bundle's current state exactly once.

    The previous render is cleared first so a re-render never embeds stale
    render metadata of its own: a measured cost is the length of the text that
    is actually delivered.
    """
    rendered = render_bundle(
        replace(bundle, render=None),
        output_format=context.presentation.output_format,
    )
    return replace(bundle, render=rendered)


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


def _omission_fields(items: tuple[OmittedEvidence, ...]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for item in items:
        counts[item.reason] = counts.get(item.reason, 0) + 1
    return counts
