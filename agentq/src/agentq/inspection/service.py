"""Inspection pipeline orchestration.

The service is a direct composition of independently replaceable stages. It
selects no evidence itself, branches on no language name, and imports no
concrete adapter: the capability registry arrives through the context from the
composition boundary. Enabling debug records a bounded trace and changes
nothing else.
"""

from __future__ import annotations

from dataclasses import replace

from agentq.core import merge_typed

from .acquisition import acquire, plan_collection
from .capabilities import empty_capability_report
from .contracts import (
    AmbiguousTarget,
    CapabilityGap,
    CapabilityReport,
    CollectionPlan,
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
            bundle = attach_render(bundle, context)
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
        plan = plan_collection(
            policy,
            resolution,
            report,
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
        bundle = attach_render(bundle, context)
        span.note(
            format=context.presentation.output_format,
            chars=bundle.render.chars if bundle.render is not None else 0,
        )
    return bundle


def normalize_request(
    request: InspectionRequest, context: InspectionContext
) -> InspectionRequest:
    """Bind the request to a stable semantic identity."""
    request_id = inspection_request_identity(request, context.root)
    return with_request_id(request, request_id)


def describe_capabilities(
    request: InspectionRequest, context: InspectionContext
) -> CapabilityReport:
    if context.registry is None:
        return empty_capability_report(request.request_id)
    return context.registry.describe(request, context)


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
    rendered = render_bundle(bundle, output_format=context.presentation.output_format)
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
