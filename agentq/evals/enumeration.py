"""Evaluation-only exact references for tiny selection problems.

Two references share one enumeration but answer different questions:

- :func:`reference_selection` is the surrogate-objective reference. Its utility
  is the sum of the scores the evaluated scorer assigns, so it measures how
  well a selection serves that scorer, not how well it serves the reviewer. It
  also models the selection stage only.
- :func:`judged_selection` is the independent-utility oracle. Its utility is
  the reviewed facets a selection satisfies, optional evidence the reviewer
  marked irrelevant is inadmissible, and its cost model is declared explicitly
  (:data:`DELIVERY_COST_MODEL`).

Both enumerate at most one variant per observation and keep only selections
that satisfy every required policy requirement and fit their cost model.
Overlap, per-file quotas, and whole-delivery fitting remain production
heuristics outside both models, and optimality is claimed only for these models
and only for tiny pools.
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
    observation_matches,
    select_evidence,
)

from .metrics import facet_supported, judged_variant_ids
from .models import JudgmentSet

REFERENCE_PROFILE = "reference-exact-v1"
JUDGED_PROFILE = "judged-exact-v1"
MAX_REFERENCE_CHOICES = 4096
DELIVERY_COST_MODEL = (
    "selection_cost(selected, output_format) + delivery.envelope_chars "
    "<= delivery.max_chars"
)


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


def _variant_groups(
    pool: EvidencePool,
) -> tuple[tuple[EvidenceVariant, ...], ...]:
    """Every observation's admissible variants, in pool order."""
    return tuple(
        variants
        for observation in pool.observations
        if observation.observation_id not in pool.unstable_observation_ids
        if (variants := pool.variants_for(observation.observation_id))
    )


def _choices_bound(
    groups: tuple[tuple[EvidenceVariant, ...], ...], what: str
) -> int:
    choices = 1
    for variants in groups:
        choices *= len(variants) + 1
    if choices > MAX_REFERENCE_CHOICES:
        raise ContractError(
            f"{what} enumeration is bounded to tiny pools: "
            f"{choices} choices exceed {MAX_REFERENCE_CHOICES}"
        )
    return choices


def _selection_cost_within(
    selected: tuple[SelectedEvidence, ...], config: DecisionConfig
) -> int | None:
    """The selection cost under the declared delivery model, or ``None``."""
    cost = selection_cost(selected, config.output_format)
    if cost + config.delivery.envelope_chars > config.delivery.max_chars:
        return None
    return cost


def _required_satisfied(
    selected: tuple[SelectedEvidence, ...],
    cost: int,
    policy: EvidencePolicy,
    collection: CollectionPlan,
    pool: EvidencePool,
    config: DecisionConfig,
) -> bool:
    plan = _plan(selected, cost, config.delivery.available_chars())
    assessment = assess_selected_evidence(policy, collection, pool, plan)
    return not assessment.unsatisfied(required_only=True)


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
    groups = _variant_groups(pool)
    choices = _choices_bound(groups, "reference")
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
        measured = _selection_cost_within(selected, config)
        if measured is None:
            continue
        if not _required_satisfied(
            selected, measured, policy, collection, pool, config
        ):
            continue
        variant_ids = tuple(sorted(item.variant.variant_id for item in selected))
        feasible.append(
            (_utility(variant_ids, pool, score_by_observation), measured, variant_ids)
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


@dataclass(frozen=True)
class JudgedOutcome:
    """The judged-utility optimum for one tiny pool under independent labels."""

    critical_facets: int
    noncritical_facets: int
    variant_ids: tuple[str, ...]
    delivered_cost: int
    choices: int

    @property
    def utility(self) -> tuple[int, int]:
        return (self.critical_facets, self.noncritical_facets)


def _required_observations(
    policy: EvidencePolicy, pool: EvidencePool
) -> frozenset[str]:
    """Observations the required policy can draw on; they are never inadmissible."""
    requirements = policy.required()
    return frozenset(
        observation.observation_id
        for observation in pool.observations
        if any(
            observation_matches(requirement, observation)
            for requirement in requirements
        )
    )


def _judged_utility(
    judgments: JudgmentSet, selected_ids: frozenset[str]
) -> tuple[int, int]:
    """Reviewed facets the selection satisfies, critical first."""
    critical = sum(
        1
        for facet in judgments.facets
        if facet.critical and facet_supported(facet, judgments, selected_ids)
    )
    noncritical = sum(
        1
        for facet in judgments.facets
        if not facet.critical and facet_supported(facet, judgments, selected_ids)
    )
    return critical, noncritical


def judged_selection(
    pool: EvidencePool,
    policy: EvidencePolicy,
    collection: CollectionPlan,
    config: DecisionConfig,
    judgments: JudgmentSet,
) -> JudgedOutcome:
    """Enumerate every tiny selection under independent reviewer judgments.

    Unlike :func:`reference_selection`, utility is not the evaluated scorer's
    own sum: it counts the reviewed facets the selection satisfies. Optional
    evidence the reviewer marked irrelevant is inadmissible (required evidence
    is exempt), and cost uses the declared delivery model rather than the
    selection-stage estimate alone.
    """
    groups = _variant_groups(pool)
    choices = _choices_bound(groups, "judged")
    required_observations = _required_observations(policy, pool)
    _, irrelevant = judged_variant_ids(judgments)
    variant_observation = {
        variant.variant_id: variant.observation_id for variant in pool.variants
    }
    feasible: list[tuple[tuple[int, int], int, tuple[str, ...]]] = []
    for combo in product(*(tuple(variants) + (None,) for variants in groups)):
        selected = tuple(
            SelectedEvidence(variant=variant, reason=REASON_RELEVANCE, score=0)
            for variant in combo
            if variant is not None
        )
        cost = _selection_cost_within(selected, config)
        if cost is None:
            continue
        if not _required_satisfied(
            selected, cost, policy, collection, pool, config
        ):
            continue
        delivered = {item.variant.variant_id for item in selected}
        if any(
            variant_id in irrelevant
            and variant_observation.get(variant_id) not in required_observations
            for variant_id in delivered
        ):
            continue
        utility = _judged_utility(judgments, frozenset(delivered))
        feasible.append((utility, cost, tuple(sorted(delivered))))
    if feasible:
        (critical, noncritical), cost, variant_ids = min(
            feasible,
            key=lambda item: (-item[0][0], -item[0][1], item[1], item[2]),
        )
    else:
        critical = noncritical = cost = 0
        variant_ids = ()
    return JudgedOutcome(
        critical_facets=critical,
        noncritical_facets=noncritical,
        variant_ids=variant_ids,
        delivered_cost=cost,
        choices=choices,
    )
