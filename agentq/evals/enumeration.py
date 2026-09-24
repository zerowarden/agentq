"""Evaluation-only exact reference for tiny selection problems.

The reference enumerates at most one variant per observation, keeps the
selections that satisfy every required policy requirement and fit the
selection budget, and reports the highest declared utility: the sum of the
scores of the distinct observations selected, so duplicates cannot multiply
it. It models the selection stage only; overlap, per-file quotas, and
whole-delivery fitting are production heuristics outside the model. Optimality
is claimed only for this model and only for tiny pools.
"""

from __future__ import annotations

from dataclasses import dataclass
from itertools import product

from agentq.core import ContractError
from agentq.inspection.contracts import (
    CollectionPlan,
    EvidencePolicy,
    EvidencePool,
    EvidenceVariant,
    SelectedEvidence,
    SelectionPlan,
)
from agentq.inspection.decision import DecisionConfig
from agentq.inspection.features import extract_features
from agentq.inspection.rendering import selection_cost
from agentq.inspection.scoring import score_evidence
from agentq.inspection.selection import (
    REASON_RELEVANCE,
    assess_selected_evidence,
    select_evidence,
)

REFERENCE_PROFILE = "reference-exact-v1"
MAX_REFERENCE_CHOICES = 4096


@dataclass(frozen=True)
class ReferenceOutcome:
    """The reference optimum and the heuristic result under one model."""

    utility: int
    variant_ids: tuple[str, ...]
    measured_cost: int
    choices: int
    heuristic_utility: int
    heuristic_variant_ids: tuple[str, ...]
    heuristic_feasible: bool

    @property
    def gap(self) -> int:
        return self.utility - self.heuristic_utility


def _utility(
    variant_ids: tuple[str, ...],
    pool: EvidencePool,
    score_by_observation: dict[str, int],
) -> int:
    """Utility counts distinct observations once, so duplicates add nothing."""
    selected = set(variant_ids)
    observation_ids = {
        variant.observation_id
        for variant in pool.variants
        if variant.variant_id in selected
    }
    return sum(
        score_by_observation.get(observation_id, 0)
        for observation_id in observation_ids
    )


def _plan(
    selected: tuple[SelectedEvidence, ...], measured_cost: int, available: int
) -> SelectionPlan:
    return SelectionPlan(
        profile=REFERENCE_PROFILE,
        selected=selected,
        omitted=(),
        reserved=(),
        measured_cost=measured_cost,
        budget_chars=available,
    )


def reference_selection(
    pool: EvidencePool,
    policy: EvidencePolicy,
    collection: CollectionPlan,
    config: DecisionConfig,
) -> ReferenceOutcome:
    """Enumerate every tiny selection and compare the heuristic to the optimum."""
    scores = score_evidence(
        extract_features(pool), config.scoring, intent=policy.intent
    )
    score_by_observation = {item.observation_id: item.score.total for item in scores}
    contributions_by_observation = {
        item.observation_id: item.score.contributions for item in scores
    }
    groups: tuple[tuple[EvidenceVariant, ...], ...] = tuple(
        variants
        for observation in pool.observations
        if (variants := pool.variants_for(observation.observation_id))
    )
    choices = 1
    for variants in groups:
        choices *= len(variants) + 1
    if choices > MAX_REFERENCE_CHOICES:
        raise ContractError(
            "reference enumeration is bounded to tiny pools: "
            f"{choices} choices exceed {MAX_REFERENCE_CHOICES}"
        )
    available = config.delivery.available_chars()
    feasible: list[tuple[int, int, tuple[str, ...]]] = []
    for combo in product(*(tuple(variants) + (None,) for variants in groups)):
        selected = tuple(
            SelectedEvidence(
                variant=variant,
                reason=REASON_RELEVANCE,
                score=score_by_observation.get(variant.observation_id, 0),
                contributions=contributions_by_observation.get(
                    variant.observation_id, ()
                ),
            )
            for variant in combo
            if variant is not None
        )
        measured = selection_cost(selected, config.output_format)
        if measured > available:
            continue
        plan = _plan(selected, measured, available)
        assessment = assess_selected_evidence(policy, collection, pool, plan)
        if assessment.unsatisfied(required_only=True):
            continue
        variant_ids = tuple(sorted(item.variant.variant_id for item in selected))
        feasible.append(
            (_utility(variant_ids, pool, score_by_observation), plan.measured_cost, variant_ids)
        )
    if feasible:
        utility, measured_cost, variant_ids = min(
            feasible, key=lambda item: (-item[0], item[1], item[2])
        )
    else:
        utility, measured_cost, variant_ids = 0, 0, ()
    heuristic_plan = select_evidence(
        pool,
        scores,
        policy,
        config.delivery,
        output_format=config.output_format,
        profile=config.selection,
    )
    heuristic_ids = tuple(
        sorted(item.variant.variant_id for item in heuristic_plan.selected)
    )
    heuristic_assessment = assess_selected_evidence(
        policy, collection, pool, heuristic_plan
    )
    heuristic_feasible = (
        not heuristic_assessment.unsatisfied(required_only=True)
        and heuristic_plan.measured_cost <= available
    )
    return ReferenceOutcome(
        utility=utility,
        variant_ids=variant_ids,
        measured_cost=measured_cost,
        choices=choices,
        heuristic_utility=_utility(heuristic_ids, pool, score_by_observation),
        heuristic_variant_ids=heuristic_ids,
        heuristic_feasible=heuristic_feasible,
    )
