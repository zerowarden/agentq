"""Tiny exact reference: the optimality model and heuristic gaps."""

from __future__ import annotations

import unittest
from dataclasses import replace

from agentq.core import ContractError, SourceRef
from agentq.inspection.budgeting import DeliveryBudget
from agentq.inspection.contracts import (
    Capability,
    CollectionPlan,
    EvidencePolicy,
    EvidencePool,
    EvidenceRequirement,
    EvidenceRole,
    EvidenceVariant,
    Fidelity,
    Intent,
    Observation,
    ObservationKind,
    ReferencePayload,
    RepresentationKind,
    RequirementRule,
    RequirementStrength,
    SelectedEvidence,
    SourceSpan,
    SymbolTarget,
    TargetKind,
    make_observation,
    make_variant,
)
from agentq.inspection.decision import DecisionConfig
from agentq.inspection.features import extract_features
from agentq.inspection.rendering import selected_cost
from agentq.inspection.scoring import DEFAULT_SCORING, score_evidence
from agentq.inspection.selection import REASON_ROLE, SelectionProfile
from evals.enumeration import reference_selection

AMPLE = DeliveryBudget(max_chars=12_000, envelope_chars=200)


def _reference_pair(
    path: str, line: int
) -> tuple[Observation, tuple[EvidenceVariant, ...]]:
    observation = make_observation(
        kind=ObservationKind.SEMANTIC_REFERENCE,
        payload=ReferencePayload(relationship="reference", text="use(target)"),
        source=SourceRef(path=path, start_line=line, end_line=line),
    )
    large = make_variant(
        observation_id=observation.observation_id,
        representation=RepresentationKind.REFERENCE,
        fidelity=Fidelity.EXACT,
        source=observation.source,
        text="use(target) " + "x" * 400,
        span=SourceSpan(start_line=line, end_line=line),
    )
    small = make_variant(
        observation_id=observation.observation_id,
        representation=RepresentationKind.EXCERPT,
        fidelity=Fidelity.BOUNDED,
        source=observation.source,
        text="use(target)",
        span=SourceSpan(start_line=line, end_line=line),
    )
    return observation, (large, small)


def _pool(
    observation: Observation, variants: tuple[EvidenceVariant, ...]
) -> EvidencePool:
    return EvidencePool(
        request_id="req-1", observations=(observation,), variants=variants
    )


def _policy(*requirements: EvidenceRequirement) -> EvidencePolicy:
    return EvidencePolicy(
        profile="test-policy",
        intent=Intent.UNDERSTAND,
        target_kind=TargetKind.SYMBOL,
        requirements=tuple(requirements),
    )


def _collection() -> CollectionPlan:
    return CollectionPlan(
        profile="test-plan", request_id="req-1", target=SymbolTarget(name="target")
    )


def _exact_requirement() -> EvidenceRequirement:
    return EvidenceRequirement(
        requirement_id="target_source",
        role=EvidenceRole.TARGET_SOURCE,
        rule=RequirementRule.EXACT_SOURCE,
        strength=RequirementStrength.REQUIRED,
        capabilities=(Capability.READ_SOURCE,),
        acceptable_kinds=(ObservationKind.SEMANTIC_REFERENCE,),
        representations=(RepresentationKind.REFERENCE,),
    )


def _selected(
    variant: EvidenceVariant, pool: EvidencePool, reason: str
) -> SelectedEvidence:
    scores = score_evidence(
        extract_features(pool), DEFAULT_SCORING, intent=Intent.UNDERSTAND
    )
    found = next(
        item for item in scores if item.observation_id == variant.observation_id
    )
    return SelectedEvidence(
        variant=variant,
        reason=reason,
        score=found.score.total,
        contributions=found.score.contributions,
    )


def _small_budget(pool: EvidencePool, small: EvidenceVariant) -> DeliveryBudget:
    item = _selected(small, pool, REASON_ROLE)
    return DeliveryBudget(
        max_chars=selected_cost(item, "text") + 60, envelope_chars=10
    )


class ReferenceTests(unittest.TestCase):
    def test_reference_exposes_the_variant_fallback_gap(self) -> None:
        observation, (large, small) = _reference_pair("src/use.py", 4)
        pool = _pool(observation, (large, small))
        policy = _policy()
        baseline = replace(DecisionConfig(), delivery=_small_budget(pool, small))
        outcome = reference_selection(pool, policy, _collection(), baseline)
        self.assertGreater(outcome.gap, 0)
        self.assertEqual(outcome.heuristic_utility, 0)
        self.assertEqual(outcome.utility, DEFAULT_SCORING.priority(
            Intent.UNDERSTAND, EvidenceRole.REFERENCE
        ))
        self.assertEqual(outcome.variant_ids, (small.variant_id,))
        challenger = replace(
            baseline,
            selection=SelectionProfile(
                profile="selection-test-challenger", variant_fallback=True
            ),
        )
        closed = reference_selection(pool, policy, _collection(), challenger)
        self.assertEqual(closed.gap, 0)
        self.assertEqual(closed.heuristic_utility, closed.utility)

    def test_reference_reports_an_unsatisfiable_requirement(self) -> None:
        observation, (_, small) = _reference_pair("src/use.py", 4)
        pool = _pool(observation, (small,))
        outcome = reference_selection(
            pool, _policy(_exact_requirement()), _collection(), DecisionConfig()
        )
        self.assertEqual(outcome.utility, 0)
        self.assertEqual(outcome.variant_ids, ())
        self.assertFalse(outcome.heuristic_feasible)

    def test_duplicate_representations_do_not_multiply_utility(self) -> None:
        observation, (large, small) = _reference_pair("src/use.py", 4)
        pool = _pool(observation, (large, small))
        outcome = reference_selection(
            pool, _policy(), _collection(), DecisionConfig(delivery=AMPLE)
        )
        self.assertEqual(len(outcome.variant_ids), 1)
        self.assertEqual(
            outcome.utility,
            DEFAULT_SCORING.priority(Intent.UNDERSTAND, EvidenceRole.REFERENCE),
        )

    def test_reference_is_deterministic(self) -> None:
        observation, (large, small) = _reference_pair("src/use.py", 4)
        pool = _pool(observation, (large, small))
        first = reference_selection(
            pool, _policy(), _collection(), DecisionConfig(delivery=AMPLE)
        )
        second = reference_selection(
            pool, _policy(), _collection(), DecisionConfig(delivery=AMPLE)
        )
        self.assertEqual(first, second)

    def test_reference_rejects_pools_that_are_not_tiny(self) -> None:
        pairs = [_reference_pair(f"src/use{index}.py", 4) for index in range(12)]
        pool = EvidencePool(
            request_id="req-1",
            observations=tuple(observation for observation, _ in pairs),
            variants=tuple(
                variant for _, variants in pairs for variant in variants
            ),
        )
        with self.assertRaises(ContractError):
            reference_selection(
                pool, _policy(), _collection(), DecisionConfig(delivery=AMPLE)
            )


if __name__ == "__main__":
    unittest.main()
