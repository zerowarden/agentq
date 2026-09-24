"""The shared post-acquisition decision stage.

Live inspection and evaluation replay call the same :func:`decide_evidence`.
The stage runs after acquisition and source-stability assessment: it extracts
features, scores evidence, selects the initial bundle, assesses policy
requirements, fits the whole response to the delivery ceiling, and returns the
delivered bundle with its intermediate records.

The function reads only a :class:`DecisionInput` and a :class:`DecisionConfig`:
no repository, provider, clock, network, subprocess, or evaluation label. The
optional trace recorder is observational only and never changes the outcome.
"""

from __future__ import annotations

from collections.abc import Callable, Generator
from contextlib import AbstractContextManager, contextmanager
from dataclasses import dataclass, field, replace

from agentq.core import ContractError, merge_typed

from .budgeting import (
    DELIVERY_BUDGET_CODE,
    DELIVERY_FORMATS,
    DELIVERY_TERMINATOR_CHARS,
    DeliveryBudget,
)
from .contracts import (
    CollectionPlan,
    DecisionDelivered,
    DecisionFailure,
    DecisionInput,
    DecisionOutcome,
    EvidencePolicy,
    EvidencePool,
    FittingEvent,
    InspectionBundle,
    InspectionRequest,
    OmittedEvidence,
    PolicyAssessment,
    ResolvedTarget,
    SelectionPlan,
)
from .debug import StageTrace, TraceRecorder
from .features import extract_features
from .rendering import attach_render, delivery_overflow
from .scoring import DEFAULT_SCORING, ScoringProfile, score_evidence
from .selection import (
    DEFAULT_SELECTION,
    SelectionProfile,
    assess_selected_evidence,
    reduce_selection,
    select_evidence,
)

# Bumped whenever the stage's behavior changes: part of the decision identity,
# so a replay cannot silently attribute a new decision to an old fingerprint.
DECISION_VERSION = "decision-v1"


@dataclass(frozen=True)
class DecisionConfig:
    """Every algorithm/configuration choice that can change one decision."""

    scoring: ScoringProfile = DEFAULT_SCORING
    selection: SelectionProfile = DEFAULT_SELECTION
    delivery: DeliveryBudget = field(default_factory=DeliveryBudget)
    output_format: str = "text"

    def __post_init__(self) -> None:
        if not isinstance(self.scoring, ScoringProfile):
            raise ContractError("decision config scoring must be a ScoringProfile")
        if not isinstance(self.selection, SelectionProfile):
            raise ContractError("decision config selection must be a SelectionProfile")
        if not isinstance(self.delivery, DeliveryBudget):
            raise ContractError("decision config delivery must be a DeliveryBudget")
        if self.output_format not in DELIVERY_FORMATS:
            raise ContractError(
                f"unsupported decision output format: {self.output_format!r}"
            )

    def to_wire(self) -> dict[str, object]:
        return {
            "scoring": self.scoring.to_wire(),
            "selection": self.selection.to_wire(),
            "delivery": self.delivery.to_wire(),
            "output_format": self.output_format,
        }


@contextmanager
def _untraced_stage(_name: str) -> Generator[StageTrace, None, None]:
    """An untimed stage for decisions that run without a trace recorder."""
    yield StageTrace()


