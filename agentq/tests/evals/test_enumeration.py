"""Tiny exact reference: the optimality model and heuristic gaps."""

from __future__ import annotations

import unittest
from dataclasses import replace

from agentq.core import ContractError, SourceRef
from agentq.inspection.budgeting import DeliveryBudget
from agentq.inspection.contracts import (
    Binding,
    Capability,
    EvidencePolicy,
    EvidencePool,
    EvidenceRequirement,
    EvidenceRole,
    EvidenceVariant,
    Fidelity,
    Intent,
    MentionPayload,
    Observation,
    ObservationKind,
    ReferencePayload,
    RepresentationKind,
    RequirementRule,
    RequirementStrength,
    SelectedEvidence,
    SourceSpan,
    TargetKind,
    make_observation,
    make_variant,
)
from agentq.inspection.decision import DecisionConfig
from agentq.inspection.features import extract_features
from agentq.inspection.rendering import selected_cost
from agentq.inspection.scoring import DEFAULT_SCORING, score_evidence
from agentq.inspection.selection import (
    REASON_RELEVANCE,
    REASON_ROLE,
    SelectionProfile,
)
from evals.enumeration import judged_selection, reference_selection
from evals.models import JudgmentFacet, JudgmentSet, JudgmentWitness
from tests.support.inspection_fixtures import collection_plan

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


def _decoy(path: str) -> tuple[Observation, EvidenceVariant]:
    """A high-scoring, unjudged reference: the surrogate's favorite."""
    observation = make_observation(
        kind=ObservationKind.SEMANTIC_REFERENCE,
        payload=ReferencePayload(
            relationship="calls", text="use(target)", binding=Binding.RESOLVED
        ),
        source=SourceRef(path=path, start_line=4, end_line=4),
    )
    variant = make_variant(
        observation_id=observation.observation_id,
        representation=RepresentationKind.REFERENCE,
        fidelity=Fidelity.EXACT,
        source=observation.source,
        text="use(target)",
        span=SourceSpan(start_line=4, end_line=4),
    )
    return observation, variant


def _relevant_mention(path: str) -> tuple[Observation, EvidenceVariant]:
    observation = make_observation(
        kind=ObservationKind.LEXICAL_MENTION,
        payload=MentionPayload(text="target"),
        source=SourceRef(path=path, start_line=4, end_line=4),
    )
    variant = make_variant(
        observation_id=observation.observation_id,
        representation=RepresentationKind.REFERENCE,
        fidelity=Fidelity.BOUNDED,
        source=observation.source,
        text="target",
        span=SourceSpan(start_line=4, end_line=4),
    )
    return observation, variant


def _judgments(relevant: str, irrelevant: str = "") -> JudgmentSet:
    return JudgmentSet(
        case_id="case",
        capture_id="c" * 64,
        basis="target_intent",
        review_status="reviewed",
        facets=(
            JudgmentFacet(
                facet_id="facet", critical=True, witness_sets=(("witness",),)
            ),
        ),
        witnesses=(JudgmentWitness("witness", (relevant,)),),
        irrelevant_variant_ids=(irrelevant,) if irrelevant else (),
    )


def _one_item_budget(
    pool: EvidencePool, *variants: EvidenceVariant
) -> DeliveryBudget:
    """A ceiling that fits exactly one scored variant, envelope included."""
    scores = {
        item.observation_id: item
        for item in score_evidence(
            extract_features(pool), DEFAULT_SCORING, intent=Intent.UNDERSTAND
        )
    }
    largest = max(
        selected_cost(
            SelectedEvidence(
                variant=variant,
                reason=REASON_RELEVANCE,
                score=scores[variant.observation_id].score.total,
                contributions=scores[variant.observation_id].score.contributions,
            ),
            "text",
        )
        for variant in variants
    )
    return DeliveryBudget(max_chars=largest + 10, envelope_chars=10)


