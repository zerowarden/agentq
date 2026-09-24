"""Independent evaluation of one decision against human labels.

The evaluator never parses prose. A facet holds when the delivered variants
satisfy one of its witness clauses; a witness holds when any of its acceptable
variants is present. Coverage is reported separately for the acquired pool,
the initial selection, and the final delivery, and correctness gates are
checked before any quality number is trusted. Unjudged evidence is neither
credited nor treated as irrelevant.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from agentq.core import ContractError, canonical_digest
from agentq.inspection.contracts import (
    DecisionDelivered,
    DecisionFailure,
    DecisionOutcome,
    Fidelity,
    RequirementRule,
    RequirementStatus,
    SelectionPlan,
)
from agentq.inspection.decision import DecisionConfig

from .codec import capture_digest, judgment_digest
from .models import (
    CaseEvaluation,
    ExpectedOutcomeKind,
    FacetCoverage,
    JudgmentFacet,
    JudgmentSet,
    ReplayCapture,
)
from .replay import decision_id

METRIC_PROFILE = "metrics-v1"


@dataclass(frozen=True)
class MetricConfig:
    """The metric configuration half of an evaluation identity."""

    profile: str = METRIC_PROFILE

    def to_wire(self) -> dict[str, object]:
        return {"profile": self.profile}


DEFAULT_METRIC_CONFIG = MetricConfig()


def witness_supported(
    judgment: JudgmentSet, witness_id: str, selected_ids: frozenset[str]
) -> bool:
    """A witness holds when any of its acceptable variants is present."""
    witness = judgment.witness(witness_id)
    if witness is None:
        return False
    return bool(selected_ids.intersection(witness.acceptable_variant_ids))


def facet_supported(
    facet: JudgmentFacet, judgment: JudgmentSet, selected_ids: frozenset[str]
) -> bool:
    """A facet holds when one clause's every witness holds."""
    return any(
        all(
            witness_supported(judgment, witness_id, selected_ids)
            for witness_id in clause
        )
        for clause in facet.witness_sets
    )


def _selection_variant_ids(selection: SelectionPlan | None) -> tuple[str, ...]:
    if selection is None:
        return ()
    return tuple(chosen.variant.variant_id for chosen in selection.selected)


def _pool_variant_ids(capture: ReplayCapture) -> frozenset[str]:
    """Every acquired variant: pool coverage reports availability, not admissibility."""
    return frozenset(variant.variant_id for variant in capture.decision.pool.variants)


def _render_violations(
    outcome: DecisionDelivered, decision_config: DecisionConfig
) -> list[str]:
    render = outcome.bundle.render
    if render is None:
        return ["delivered decision carries no rendered bundle"]
    violations: list[str] = []
    if render.chars != len(render.text):
        violations.append("rendered character count does not match its text")
    if render.chars + 1 > decision_config.delivery.max_chars:
        violations.append(
            "delivered response exceeds its ceiling: "
            f"{render.chars + 1} > {decision_config.delivery.max_chars}"
        )
    return violations


def _unstable_violations(capture: ReplayCapture, outcome: DecisionOutcome) -> list[str]:
    unstable = set(capture.decision.pool.unstable_observation_ids)
    if not unstable or not isinstance(outcome, DecisionDelivered):
        return []
    violations: list[str] = []
    stages = (
        ("initial selection", outcome.initial_selection),
        ("final delivery", outcome.bundle.selection),
    )
    for label, selection in stages:
        if selection is None:
            continue
        for chosen in selection.selected:
            if chosen.observation_id in unstable:
                violations.append(
                    f"unstable observation {chosen.observation_id} was selected "
                    f"in the {label}"
                )
    return violations


