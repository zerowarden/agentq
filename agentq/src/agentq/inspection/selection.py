"""Deterministic constrained selection and policy assessment.

Selection reserves required evidence first, then fills remaining capacity by
relevance under the measured delivery budget. It never performs hidden
searches, and it records a selection reason separately from the relevance
score. This baseline makes no optimality claim; diversity and overlap handling
are refined in M1.4 within the same contract.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from agentq.core import require_bool, require_str

from .budgeting import DeliveryBudget, evidence_cost
from .contracts import (
    CollectionStatus,
    EvidencePolicy,
    EvidencePool,
    EvidenceRequirement,
    EvidenceVariant,
    Fidelity,
    Observation,
    OmittedEvidence,
    PolicyAssessment,
    RepresentationKind,
    RequirementAssessment,
    RequirementRule,
    RequirementStatus,
    ScoredEvidence,
    SelectedEvidence,
    SelectionPlan,
)

SELECTION_PROFILE = "selection-v0"
ASSESSMENT_PROFILE = "assessment-v0"
REASON_REQUIRED = "required"
REASON_RELEVANCE = "relevance"
OMISSION_REDUNDANT = "redundant"
OMISSION_BUDGET = "delivery_budget"
OMISSION_NO_REPRESENTATION = "no_representation"

SUCCESSFUL_COLLECTION = frozenset(
    {
        CollectionStatus.COMPLETED,
        CollectionStatus.EMPTY,
        CollectionStatus.PARTIAL,
    }
)

FIDELITY_RANK = {Fidelity.EXACT: 0, Fidelity.BOUNDED: 1, Fidelity.SUMMARY: 2}
REPRESENTATION_RANK = {
    RepresentationKind.EXACT_SOURCE: 0,
    RepresentationKind.SIGNATURE: 1,
    RepresentationKind.REFERENCE: 2,
    RepresentationKind.EXCERPT: 3,
    RepresentationKind.OUTLINE: 4,
    RepresentationKind.PACKAGE: 5,
}


@dataclass(frozen=True)
class SelectionProfile:
    """A versioned selector configuration; not an optimality guarantee."""

    profile: str = SELECTION_PROFILE
    reserve_required: bool = True
    fill_by_score: bool = True

    def __post_init__(self) -> None:
        require_str(self.profile, "selection profile id")
        require_bool(self.reserve_required, "selection reserve_required")
        require_bool(self.fill_by_score, "selection fill_by_score")

    def to_wire(self) -> dict[str, Any]:
        return {
            "profile": self.profile,
            "reserve_required": self.reserve_required,
            "fill_by_score": self.fill_by_score,
        }


DEFAULT_SELECTION = SelectionProfile()


@dataclass
class _SelectionState:
    available: int
    output_format: str
    selected: list[SelectedEvidence] = field(default_factory=list[SelectedEvidence])
    omitted: list[OmittedEvidence] = field(default_factory=list[OmittedEvidence])
    chosen: dict[str, EvidenceVariant] = field(
        default_factory=dict[str, EvidenceVariant]
    )
    reserved: list[str] = field(default_factory=list[str])
    cost: int = 0

    def fits(self, variant: EvidenceVariant) -> bool:
        return self.cost + evidence_cost(variant, self.output_format) <= self.available

    def take(
        self,
        observation_id: str,
        variant: EvidenceVariant,
        reason: str,
        score: int,
        requirement_id: str | None = None,
    ) -> None:
        self.selected.append(
            SelectedEvidence(
                variant=variant,
                reason=reason,
                score=score,
                requirement_id=requirement_id,
            )
        )
        self.chosen[observation_id] = variant
        self.cost += evidence_cost(variant, self.output_format)

    def omit(self, observation_id: str, variant_id: str | None, reason: str) -> None:
        self.omitted.append(OmittedEvidence(observation_id, variant_id, reason))


def select_evidence(
    pool: EvidencePool,
    scores: tuple[ScoredEvidence, ...],
    policy: EvidencePolicy,
    budget: DeliveryBudget,
    *,
    output_format: str = "text",
    profile: SelectionProfile = DEFAULT_SELECTION,
) -> SelectionPlan:
    """Reserve required evidence, then fill capacity by relevance per cost."""
    state = _SelectionState(
        available=budget.available_chars(), output_format=output_format
    )
    score_by_observation = {item.observation_id: item.score.total for item in scores}
    if profile.reserve_required:
        _reserve_required(state, pool, policy, score_by_observation)
    if profile.fill_by_score:
        _fill_by_score(state, pool, score_by_observation)
    _record_redundant(state, pool)
    return SelectionPlan(
        profile=profile.profile,
        selected=tuple(state.selected),
        omitted=tuple(state.omitted),
        reserved=tuple(state.reserved),
        measured_cost=state.cost,
        budget_chars=state.available,
    )


def _reserve_required(
    state: _SelectionState,
    pool: EvidencePool,
    policy: EvidencePolicy,
    score_by_observation: dict[str, int],
) -> None:
    for requirement in policy.required():
        candidates = _reservation_candidates(
            pool, score_by_observation, requirement, state.chosen
        )
        if not candidates:
            continue
        if _reserve_requirement(state, candidates, requirement, score_by_observation):
            state.reserved.append(requirement.requirement_id)


def _reserve_requirement(
    state: _SelectionState,
    candidates: tuple[tuple[str, EvidenceVariant], ...],
    requirement: EvidenceRequirement,
    score_by_observation: dict[str, int],
) -> bool:
    """Take the smallest acceptable representation that fits the budget."""
    for observation_id, variant in candidates:
        if observation_id in state.chosen:
            return True
        if not state.fits(variant):
            continue
        state.take(
            observation_id,
            variant,
            REASON_REQUIRED,
            score_by_observation.get(observation_id, 0),
            requirement_id=requirement.requirement_id,
        )
        return True
    observation_id, variant = candidates[0]
    state.omit(observation_id, variant.variant_id, OMISSION_BUDGET)
    return False


def _fill_by_score(
    state: _SelectionState,
    pool: EvidencePool,
    score_by_observation: dict[str, int],
) -> None:
    for observation in _ordered_observations(pool, score_by_observation):
        if observation.observation_id in state.chosen:
            continue
        variants = pool.variants_for(observation.observation_id)
        if not variants:
            state.omit(observation.observation_id, None, OMISSION_NO_REPRESENTATION)
            continue
        variant = _best_variant(variants)
        if not state.fits(variant):
            state.omit(observation.observation_id, variant.variant_id, OMISSION_BUDGET)
            continue
        state.take(
            observation.observation_id,
            variant,
            REASON_RELEVANCE,
            score_by_observation.get(observation.observation_id, 0),
        )


def _record_redundant(state: _SelectionState, pool: EvidencePool) -> None:
    for observation_id, variant in state.chosen.items():
        for candidate in pool.variants_for(observation_id):
            if candidate.variant_id != variant.variant_id:
                state.omit(observation_id, candidate.variant_id, OMISSION_REDUNDANT)


def _ordered_observations(
    pool: EvidencePool, score_by_observation: dict[str, int]
) -> tuple[Observation, ...]:
    return tuple(
        sorted(
            pool.observations,
            key=lambda observation: (
                -score_by_observation.get(observation.observation_id, 0),
                observation.observation_id,
            ),
        )
    )


def _best_variant(variants: tuple[EvidenceVariant, ...]) -> EvidenceVariant:
    return min(variants, key=_variant_preference)


def _variant_preference(variant: EvidenceVariant) -> tuple[int, int, str]:
    return (
        FIDELITY_RANK[variant.fidelity],
        REPRESENTATION_RANK.get(variant.representation, 99),
        variant.variant_id,
    )


def _reservation_candidates(
    pool: EvidencePool,
    score_by_observation: dict[str, int],
    requirement: EvidenceRequirement,
    chosen: dict[str, EvidenceVariant],
) -> tuple[tuple[str, EvidenceVariant], ...]:
    """Acceptable representations in preference order; best fit wins."""
    candidates: list[tuple[str, EvidenceVariant]] = []
    for observation in _ordered_observations(pool, score_by_observation):
        if requirement.acceptable_kinds and (
            observation.kind not in requirement.acceptable_kinds
        ):
            continue
        existing = chosen.get(observation.observation_id)
        if existing is not None:
            if _variant_satisfies(pool, existing, requirement):
                candidates.append((observation.observation_id, existing))
            continue
        acceptable = sorted(
            (
                variant
                for variant in pool.variants_for(observation.observation_id)
                if _variant_satisfies(pool, variant, requirement)
            ),
            key=_variant_preference,
        )
        candidates.extend(
            (observation.observation_id, variant) for variant in acceptable
        )
    return tuple(candidates)


def _variant_satisfies(
    pool: EvidencePool, variant: EvidenceVariant, requirement: EvidenceRequirement
) -> bool:
    if (
        requirement.representations
        and variant.representation not in requirement.representations
    ):
        return False
    if (
        requirement.rule is RequirementRule.EXACT_SOURCE
        and variant.fidelity is not Fidelity.EXACT
    ):
        return False
    observation = pool.observation(variant.observation_id)
    if observation is None:
        return False
    return not (
        requirement.acceptable_kinds
        and observation.kind not in requirement.acceptable_kinds
    )


def assess_selected_evidence(
    policy: EvidencePolicy, pool: EvidencePool, selection: SelectionPlan
) -> PolicyAssessment:
    """Assess requirements against selected evidence, not just the pool."""
    selected_variant_ids = frozenset(
        item.variant.variant_id for item in selection.selected
    )
    selected_observations = frozenset(
        item.observation_id for item in selection.selected
    )
    assessments = tuple(
        _assess(requirement, pool, selected_observations, selected_variant_ids)
        for requirement in policy.requirements
    )
    return PolicyAssessment(profile=ASSESSMENT_PROFILE, requirements=assessments)


def _assess(
    requirement: EvidenceRequirement,
    pool: EvidencePool,
    selected_observations: frozenset[str],
    selected_variant_ids: frozenset[str],
) -> RequirementAssessment:
    if requirement.rule is RequirementRule.COLLECTION_OUTCOME:
        return _assess_collection_outcome(requirement, pool)
    admissible = tuple(
        observation
        for observation in pool.observations
        if not requirement.acceptable_kinds
        or observation.kind in requirement.acceptable_kinds
    )
    if requirement.rule is RequirementRule.EXACT_SOURCE:
        return _assess_exact_source(requirement, pool, admissible, selected_variant_ids)
    if requirement.rule is RequirementRule.REPRESENTATIVE_EVIDENCE:
        return _assess_representative(requirement, admissible, selected_observations)
    return _assess_minimum(requirement, admissible, selected_observations)


def _assess_minimum(
    requirement: EvidenceRequirement,
    admissible: tuple[Observation, ...],
    selected_observations: frozenset[str],
) -> RequirementAssessment:
    supporting = tuple(
        observation.observation_id
        for observation in admissible
        if observation.observation_id in selected_observations
    )
    if supporting:
        return RequirementAssessment(
            requirement_id=requirement.requirement_id,
            status=RequirementStatus.SATISFIED,
            strength=requirement.strength,
            supporting=supporting,
        )
    detail = (
        "admissible evidence was acquired but not selected"
        if admissible
        else "no admissible evidence was acquired"
    )
    return RequirementAssessment(
        requirement_id=requirement.requirement_id,
        status=RequirementStatus.UNSATISFIED,
        strength=requirement.strength,
        detail=detail,
    )


def _assess_exact_source(
    requirement: EvidenceRequirement,
    pool: EvidencePool,
    admissible: tuple[Observation, ...],
    selected_variant_ids: frozenset[str],
) -> RequirementAssessment:
    supporting: list[str] = []
    acquired_exact = False
    for observation in admissible:
        for variant in pool.variants_for(observation.observation_id):
            if not _variant_satisfies(pool, variant, requirement):
                continue
            acquired_exact = True
            if variant.variant_id in selected_variant_ids:
                supporting.append(observation.observation_id)
    if supporting:
        return RequirementAssessment(
            requirement_id=requirement.requirement_id,
            status=RequirementStatus.SATISFIED,
            strength=requirement.strength,
            supporting=tuple(dict.fromkeys(supporting)),
        )
    detail = (
        "an exact source representation was acquired but not selected"
        if acquired_exact
        else "no exact source representation was acquired"
    )
    return RequirementAssessment(
        requirement_id=requirement.requirement_id,
        status=RequirementStatus.UNSATISFIED,
        strength=requirement.strength,
        detail=detail,
    )


def _assess_representative(
    requirement: EvidenceRequirement,
    admissible: tuple[Observation, ...],
    selected_observations: frozenset[str],
) -> RequirementAssessment:
    if not admissible:
        return RequirementAssessment(
            requirement_id=requirement.requirement_id,
            status=RequirementStatus.SATISFIED,
            strength=requirement.strength,
            detail="no admissible evidence was acquired",
        )
    supporting = tuple(
        observation.observation_id
        for observation in admissible
        if observation.observation_id in selected_observations
    )
    if supporting:
        return RequirementAssessment(
            requirement_id=requirement.requirement_id,
            status=RequirementStatus.SATISFIED,
            strength=requirement.strength,
            supporting=supporting,
        )
    return RequirementAssessment(
        requirement_id=requirement.requirement_id,
        status=RequirementStatus.UNSATISFIED,
        strength=requirement.strength,
        detail="admissible evidence was acquired but omitted from the bundle",
    )


def _assess_collection_outcome(
    requirement: EvidenceRequirement, pool: EvidencePool
) -> RequirementAssessment:
    if requirement.capability is None:
        return RequirementAssessment(
            requirement_id=requirement.requirement_id,
            status=RequirementStatus.NOT_APPLICABLE,
            strength=requirement.strength,
            detail="the requirement names no capability",
        )
    records = pool.acquisitions_for(requirement.capability)
    if not records:
        return RequirementAssessment(
            requirement_id=requirement.requirement_id,
            status=RequirementStatus.UNSATISFIED,
            strength=requirement.strength,
            detail="the requested acquisition was not attempted",
        )
    successful = tuple(
        record for record in records if record.status in SUCCESSFUL_COLLECTION
    )
    supporting = tuple(record.acquisition_id for record in records)
    if successful:
        return RequirementAssessment(
            requirement_id=requirement.requirement_id,
            status=RequirementStatus.SATISFIED,
            strength=requirement.strength,
            supporting=supporting,
        )
    return RequirementAssessment(
        requirement_id=requirement.requirement_id,
        status=RequirementStatus.UNSATISFIED,
        strength=requirement.strength,
        supporting=supporting,
        detail="the requested acquisition was reported unavailable or failed",
    )
