"""Inspection contract validation: targets, identity, evidence, and decisions."""

from __future__ import annotations

import unittest
from pathlib import Path

from agentq.core import COMPLETE, ContractError, SourceRef, typed_coverage
from agentq.inspection.contracts import (
    AmbiguousTarget,
    Binding,
    CandidateTarget,
    Capability,
    CapabilityResult,
    CollectionPlan,
    CollectionRequest,
    CollectionStatus,
    DeclarationCandidate,
    DeclarationPayload,
    EvidenceFeatures,
    EvidencePool,
    EvidenceRole,
    Fidelity,
    InspectionRequest,
    Intent,
    LocationTarget,
    Observation,
    ObservationKind,
    PathTarget,
    RangeTarget,
    RepresentationKind,
    RequirementRule,
    RequirementStrength,
    ScoreBreakdown,
    ScoreContribution,
    SelectedEvidence,
    SelectionPlan,
    SourceSpan,
    SymbolTarget,
    inspection_request_identity,
    make_declaration_candidate,
    make_observation,
    make_variant,
)
from agentq.inspection.scoring import ScoringProfile, score_evidence

REPO = Path("/repo")


def _candidate(
    *,
    path: str = "src/a.ts",
    line: int = 2,
    end_line: int = 4,
    signature: str = "function foo()",
) -> DeclarationCandidate:
    return make_declaration_candidate(
        provider="fake",
        path=path,
        source_version="v1",
        kind="function",
        span=SourceSpan(start_line=line, end_line=end_line),
        signature=signature,
    )


def _observation(symbol: str = "foo") -> Observation:
    span = SourceSpan(start_line=2, end_line=4)
    return make_observation(
        kind=ObservationKind.DECLARATION,
        payload=DeclarationPayload(
            name=symbol, kind="function", signature="function foo()", span=span
        ),
        source=SourceRef(path="src/a.ts", start_line=2, end_line=4, symbol=symbol),
    )


class TargetValidationTests(unittest.TestCase):
    def test_symbol_name_is_required(self) -> None:
        with self.assertRaises(ContractError):
            SymbolTarget(name="")

    def test_absolute_and_parent_paths_are_rejected(self) -> None:
        for path in ("/etc/passwd", "../outside.ts", "src/../outside.ts"):
            with self.subTest(path=path):
                with self.assertRaises(ContractError):
                    PathTarget(path=path)

    def test_location_requires_one_based_coordinates(self) -> None:
        with self.assertRaises(ContractError):
            LocationTarget(path="src/a.ts", line=0, column=1)
        with self.assertRaises(ContractError):
            LocationTarget(path="src/a.ts", line=1, column=0)

    def test_range_requires_at_least_one_span(self) -> None:
        with self.assertRaises(ContractError):
            RangeTarget(path="src/a.ts", ranges=())

    def test_span_rejects_reversed_lines(self) -> None:
        with self.assertRaises(ContractError):
            SourceSpan(start_line=10, end_line=9)

    def test_span_column_requires_start_column(self) -> None:
        with self.assertRaises(ContractError):
            SourceSpan(start_line=10, end_line=10, end_column=4)

    def test_same_line_span_requires_ordered_columns(self) -> None:
        with self.assertRaises(ContractError):
            SourceSpan(start_line=10, end_line=10, start_column=8, end_column=8)
        SourceSpan(start_line=10, end_line=10, start_column=8, end_column=9)

    def test_line_only_span_is_not_a_cursor(self) -> None:
        span = SourceSpan(start_line=40, end_line=40)
        self.assertTrue(span.is_line_only())
        self.assertEqual(
            span.to_wire(),
            {
                "start_line": 40,
                "start_column": None,
                "end_line": 40,
                "end_column": None,
            },
        )

    def test_candidate_target_requires_id_and_symbol(self) -> None:
        with self.assertRaises(ContractError):
            CandidateTarget(candidate_id="", symbol="foo")
        with self.assertRaises(ContractError):
            CandidateTarget(candidate_id="cand-1", symbol="")


class RequestValidationTests(unittest.TestCase):
    def test_invalid_intent_is_rejected(self) -> None:
        with self.assertRaises(ContractError):
            Intent.parse("delete")
        self.assertIs(Intent.parse("rename"), Intent.RENAME)

    def test_request_rejects_invalid_evidence_scope(self) -> None:
        with self.assertRaises(ContractError):
            InspectionRequest(
                target=SymbolTarget(name="foo"), evidence_scopes=("/abs",)
            )
        with self.assertRaises(ContractError):
            InspectionRequest(
                target=SymbolTarget(name="foo"), evidence_scopes=("src", "src")
            )

    def test_identity_depends_on_semantics_not_presentation(self) -> None:
        base = InspectionRequest(target=SymbolTarget(name="foo"), intent=Intent.EDIT)
        self.assertEqual(
            inspection_request_identity(base, REPO),
            inspection_request_identity(
                InspectionRequest(target=SymbolTarget(name="foo"), intent=Intent.EDIT),
                REPO,
            ),
        )
        self.assertNotEqual(
            inspection_request_identity(base, REPO),
            inspection_request_identity(
                InspectionRequest(
                    target=SymbolTarget(name="foo"), intent=Intent.RENAME
                ),
                REPO,
            ),
        )
        self.assertNotEqual(
            inspection_request_identity(base, REPO),
            inspection_request_identity(
                InspectionRequest(
                    target=SymbolTarget(name="foo"),
                    intent=Intent.EDIT,
                    evidence_scopes=("src",),
                ),
                REPO,
            ),
        )