def _duplicate_violations(outcome: DecisionDelivered) -> list[str]:
    violations: list[str] = []
    stages = (
        ("initial selection", outcome.initial_selection),
        ("final delivery", outcome.bundle.selection),
    )
    for label, selection in stages:
        if selection is None:
            continue
        ids = [chosen.variant.variant_id for chosen in selection.selected]
        if len(set(ids)) != len(ids):
            violations.append(f"the {label} repeats an evidence variant")
    return violations


def _exact_source_violations(
    capture: ReplayCapture, outcome: DecisionDelivered
) -> list[str]:
    """A satisfied exact-source requirement must have an exact representation."""
    bundle = outcome.bundle
    assessment = bundle.assessment
    selection = bundle.selection
    policy = bundle.policy
    if assessment is None or selection is None or policy is None:
        return []
    pool = capture.decision.pool
    violations: list[str] = []
    for requirement in policy.requirements:
        if requirement.rule is not RequirementRule.EXACT_SOURCE:
            continue
        status = assessment.by_id(requirement.requirement_id)
        if status is None or status.status is not RequirementStatus.SATISFIED:
            continue
        supported = False
        for chosen in selection.selected:
            variant = chosen.variant
            observation = pool.observation(variant.observation_id)
            if observation is None:
                continue
            if (
                requirement.acceptable_kinds
                and observation.kind not in requirement.acceptable_kinds
            ):
                continue
            if (
                requirement.representations
                and variant.representation not in requirement.representations
            ):
                continue
            if variant.fidelity is not Fidelity.EXACT:
                continue
            supported = True
            break
        if not supported:
            violations.append(
                f"requirement {requirement.requirement_id} claims exact source "
                "without an exact representation selected"
            )
    return violations


def _check_expectations(
    capture: ReplayCapture,
    outcome: DecisionOutcome,
    judgments: JudgmentSet,
) -> tuple[str, ...]:
    unmet: list[str] = []
    delivered = outcome if isinstance(outcome, DecisionDelivered) else None
    assessment = delivered.bundle.assessment if delivered is not None else None
    for expected in judgments.expected_outcomes:
        if expected.kind is ExpectedOutcomeKind.DELIVERED:
            if delivered is None:
                unmet.append(
                    "expected a delivered decision, observed a delivery failure"
                )
            continue
        if expected.kind is ExpectedOutcomeKind.REQUIREMENT_STATUS:
            if assessment is None:
                unmet.append(
                    f"expected {expected.requirement_id} "
                    f"{expected.status}, observed no assessment"
                )
                continue
            found = assessment.by_id(str(expected.requirement_id))
            observed = found.status.value if found is not None else "missing"
            if observed != expected.status:
                unmet.append(
                    f"expected {expected.requirement_id} {expected.status}, "
                    f"observed {observed}"
                )
            continue
        if expected.kind is ExpectedOutcomeKind.FITTING_REDUCTION:
            observed_events = len(delivered.fitting_events) if delivered else 0
            if observed_events < int(expected.minimum or 0):
                unmet.append(
                    "expected at least "
                    f"{expected.minimum} fitting reductions, observed "
                    f"{observed_events}"
                )
            continue
        codes = {item.code for item in capture.decision.pool.limitations}
        if expected.code not in codes:
            unmet.append(f"expected limitation {expected.code!r} to be recorded")
    return tuple(unmet)


