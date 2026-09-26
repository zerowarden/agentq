"""Development-only scorer tuning: split isolation, reproducibility, attribution."""

from __future__ import annotations

import unittest
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory

from agentq.core import ContractError, SourceRef, typed_coverage
from agentq.inspection.budgeting import DeliveryBudget
from agentq.inspection.contracts import (
    Binding,
    EvidencePool,
    EvidenceVariant,
    Fidelity,
    Intent,
    Observation,
    ObservationKind,
    ReferencePayload,
    RepresentationKind,
    SourceSpan,
    make_observation,
    make_variant,
)
from agentq.inspection.decision import DecisionConfig
from agentq.inspection.scoring import DEFAULT_SCORING
from evals.codec import decode_config, encode_config
from evals.experiments import (
    CHANGE_MEMBERSHIP,
    CHANGE_ORDER,
    CHANGE_OUTPUT,
    CHANGE_UTILITY,
    DEVELOPMENT,
    DISCRIMINATION_AVAILABLE_FEATURES,
    DISCRIMINATION_CURRENT_SCORE,
    DISCRIMINATION_SCORER_INPUTS,
    HOLDOUT,
    VALIDATION,
    Candidate,
    CandidateResult,
    CaseObservation,
    SplitAssignments,
    TuningCase,
    changed_dimensions,
    choose_candidate,
    coordinate_search,
    declared_candidates,
    delivered_irrelevant,
    discrimination_findings,
    evaluate_candidate,
    load_splits,
    load_tuning_cases,
    pool_discrimination_findings,
    select_split,
    undelivered_facets,
)
from evals.models import (
    JudgmentFacet,
    JudgmentSet,
    JudgmentWitness,
    LockedCase,
    SuiteLock,
)
from evals.store import CaptureStore
from tests.evals.support import tuning_case


def _reference(
    path: str,
    *,
    relationship: str,
    binding: Binding,
    domain: str | None = None,
) -> tuple[Observation, EvidenceVariant]:
    observation = make_observation(
        kind=ObservationKind.SEMANTIC_REFERENCE,
        payload=ReferencePayload(
            relationship=relationship,
            text=f"use({path})",
            binding=binding,
            domain=domain,
        ),
        source=SourceRef(path=path, start_line=4, end_line=4),
    )
    variant = make_variant(
        observation_id=observation.observation_id,
        representation=RepresentationKind.REFERENCE,
        fidelity=Fidelity.BOUNDED,
        source=observation.source,
        text=f"use({path})",
        span=SourceSpan(start_line=4, end_line=4),
    )
    return observation, variant


def _test_mention(path: str) -> tuple[Observation, EvidenceVariant]:
    observation = make_observation(
        kind=ObservationKind.TEST_MENTION,
        payload=ReferencePayload(
            relationship="mentions", text=f"test({path})", domain="test"
        ),
        source=SourceRef(path=path, start_line=4, end_line=4),
    )
    variant = make_variant(
        observation_id=observation.observation_id,
        representation=RepresentationKind.REFERENCE,
        fidelity=Fidelity.BOUNDED,
        source=observation.source,
        text=f"test({path})",
        span=SourceSpan(start_line=4, end_line=4),
    )
    return observation, variant


def _pool(*pairs: tuple[Observation, EvidenceVariant]) -> EvidencePool:
    return EvidencePool(
        request_id="req",
        observations=tuple(observation for observation, _ in pairs),
        variants=tuple(variant for _, variant in pairs),
        coverage=typed_coverage("complete"),
    )


def _judgments(relevant: str, irrelevant: str) -> JudgmentSet:
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
        witnesses=(
            JudgmentWitness(witness_id="witness", acceptable_variant_ids=(relevant,)),
        ),
        irrelevant_variant_ids=(irrelevant,),
    )