class EvidenceContractTests(unittest.TestCase):
    def test_observation_identity_is_deterministic(self) -> None:
        self.assertEqual(_observation().observation_id, _observation().observation_id)
        self.assertNotEqual(
            _observation("foo").observation_id, _observation("bar").observation_id
        )

    def test_variant_identity_is_deterministic(self) -> None:
        observation = _observation()
        first = make_variant(
            observation_id=observation.observation_id,
            representation=RepresentationKind.SIGNATURE,
            fidelity=Fidelity.SUMMARY,
            source=observation.source,
            text="function foo()",
        )
        second = make_variant(
            observation_id=observation.observation_id,
            representation=RepresentationKind.SIGNATURE,
            fidelity=Fidelity.SUMMARY,
            source=observation.source,
            text="function foo()",
        )
        self.assertEqual(first.variant_id, second.variant_id)

    def test_result_status_and_payload_stay_consistent(self) -> None:
        with self.assertRaises(ContractError):
            CapabilityResult(
                status=CollectionStatus.EMPTY, observations=(_observation(),)
            )
        with self.assertRaises(ContractError):
            CapabilityResult(
                status=CollectionStatus.UNAVAILABLE,
                coverage=typed_coverage(COMPLETE),
            )

    def test_pool_exposes_observations_and_variants(self) -> None:
        observation = _observation()
        pool = EvidencePool(request_id="req-1", observations=(observation,))
        self.assertEqual(pool.observation(observation.observation_id), observation)
        self.assertEqual(pool.variants_for(observation.observation_id), ())


class ScoreContractTests(unittest.TestCase):
    def test_total_must_match_contributions(self) -> None:
        with self.assertRaises(ContractError):
            ScoreBreakdown(
                observation_id="obs-1",
                total=5,
                contributions=(ScoreContribution(name="role_priority", value=3),),
                profile="p",
            )

    def test_binding_bonus_applies_only_to_relationship_evidence(self) -> None:
        reference = EvidenceFeatures(
            observation_id="obs-ref",
            role=EvidenceRole.REFERENCE,
            observation_kind=ObservationKind.SEMANTIC_REFERENCE,
            binding=Binding.RESOLVED,
        )
        declaration = EvidenceFeatures(
            observation_id="obs-decl",
            role=EvidenceRole.DECLARATION,
            observation_kind=ObservationKind.DECLARATION,
            binding=Binding.RESOLVED,
        )
        profile = ScoringProfile(profile="test-v1", binding_bonus=2)
        scored = {
            item.observation_id: item
            for item in score_evidence(
                (reference, declaration), profile, intent=Intent.EDIT
            )
        }
        self.assertEqual(scored["obs-ref"].score.contribution("binding_resolved"), 2)
        self.assertIsNone(scored["obs-decl"].score.contribution("binding_resolved"))

    def test_role_priority_arithmetic_is_named(self) -> None:
        features = EvidenceFeatures(
            observation_id="obs-ref",
            role=EvidenceRole.REFERENCE,
            observation_kind=ObservationKind.SEMANTIC_REFERENCE,
            binding=Binding.RESOLVED,
        )
        profile = ScoringProfile(
            profile="test-v1",
            binding_bonus=2,
            intent_priorities=((Intent.UNDERSTAND, ((EvidenceRole.REFERENCE, 6),)),),
        )
        scored = score_evidence((features,), profile, intent=Intent.UNDERSTAND)[0]
        self.assertEqual(scored.score.total, 8)
        self.assertEqual(scored.score.contribution("reference_priority"), 6)
        self.assertEqual(scored.score.contribution("binding_resolved"), 2)

    def test_unknown_features_score_zero(self) -> None:
        features = EvidenceFeatures(
            observation_id="obs-unknown",
            role=None,
            observation_kind=ObservationKind.DECLARATION,
        )
        profile = ScoringProfile(profile="test-v1")
        self.assertEqual(
            score_evidence((features,), profile, intent=Intent.EDIT)[0].score.total,
            0,
        )


class DecisionContractTests(unittest.TestCase):
    def test_measured_cost_cannot_exceed_budget(self) -> None:
        observation = _observation()
        variant = make_variant(
            observation_id=observation.observation_id,
            representation=RepresentationKind.SIGNATURE,
            fidelity=Fidelity.SUMMARY,
            source=observation.source,
            text="x",
        )
        with self.assertRaises(ContractError):
            SelectionPlan(
                profile="selection-v0",
                selected=(SelectedEvidence(variant=variant, reason="required"),),
                measured_cost=10,
                budget_chars=5,
            )

    def test_ambiguous_target_requires_candidates(self) -> None:
        with self.assertRaises(ContractError):
            AmbiguousTarget(
                target=SymbolTarget(name="foo"),
                candidates=(),
                candidate_total=0,
                count_quality="exact",
            )

    def test_collection_plan_matches_requirements_to_capabilities(self) -> None:
        plan = CollectionPlan(
            profile="collection-v0",
            request_id="req-1",
            target=SymbolTarget(name="foo"),
            requests=(
                CollectionRequest(
                    request_id="collect-1",
                    capability=Capability.FIND_DECLARATIONS,
                    role=EvidenceRole.DECLARATION,
                    requirement_id="declaration_identity",
                ),
            ),
        )
        self.assertEqual(plan.capabilities(), (Capability.FIND_DECLARATIONS,))

    def test_candidate_identity_depends_on_source_version(self) -> None:
        first = _candidate()
        second = make_declaration_candidate(
            provider="fake",
            path="src/a.ts",
            source_version="v2",
            kind="function",
            span=SourceSpan(start_line=2, end_line=4),
            signature="function foo()",
        )
        self.assertNotEqual(first.candidate_id, second.candidate_id)

    def test_requirement_vocabulary_is_explicit(self) -> None:
        self.assertEqual(RequirementStrength.REQUIRED.value, "required")
        self.assertEqual(RequirementRule.EXACT_SOURCE.value, "exact_source")