def evaluate_decision(
    capture: ReplayCapture,
    outcome: DecisionOutcome,
    judgments: JudgmentSet,
    *,
    decision_config: DecisionConfig,
    config: MetricConfig = DEFAULT_METRIC_CONFIG,
) -> CaseEvaluation:
    """Evaluate one decision against one capture-bound judgment set."""
    capture_id = capture_digest(capture)
    if judgments.capture_id != capture_id:
        raise ContractError("judgment set is bound to a different capture")
    if judgments.case_id != capture.case_id:
        raise ContractError("judgment set case does not match the capture case")
    decision_key = decision_id(capture_id, decision_config)
    judgment_key = judgment_digest(judgments)
    evaluation_key = canonical_digest(
        {
            "decision": decision_key,
            "judgments": judgment_key,
            "metrics": config.to_wire(),
        }
    )

    delivered = outcome if isinstance(outcome, DecisionDelivered) else None
    if delivered is not None:
        initial_ids = frozenset(_selection_variant_ids(outcome.initial_selection))
        delivered_ids = frozenset(_selection_variant_ids(outcome.bundle.selection))
    else:
        initial_ids = frozenset()
        delivered_ids = frozenset()
    pool_ids = _pool_variant_ids(capture)

    facets = tuple(
        FacetCoverage(
            facet_id=facet.facet_id,
            critical=facet.critical,
            pool_supported=facet_supported(facet, judgments, pool_ids),
            initial_supported=facet_supported(facet, judgments, initial_ids),
            delivered_supported=facet_supported(facet, judgments, delivered_ids),
        )
        for facet in judgments.facets
    )

    violations: list[str] = []
    render_chars: int | None = None
    render_bytes: int | None = None
    failure_detail: str | None = None
    if delivered is not None:
        violations.extend(_render_violations(delivered, decision_config))
        violations.extend(_unstable_violations(capture, outcome))
        violations.extend(_duplicate_violations(delivered))
        violations.extend(_exact_source_violations(capture, delivered))
        render = delivered.bundle.render
        if render is not None:
            render_chars = render.chars
            render_bytes = len(render.text.encode("utf-8"))
    else:
        assert isinstance(outcome, DecisionFailure)
        failure_detail = outcome.detail

    return CaseEvaluation(
        case_id=capture.case_id,
        capture_id=capture_id,
        judgment_id=judgment_key,
        decision_id=decision_key,
        evaluation_id=evaluation_key,
        outcome="delivered" if delivered is not None else "failed",
        violations=tuple(violations),
        unmet_expectations=_check_expectations(capture, outcome, judgments),
        facets=facets,
        render_chars=render_chars,
        render_bytes=render_bytes,
        token_count=None,
        token_reason="no pinned tokenizer",
        failure_detail=failure_detail,
        selected_variant_ids=_selection_variant_ids(
            None if delivered is None else delivered.bundle.selection
        ),
        initial_selected_variant_ids=(
            () if delivered is None else _selection_variant_ids(outcome.initial_selection)
        ),
        fitting_events=0 if delivered is None else len(delivered.fitting_events),
    )


def _ratio(numerator: int, denominator: int) -> float | None:
    return None if denominator == 0 else numerator / denominator


def summarize(
    evaluations: Sequence[CaseEvaluation], config: MetricConfig = DEFAULT_METRIC_CONFIG
) -> dict[str, object]:
    """Aggregate quality metrics with explicit denominators and N/A handling."""
    critical = sum(item.critical_total for item in evaluations)
    with_critical = [item for item in evaluations if item.critical_total]
    present = [item for item in with_critical if item.all_critical_present]
    return {
        "profile": config.profile,
        "cases": len(evaluations),
        "violation_cases": sum(1 for item in evaluations if item.violations),
        "unmet_expectation_cases": sum(
            1 for item in evaluations if item.unmet_expectations
        ),
        "delivery_failures": sum(
            1 for item in evaluations if item.outcome == "failed"
        ),
        "critical_facets": critical,
        "critical_delivered": sum(item.critical_delivered for item in evaluations),
        "critical_recall_delivered": _ratio(
            sum(item.critical_delivered for item in evaluations), critical
        ),
        "critical_recall_pool": _ratio(
            sum(item.critical_pool for item in evaluations), critical
        ),
        "critical_recall_initial": _ratio(
            sum(item.critical_initial for item in evaluations), critical
        ),
        "cases_with_critical_facets": len(with_critical),
        "all_critical_present_cases": len(present),
        "all_critical_present_rate": _ratio(len(present), len(with_critical)),
        "render_chars_total": sum(item.render_chars or 0 for item in evaluations),
    }