def _result(
    label: str,
    critical: int,
    present: int,
    noncritical: int,
    distance: int,
    *,
    violations: int = 0,
    unmet: int = 0,
) -> CandidateResult:
    return CandidateResult(
        label=label,
        rationale="",
        scoring=DEFAULT_SCORING,
        critical_delivered=critical,
        critical_total=19,
        all_critical_present=present,
        cases_with_critical=11,
        noncritical_delivered=noncritical,
        noncritical_total=7,
        violations=violations,
        unmet=unmet,
        render_chars=0,
        distance=distance,
        changed_cases=(),
        observations=(),
    )


class SplitTests(unittest.TestCase):

    def test_overlapping_suites_cannot_reweight_an_experiment(self) -> None:
        with TemporaryDirectory() as temp:
            store = CaptureStore(Path(temp))
            case = tuning_case("basic-edit")
            locked = LockedCase(
                case.case_id,
                store.write_capture(case.capture),
                store.write_judgment(case.judgments),
            )
            for locks in (
                (SuiteLock("one", (locked,)), SuiteLock("one", (locked,))),
                (SuiteLock("one", (locked,)), SuiteLock("two", (locked,))),
            ):
                with self.subTest(suites=[lock.suite_id for lock in locks]):
                    with self.assertRaisesRegex(ContractError, "duplicate"):
                        load_tuning_cases(store, locks)

    def test_load_splits_rejects_unknown_schema_and_splits(self) -> None:
        with TemporaryDirectory() as temp:
            path = Path(temp) / "splits.json"
            path.write_text('{"schema": "wrong", "assignments": {}}')
            with self.assertRaises(ContractError):
                load_splits(path)
            path.write_text(
                '{"schema": "agentq.eval.splits/v1",'
                ' "assignments": {"a": "training"}}'
            )
            with self.assertRaises(ContractError):
                load_splits(path)

    def test_select_split_rejects_unlisted_cases(self) -> None:
        lock = SuiteLock(
            suite_id="suite",
            cases=(
                LockedCase(case_id="a", capture_id="a" * 64),
                LockedCase(case_id="b", capture_id="b" * 64),
                LockedCase(case_id="c", capture_id="c" * 64),
            ),
        )
        splits = SplitAssignments({"a": HOLDOUT, "b": VALIDATION})
        with self.assertRaisesRegex(ContractError, "missing explicit"):
            select_split(lock, splits, DEVELOPMENT)
        splits = SplitAssignments({"a": HOLDOUT, "b": VALIDATION, "c": DEVELOPMENT})
        self.assertEqual(
            [case.case_id for case in select_split(lock, splits, DEVELOPMENT).cases],
            ["c"],
        )
        self.assertEqual(
            [case.case_id for case in select_split(lock, splits, HOLDOUT).cases],
            ["a"],
        )

    def test_select_split_rejects_an_unknown_split(self) -> None:
        lock = SuiteLock(
            suite_id="suite", cases=(LockedCase(case_id="a", capture_id="a" * 64),)
        )
        with self.assertRaises(ContractError):
            select_split(lock, SplitAssignments({}), "training")

    def test_unjudged_and_holdout_cases_are_never_loaded(self) -> None:
        with TemporaryDirectory() as temp:
            store = CaptureStore(Path(temp))
            unjudged = SuiteLock(
                suite_id="suite",
                cases=(LockedCase(case_id="a", capture_id="a" * 64),),
            )
            self.assertEqual(load_tuning_cases(store, [unjudged]), ())
            holdout = SuiteLock(
                suite_id="suite",
                cases=(
                    LockedCase(
                        case_id="hold", capture_id="f" * 64, judgment_id="e" * 64
                    ),
                ),
            )
            splits = SplitAssignments({"hold": HOLDOUT})
            self.assertEqual(
                load_tuning_cases(
                    store, [select_split(holdout, splits, DEVELOPMENT)]
                ),
                (),
            )