class JudgedOracleTests(unittest.TestCase):

    def test_both_oracles_exclude_unstable_optional_evidence(self) -> None:
        observation, variant = _decoy("src/unstable.py")
        pool = replace(
            _pool(observation, (variant,)),
            unstable_observation_ids=(observation.observation_id,),
        )
        config = DecisionConfig(delivery=AMPLE)
        reference = reference_selection(pool, _policy(), collection_plan(), config)
        judged = judged_selection(
            pool,
            _policy(),
            collection_plan(),
            config,
            _judgments(variant.variant_id),
        )
        self.assertEqual(reference.variant_ids, ())
        self.assertEqual(reference.utility, 0)
        self.assertEqual(judged.variant_ids, ())
        self.assertEqual(judged.utility, (0, 0))

    def test_decoy_wins_the_surrogate_but_loses_the_independent_oracle(self) -> None:
        decoy, decoy_variant = _decoy("src/decoy.py")
        relevant, relevant_variant = _relevant_mention("src/relevant.py")
        pool = EvidencePool(
            request_id="req-1",
            observations=(decoy, relevant),
            variants=(decoy_variant, relevant_variant),
        )
        config = DecisionConfig(
            delivery=_one_item_budget(pool, decoy_variant, relevant_variant)
        )
        policy = _policy()
        surrogate = reference_selection(pool, policy, collection_plan(), config)
        self.assertEqual(surrogate.variant_ids, (decoy_variant.variant_id,))
        oracle = judged_selection(
            pool,
            policy,
            collection_plan(),
            config,
            _judgments(relevant_variant.variant_id),
        )
        self.assertEqual(oracle.variant_ids, (relevant_variant.variant_id,))
        self.assertEqual(oracle.utility, (1, 0))

    def test_oracle_rejects_inadmissible_irrelevant_evidence(self) -> None:
        decoy, decoy_variant = _decoy("src/decoy.py")
        relevant, relevant_variant = _relevant_mention("src/relevant.py")
        noise, noise_variant = _decoy("src/noise.py")
        pool = EvidencePool(
            request_id="req-1",
            observations=(decoy, relevant, noise),
            variants=(decoy_variant, relevant_variant, noise_variant),
        )
        config = DecisionConfig(
            delivery=_one_item_budget(pool, decoy_variant, relevant_variant, noise_variant)
        )
        oracle = judged_selection(
            pool,
            _policy(),
            collection_plan(),
            config,
            _judgments(relevant_variant.variant_id, noise_variant.variant_id),
        )
        self.assertEqual(oracle.variant_ids, (relevant_variant.variant_id,))
        self.assertNotIn(noise_variant.variant_id, oracle.variant_ids)


class ReferenceTests(unittest.TestCase):
    def test_reference_exposes_the_variant_fallback_gap(self) -> None:
        observation, (large, small) = _reference_pair("src/use.py", 4)
        pool = _pool(observation, (large, small))
        policy = _policy()
        baseline = replace(DecisionConfig(), delivery=_small_budget(pool, small))
        outcome = reference_selection(pool, policy, collection_plan(), baseline)
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
        closed = reference_selection(pool, policy, collection_plan(), challenger)
        self.assertEqual(closed.gap, 0)
        self.assertEqual(closed.heuristic_utility, closed.utility)

    def test_reference_reports_an_unsatisfiable_requirement(self) -> None:
        observation, (_, small) = _reference_pair("src/use.py", 4)
        pool = _pool(observation, (small,))
        outcome = reference_selection(
            pool, _policy(_exact_requirement()), collection_plan(), DecisionConfig()
        )
        self.assertEqual(outcome.utility, 0)
        self.assertEqual(outcome.variant_ids, ())
        self.assertFalse(outcome.heuristic_feasible)

    def test_duplicate_representations_do_not_multiply_utility(self) -> None:
        observation, (large, small) = _reference_pair("src/use.py", 4)
        pool = _pool(observation, (large, small))
        outcome = reference_selection(
            pool, _policy(), collection_plan(), DecisionConfig(delivery=AMPLE)
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
            pool, _policy(), collection_plan(), DecisionConfig(delivery=AMPLE)
        )
        second = reference_selection(
            pool, _policy(), collection_plan(), DecisionConfig(delivery=AMPLE)
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
                pool, _policy(), collection_plan(), DecisionConfig(delivery=AMPLE)
            )


if __name__ == "__main__":
    unittest.main()