def decide_evidence(
    decision_input: DecisionInput,
    config: DecisionConfig,
    *,
    trace: TraceRecorder | None = None,
) -> DecisionOutcome:
    """Run the one post-acquisition decision path."""
    stage: Callable[[str], AbstractContextManager[StageTrace]]
    stage = trace.stage if trace is not None else _untraced_stage
    request = decision_input.request
    pool = decision_input.pool
    policy = decision_input.policy
    plan = decision_input.collection

    with stage("scoring") as span:
        features = extract_features(pool)
        scores = score_evidence(features, config.scoring, intent=request.intent)
        span.note(
            profile=config.scoring.profile,
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

    with stage("selection") as span:
        initial = select_evidence(
            pool,
            scores,
            policy,
            config.delivery,
            output_format=config.output_format,
            profile=config.selection,
        )
        assessment = assess_selected_evidence(policy, plan, pool, initial)
        span.note(
            profile=initial.profile,
            acquired=len(pool.observations),
            selected=len(initial.selected),
            omitted=_omission_fields(initial.omitted),
            required_missing=[
                item.requirement_id
                for item in assessment.unsatisfied(required_only=True)
            ],
            output_chars=initial.measured_cost,
        )

    with stage("assessment") as span:
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

    bundle = _build_bundle(
        request, decision_input.resolution, policy, plan, pool, initial, assessment
    )
    with stage("render") as span:
        fitted, events = _fit_delivery(
            bundle,
            policy=policy,
            plan=plan,
            pool=pool,
            delivery=config.delivery,
            output_format=config.output_format,
        )
        render = fitted.render
        if delivery_overflow(fitted, config.delivery) > 0:
            span.note(
                format=config.output_format,
                chars=0 if render is None else render.chars,
                selected=0,
                failed=True,
            )
            return DecisionFailure(
                reason=DELIVERY_BUDGET_CODE,
                detail=_envelope_detail(fitted, config.delivery),
            )
        span.note(
            format=config.output_format,
            chars=0 if render is None else render.chars,
            selected=len(fitted.selection.selected) if fitted.selection else 0,
        )
    return DecisionDelivered(
        bundle=fitted,
        features=features,
        scores=scores,
        initial_selection=initial,
        fitting_events=events,
    )


def _fit_delivery(
    bundle: InspectionBundle,
    *,
    policy: EvidencePolicy,
    plan: CollectionPlan,
    pool: EvidencePool,
    delivery: DeliveryBudget,
    output_format: str,
) -> tuple[InspectionBundle, tuple[FittingEvent, ...]]:
    """Reduce the complete rendered response until it respects the ceiling.

    Selection bounds evidence text alone; the delivered result also carries
    request, resolution, policy, collection, assessment, gaps, and compacted
    omission metadata. The whole serialized projection is measured, and the
    lowest-priority selected representations are dropped until the response,
    transport terminator included, fits. The returned bundle still overflows
    when nothing is left to drop; the caller reports that as a typed failure.
    """
    bundle = attach_render(bundle, output_format)
    events: list[FittingEvent] = []
    while (overflow := delivery_overflow(bundle, delivery)) > 0:
        assert bundle.selection is not None
        reduced = reduce_selection(
            bundle.selection, overflow_chars=overflow, output_format=output_format
        )
        if reduced is None:
            return bundle, tuple(events)
        events.append(
            FittingEvent(
                overflow_chars=overflow,
                dropped_variant_ids=_dropped_variant_ids(bundle.selection, reduced),
            )
        )
        bundle = replace(
            bundle,
            selection=reduced,
            assessment=assess_selected_evidence(policy, plan, pool, reduced),
        )
        bundle = attach_render(bundle, output_format)
    return bundle, tuple(events)


def _dropped_variant_ids(
    before: SelectionPlan, after: SelectionPlan
) -> tuple[str, ...]:
    kept = {item.variant_id for item in after.selected}
    return tuple(
        item.variant_id for item in before.selected if item.variant_id not in kept
    )


def _envelope_detail(bundle: InspectionBundle, delivery: DeliveryBudget) -> str:
    render = bundle.render
    chars = 0 if render is None else render.chars
    return (
        "the delivery budget cannot hold the inspection response: "
        f"{chars + DELIVERY_TERMINATOR_CHARS} characters "
        f"exceed max_chars={delivery.max_chars}"
    )


def _build_bundle(
    request: InspectionRequest,
    resolution: ResolvedTarget,
    policy: EvidencePolicy,
    plan: CollectionPlan,
    pool: EvidencePool,
    selection: SelectionPlan,
    assessment: PolicyAssessment,
) -> InspectionBundle:
    coverage = merge_typed(resolution.candidate_coverage, pool.coverage)
    return InspectionBundle(
        request=request,
        resolution=resolution,
        policy=policy,
        collection=plan,
        selection=selection,
        assessment=assessment,
        gaps=pool.limitations,
        coverage=coverage,
    )


def _omission_fields(items: tuple[OmittedEvidence, ...]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for item in items:
        counts[item.reason] = counts.get(item.reason, 0) + 1
    return counts