class CandidateTests(unittest.TestCase):
    def test_delivery_failure_is_ineligible_without_an_explicit_expectation(self) -> None:
        case = tuning_case("basic-edit")
        case = replace(
            case,
            judgments=replace(case.judgments, expected_outcomes=()),
            delivery=DeliveryBudget(max_chars=1, envelope_chars=0),
        )
        result = evaluate_candidate(
            (case,), Candidate("failed", "", DEFAULT_SCORING)
        )
        self.assertEqual(result.violations, 0)
        self.assertEqual(result.unmet, 0)
        self.assertFalse(result.eligible())
        self.assertEqual(result.to_wire()["failures"], 1)

    def test_declared_candidates_are_complete_and_reproducible(self) -> None:
        first = declared_candidates()
        self.assertEqual(first, declared_candidates())
        self.assertTrue(first)
        for candidate in first:
            self.assertEqual(
                {intent for intent, _ in candidate.scoring.intent_priorities},
                set(Intent),
            )
            data = encode_config(
                replace(DecisionConfig(), scoring=candidate.scoring)
            )
            self.assertEqual(decode_config(data).scoring, candidate.scoring)

    def test_coordinate_search_reproduces_the_same_candidates(self) -> None:
        case = tuning_case("variant-fallback")
        baseline = evaluate_candidate(
            (case,), Candidate("baseline", "", DEFAULT_SCORING)
        )
        first = coordinate_search((case,), baseline)
        second = coordinate_search((case,), baseline)
        self.assertEqual(first, second)
        self.assertTrue(first)

    def test_choose_prefers_coverage_then_parsimony(self) -> None:
        baseline = _result("baseline", 17, 9, 6, 0)
        better = _result("search-000", 18, 9, 6, 5)
        closer = _result("ablation-uniform", 17, 9, 6, 3)
        farther = _result("ablation-test-first", 17, 9, 6, 4)
        self.assertEqual(
            choose_candidate([farther, baseline, closer]).label, "baseline"
        )
        self.assertEqual(
            choose_candidate([baseline, better, closer]).label, "search-000"
        )
        ineligible = _result("search-001", 19, 11, 7, 1, violations=1)
        self.assertEqual(choose_candidate([baseline, ineligible]).label, "baseline")
        with self.assertRaises(ContractError):
            choose_candidate([ineligible])


class AttributionTests(unittest.TestCase):
    def test_undelivered_facets_carry_selector_reasons(self) -> None:
        case = tuning_case("variant-fallback")
        findings = {
            item.facet_id: item
            for item in undelivered_facets((case,), DecisionConfig())
        }
        self.assertEqual(
            set(findings), {"implementation-visible", "exact-body"}
        )
        self.assertTrue(findings["implementation-visible"].critical)
        self.assertEqual(
            findings["implementation-visible"].reasons, ("delivery_budget",)
        )

    def test_delivered_irrelevant_evidence_is_visible(self) -> None:
        case = tuning_case("lexical-decoy")
        baseline = evaluate_candidate(
            (case,), Candidate("baseline", "", DEFAULT_SCORING)
        )
        findings = delivered_irrelevant((case,), baseline)
        self.assertEqual(len(findings), 1)
        self.assertEqual(findings[0][0], "lexical-decoy")
        self.assertIn(findings[0][1], case.judgments.irrelevant_variant_ids)

    def test_guardrails_reject_negative_delivery_growth(self) -> None:
        baseline = _result("baseline", 10, 5, 3, 0)
        regressed = replace(
            baseline,
            label="regressed",
            known_irrelevant_variants_delivered=1,
            known_irrelevant_source_chars_delivered=40,
            judged_source_chars_delivered=180,
            delivered_source_chars=200,
            baseline_irrelevant_source_chars=18,
            baseline_unjudged_fraction=0.2,
        )
        self.assertEqual(
            regressed.guardrail_violations(),
            ("known_irrelevant_delivery_regressed",),
        )
        self.assertFalse(regressed.eligible())

    def test_guardrails_reject_unjudged_delivery_growth(self) -> None:
        baseline = _result("baseline", 10, 5, 3, 0)
        regressed = replace(
            baseline,
            label="regressed",
            known_irrelevant_source_chars_delivered=0,
            judged_source_chars_delivered=50,
            delivered_source_chars=200,
            baseline_irrelevant_source_chars=18,
            baseline_unjudged_fraction=0.2,
        )
        self.assertEqual(
            regressed.guardrail_violations(),
            ("unjudged_delivery_regressed",),
        )
        self.assertFalse(regressed.eligible())

    def test_guardrails_do_not_reward_returning_almost_nothing(self) -> None:
        baseline = _result("baseline", 10, 5, 3, 0)
        empty = replace(
            baseline,
            label="empty",
            critical_delivered=0,
            all_critical_present=0,
            noncritical_delivered=0,
            judged_source_chars_delivered=0,
            delivered_source_chars=0,
            baseline_irrelevant_source_chars=18,
            baseline_unjudged_fraction=0.2,
        )
        # The guardrail is not a scalar penalty: an empty delivery passes it,
        # but the objective still ranks it below the baseline.
        self.assertEqual(empty.guardrail_violations(), ())
        self.assertTrue(empty.eligible())
        self.assertLess(empty.objective(), baseline.objective())

    def test_no_observation_conflicts_on_the_curated_cases(self) -> None:
        cases = tuple(
            tuning_case(case_id)
            for case_id in ("basic-edit", "lexical-decoy", "same-file-quota")
        )
        findings = discrimination_findings(cases)
        observation_kinds = {
            DISCRIMINATION_AVAILABLE_FEATURES,
            DISCRIMINATION_SCORER_INPUTS,
            DISCRIMINATION_CURRENT_SCORE,
        }
        self.assertEqual(
            [item for item in findings if item.kind in observation_kinds], []
        )

    def test_discrimination_reports_available_feature_depth(self) -> None:
        left, left_variant = _reference(
            "src/a.py", relationship="calls", binding=Binding.UNRESOLVED
        )
        right, right_variant = _reference(
            "src/b.py", relationship="calls", binding=Binding.UNRESOLVED
        )
        findings = pool_discrimination_findings(
            "case",
            _pool((left, left_variant), (right, right_variant)),
            _judgments(left_variant.variant_id, right_variant.variant_id),
            intent=Intent.EDIT,
        )
        self.assertEqual(
            [item.kind for item in findings], [DISCRIMINATION_AVAILABLE_FEATURES]
        )
        self.assertEqual(findings[0].statuses, ("irrelevant", "relevant"))

    def test_discrimination_keeps_positive_labels_without_facets(self) -> None:
        left, left_variant = _reference(
            "src/a.py", relationship="calls", binding=Binding.UNRESOLVED
        )
        right, right_variant = _reference(
            "src/b.py", relationship="calls", binding=Binding.UNRESOLVED
        )
        judgments = replace(
            _judgments(left_variant.variant_id, right_variant.variant_id), facets=()
        )
        findings = pool_discrimination_findings(
            "case",
            _pool((left, left_variant), (right, right_variant)),
            judgments,
            intent=Intent.EDIT,
        )
        self.assertEqual(
            [item.kind for item in findings], [DISCRIMINATION_AVAILABLE_FEATURES]
        )
        self.assertEqual(findings[0].statuses, ("irrelevant", "relevant"))

    def test_discrimination_reports_scorer_input_depth(self) -> None:
        # Same role and resolved binding, different relationship strings: no
        # coefficient change can separate them.
        left, left_variant = _reference(
            "src/a.py", relationship="calls", binding=Binding.RESOLVED
        )
        right, right_variant = _reference(
            "src/b.py", relationship="imports", binding=Binding.RESOLVED
        )
        findings = pool_discrimination_findings(
            "case",
            _pool((left, left_variant), (right, right_variant)),
            _judgments(left_variant.variant_id, right_variant.variant_id),
            intent=Intent.EDIT,
        )
        self.assertEqual(
            [item.kind for item in findings], [DISCRIMINATION_SCORER_INPUTS]
        )
        self.assertEqual(findings[0].statuses, ("irrelevant", "relevant"))

    def test_discrimination_reports_current_score_depth(self) -> None:
        # REFERENCE resolved scores 6+2; TEST scores 8 with no binding bonus.
        reference, reference_variant = _reference(
            "src/a.py", relationship="calls", binding=Binding.RESOLVED
        )
        test_mention, test_variant = _test_mention("tests/test_a.py")
        findings = pool_discrimination_findings(
            "case",
            _pool((reference, reference_variant), (test_mention, test_variant)),
            _judgments(reference_variant.variant_id, test_variant.variant_id),
            intent=Intent.EDIT,
        )
        self.assertEqual(
            [item.kind for item in findings], [DISCRIMINATION_CURRENT_SCORE]
        )

    def test_discrimination_ignores_pairs_the_current_score_separates(self) -> None:
        reference, reference_variant = _reference(
            "src/a.py", relationship="calls", binding=Binding.RESOLVED
        )
        other, other_variant = _reference(
            "src/b.py", relationship="calls", binding=Binding.UNRESOLVED
        )
        findings = pool_discrimination_findings(
            "case",
            _pool((reference, reference_variant), (other, other_variant)),
            _judgments(reference_variant.variant_id, other_variant.variant_id),
            intent=Intent.EDIT,
        )
        self.assertEqual(findings, ())

    def test_changed_dimensions_track_each_behavioral_aspect(self) -> None:
        reference = CaseObservation("case", ("a", "b"), (1, 1, 0), "out-1")
        self.assertEqual(changed_dimensions(reference, reference), frozenset())
        self.assertEqual(
            changed_dimensions(
                reference, CaseObservation("case", ("b", "a"), (1, 1, 0), "out-1")
            ),
            frozenset({CHANGE_ORDER}),
        )
        self.assertEqual(
            changed_dimensions(
                reference, CaseObservation("case", ("a", "c"), (1, 1, 0), "out-1")
            ),
            frozenset({CHANGE_MEMBERSHIP}),
        )
        self.assertEqual(
            changed_dimensions(
                reference, CaseObservation("case", ("a", "b"), (2, 1, 0), "out-1")
            ),
            frozenset({CHANGE_UTILITY}),
        )
        self.assertEqual(
            changed_dimensions(
                reference, CaseObservation("case", ("a", "b"), (1, 1, 0), "out-2")
            ),
            frozenset({CHANGE_OUTPUT}),
        )

    def test_unchanged_membership_does_not_imply_unchanged_behavior(self) -> None:
        cases = tuple(
            tuning_case(case_id)
            for case_id in ("basic-edit", "lexical-decoy", "same-file-quota")
        )
        baseline = evaluate_candidate(
            cases, Candidate("baseline", "", DEFAULT_SCORING)
        )
        muted = replace(
            DEFAULT_SCORING,
            profile="scoring-probe-no-bonus",
            binding_bonus=0,
        )
        candidate = evaluate_candidate(
            cases, Candidate("muted", "", muted), baseline=baseline
        )
        # The selected set is unchanged, but the same evidence arrives in a
        # different order and renders different bytes.
        self.assertEqual(candidate.changed_cases, ())
        self.assertIn("basic-edit", candidate.reordered_cases)
        self.assertEqual(candidate.utility_cases, ())
        self.assertIn("basic-edit", candidate.output_cases)

    def test_representation_conflicts_are_reported(self) -> None:
        case = tuning_case("variant-fallback")
        observation = next(
            item
            for item in case.capture.decision.pool.observations
            if item.kind.value == "implementation"
        )
        variants = case.capture.decision.pool.variants_for(
            observation.observation_id
        )
        excerpt = next(
            variant for variant in variants if variant.representation.value == "excerpt"
        )
        altered = replace(
            case.judgments,
            witnesses=tuple(
                replace(
                    witness,
                    acceptable_variant_ids=tuple(
                        variant_id
                        for variant_id in witness.acceptable_variant_ids
                        if variant_id != excerpt.variant_id
                    ),
                )
                for witness in case.judgments.witnesses
            ),
            irrelevant_variant_ids=(excerpt.variant_id,),
        )
        findings = discrimination_findings(
            (TuningCase(case.case_id, case.capture, altered, case.delivery),)
        )
        self.assertEqual([item.kind for item in findings], ["representation"])
        self.assertEqual(findings[0].statuses, ("irrelevant", "relevant"))


if __name__ == "__main__":
    unittest.main()
