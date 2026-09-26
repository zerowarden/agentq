"""The witness rule, correctness gates, and stage-separated coverage.

The eight-case table below is hand-calculated for the corrected baseline:
critical facets are counted from the authored drafts, pool coverage from the
captured variants, and delivered coverage from the pinned per-case budgets.
"""

from __future__ import annotations

import unittest
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from agentq.core import ContractError
from agentq.inspection.budgeting import DeliveryBudget
from agentq.inspection.contracts import (
    DecisionDelivered,
    RenderedBundle,
    SelectedEvidence,
    SelectionPlan,
)
from agentq.inspection.decision import DecisionConfig
from agentq.inspection.selection import assess_selected_evidence
from evals.build_fixtures import write_suite
from evals.metrics import (
    evaluate_decision,
    facet_supported,
    summarize,
    witness_supported,
)
from evals.models import (
    CaseEvaluation,
    FacetCoverage,
    JudgmentFacet,
    JudgmentSet,
    JudgmentWitness,
)
from evals.replay import case_config, replay_capture
from evals.store import CaptureStore
from tests.evals.support import compiled as _compiled

# case_id: (critical total, pool, initial, delivered, all critical present)
HAND_CALCULATED = {
    "basic-edit": (3, 3, 3, 3, True),
    "lexical-decoy": (3, 3, 3, 3, True),
    "same-file-quota": (1, 1, 1, 1, True),
    "variant-fallback": (2, 2, 1, 1, False),
    "required-upgrade": (2, 2, 2, 2, True),
    "empty-test-search": (2, 2, 2, 2, True),
    "unstable-source": (2, 2, 1, 1, False),
    "delivery-overhead": (1, 1, 1, 1, True),
}


def _evaluate(fixture, judgment, config: DecisionConfig):
    outcome = replay_capture(fixture.capture, config)
    evaluation = evaluate_decision(
        fixture.capture,
        outcome,
        judgment,
        decision_config=config,
    )
    return outcome, evaluation


def _empty_selection(variant_id: str, fixture) -> SelectionPlan:
    variant = next(
        item
        for item in fixture.capture.decision.pool.variants
        if item.variant_id == variant_id
    )
    return SelectionPlan(
        profile="test",
        selected=(SelectedEvidence(variant=variant, reason="test", score=0),),
        omitted=(),
        reserved=(),
        measured_cost=0,
        budget_chars=0,
    )


def _evaluation_with_facets(*facets: FacetCoverage) -> CaseEvaluation:
    return CaseEvaluation(
        case_id="case",
        capture_id="c" * 64,
        judgment_id="j" * 64,
        decision_id="d" * 64,
        evaluation_id="e" * 64,
        outcome="delivered",
        violations=(),
        unmet_expectations=(),
        facets=facets,
    )


def _delivered_with(fixture, outcome, selection: SelectionPlan):
    decision = fixture.capture.decision
    bundle = replace(
        outcome.bundle,
        selection=selection,
        assessment=assess_selected_evidence(
            decision.policy, decision.collection, decision.pool, selection
        ),
    )
    return replace(outcome, bundle=bundle, initial_selection=selection)


class WitnessRuleTests(unittest.TestCase):
    def _judgment(self) -> JudgmentSet:
        return JudgmentSet(
            case_id="toy",
            capture_id="a" * 64,
            basis="target_intent",
            review_status="reviewed",
            facets=(
                JudgmentFacet(
                    facet_id="facet",
                    critical=True,
                    witness_sets=(("a", "b"), ("c",)),
                ),
                JudgmentFacet(
                    facet_id="absent",
                    critical=False,
                    witness_sets=(("absent",),),
                ),
            ),
            witnesses=(
                JudgmentWitness("a", ("v1",)),
                JudgmentWitness("b", ("v2",)),
                JudgmentWitness("c", ("v3",)),
                JudgmentWitness("absent", ()),
            ),
        )

    def test_either_valid_alternative_satisfies_a_facet(self) -> None:
        judgment = self._judgment()
        facet = judgment.facets[0]
        self.assertTrue(facet_supported(facet, judgment, frozenset({"v1", "v2"})))
        self.assertTrue(facet_supported(facet, judgment, frozenset({"v3"})))

    def test_partial_and_clause_does_not_satisfy(self) -> None:
        judgment = self._judgment()
        facet = judgment.facets[0]
        self.assertFalse(facet_supported(facet, judgment, frozenset({"v1"})))
        self.assertFalse(facet_supported(facet, judgment, frozenset({"v2"})))

    def test_absent_witness_never_satisfies(self) -> None:
        judgment = self._judgment()
        absent = judgment.facets[1]
        self.assertFalse(
            witness_supported(judgment, "absent", frozenset({"v1", "v2", "v3"}))
        )
        self.assertFalse(
            facet_supported(absent, judgment, frozenset({"v1", "v2", "v3"}))
        )

    def test_unjudged_evidence_is_ignored(self) -> None:
        judgment = self._judgment()
        facet = judgment.facets[0]
        self.assertFalse(facet_supported(facet, judgment, frozenset({"v99"})))
        self.assertTrue(facet_supported(facet, judgment, frozenset({"v99", "v3"})))


class HandCalculatedMetricsTests(unittest.TestCase):
    def test_evaluation_identity_binds_metric_implementation(self) -> None:
        fixture, judgment = _compiled("basic-edit")
        config = DecisionConfig()
        with patch("evals.metrics.metrics_digest", return_value="a" * 64, create=True):
            _, first = _evaluate(fixture, judgment, config)
        with patch("evals.metrics.metrics_digest", return_value="b" * 64, create=True):
            _, second = _evaluate(fixture, judgment, config)
        self.assertEqual(first.decision_id, second.decision_id)
        self.assertNotEqual(first.evaluation_id, second.evaluation_id)

    @classmethod
    def setUpClass(cls) -> None:
        cls._temp = TemporaryDirectory()
        cls.store = CaptureStore(Path(cls._temp.name))
        _, cls.lock = write_suite(cls.store, "smoke-v1")
        cls.config = DecisionConfig()
        cls.evaluations: dict[str, object] = {}
        for case in cls.lock.cases:
            capture = cls.store.read_capture(case.capture_id)
            judgments = cls.store.read_judgment(case.judgment_id)
            effective = case_config(cls.config, case)
            outcome = replay_capture(capture, effective)
            cls.evaluations[case.case_id] = evaluate_decision(
                capture,
                outcome,
                judgments,
                decision_config=effective,
            )

    @classmethod
    def tearDownClass(cls) -> None:
        cls._temp.cleanup()

    def test_hand_calculated_stage_coverage(self) -> None:
        for case_id, expected in HAND_CALCULATED.items():
            with self.subTest(case_id=case_id):
                total, pool, initial, delivered, present = expected
                evaluation = self.evaluations[case_id]
                self.assertEqual(evaluation.critical_total, total)
                self.assertEqual(evaluation.critical_pool, pool)
                self.assertEqual(evaluation.critical_initial, initial)
                self.assertEqual(evaluation.critical_delivered, delivered)
                self.assertEqual(evaluation.all_critical_present, present)

    def test_no_correctness_violations_and_all_expectations_hold(self) -> None:
        for case_id, evaluation in self.evaluations.items():
            with self.subTest(case_id=case_id):
                self.assertEqual(evaluation.violations, ())
                self.assertEqual(evaluation.unmet_expectations, ())

    def test_hand_calculated_suite_summary(self) -> None:
        summary = summarize(list(self.evaluations.values()))
        self.assertEqual(summary["cases"], 8)
        self.assertEqual(summary["critical_facets"], 16)
        self.assertEqual(summary["critical_recall_pool"], 1.0)
        self.assertEqual(summary["critical_recall_delivered"], 14 / 16)
        self.assertEqual(summary["all_critical_present_cases"], 6)
        self.assertEqual(summary["all_critical_present_rate"], 0.75)
        self.assertEqual(summary["violation_cases"], 0)
        self.assertEqual(summary["unmet_expectation_cases"], 0)
        self.assertEqual(summary["delivery_failures"], 0)


class CorrectnessGateTests(unittest.TestCase):
    def test_unstable_evidence_selected_is_a_violation(self) -> None:
        fixture, judgment = _compiled("unstable-source")
        config = DecisionConfig()
        outcome, _ = _evaluate(fixture, judgment, config)
        assert isinstance(outcome, DecisionDelivered)
        selection = _empty_selection(
            fixture.variant_aliases["target.unstable_source"], fixture
        )
        delivered = _delivered_with(fixture, outcome, selection)
        evaluation = evaluate_decision(
            fixture.capture,
            delivered,
            judgment,
            decision_config=config,
        )
        self.assertTrue(
            any("unstable" in item for item in evaluation.violations),
            evaluation.violations,
        )

    def test_delivered_response_over_its_ceiling_is_a_violation(self) -> None:
        fixture, judgment = _compiled("basic-edit")
        tight = replace(
            DecisionConfig(),
            delivery=DeliveryBudget(max_chars=100, envelope_chars=10),
        )
        outcome, _ = _evaluate(fixture, judgment, DecisionConfig())
        assert isinstance(outcome, DecisionDelivered)
        render = RenderedBundle(format="text", text="x" * 200, chars=200)
        delivered = replace(outcome, bundle=replace(outcome.bundle, render=render))
        evaluation = evaluate_decision(
            fixture.capture,
            delivered,
            judgment,
            decision_config=tight,
        )
        self.assertTrue(
            any("ceiling" in item for item in evaluation.violations),
            evaluation.violations,
        )

    def test_excerpt_earns_visibility_but_not_exact_body_credit(self) -> None:
        fixture, judgment = _compiled("variant-fallback")
        config = DecisionConfig()
        outcome, _ = _evaluate(fixture, judgment, config)
        assert isinstance(outcome, DecisionDelivered)
        selection = _empty_selection(
            fixture.variant_aliases["implementation.excerpt"], fixture
        )
        delivered = _delivered_with(fixture, outcome, selection)
        evaluation = evaluate_decision(
            fixture.capture,
            delivered,
            judgment,
            decision_config=config,
        )
        facets = {item.facet_id: item for item in evaluation.facets}
        self.assertTrue(facets["implementation-visible"].delivered_supported)
        self.assertFalse(facets["exact-body"].delivered_supported)

    def test_a_signature_earns_no_exact_source_credit(self) -> None:
        fixture, judgment = _compiled("basic-edit")
        config = DecisionConfig()
        outcome, _ = _evaluate(fixture, judgment, config)
        assert isinstance(outcome, DecisionDelivered)
        signature = _empty_selection(
            fixture.variant_aliases["target.signature"], fixture
        )
        evaluation = evaluate_decision(
            fixture.capture,
            _delivered_with(fixture, outcome, signature),
            judgment,
            decision_config=config,
        )
        facets = {item.facet_id: item for item in evaluation.facets}
        self.assertFalse(facets["editable-source"].delivered_supported)

    def test_duplicate_evidence_earns_no_extra_facet(self) -> None:
        """Two variants acceptable for one witness still support one facet."""
        fixture, judgment = _compiled("same-file-quota")
        config = DecisionConfig()
        outcome, _ = _evaluate(fixture, judgment, config)
        assert isinstance(outcome, DecisionDelivered)
        first = _empty_selection(fixture.variant_aliases["same_file.use_1"], fixture)
        second = _empty_selection(fixture.variant_aliases["same_file.use_2"], fixture)
        both = replace(first, selected=(*first.selected, *second.selected))
        single = evaluate_decision(
            fixture.capture,
            _delivered_with(fixture, outcome, first),
            judgment,
            decision_config=config,
        )
        doubled = evaluate_decision(
            fixture.capture,
            _delivered_with(fixture, outcome, both),
            judgment,
            decision_config=config,
        )
        self.assertEqual(single.critical_total, doubled.critical_total)
        self.assertEqual(
            [(item.facet_id, item.delivered_supported) for item in single.facets],
            [(item.facet_id, item.delivered_supported) for item in doubled.facets],
        )
        in_file = [item for item in doubled.facets if item.facet_id == "in-file-use"]
        self.assertEqual(len(in_file), 1)
        self.assertTrue(in_file[0].delivered_supported)

    def test_unjudged_evidence_changes_no_facet(self) -> None:
        fixture, judgment = _compiled("required-upgrade")
        config = DecisionConfig()
        outcome, _ = _evaluate(fixture, judgment, config)
        assert isinstance(outcome, DecisionDelivered)
        exact = _empty_selection(fixture.variant_aliases["target.exact"], fixture)
        mention = _empty_selection(fixture.variant_aliases["test.mention"], fixture)
        combined = replace(exact, selected=(*exact.selected, *mention.selected))
        without = evaluate_decision(
            fixture.capture,
            _delivered_with(fixture, outcome, exact),
            judgment,
            decision_config=config,
        )
        with_extra = evaluate_decision(
            fixture.capture,
            _delivered_with(fixture, outcome, combined),
            judgment,
            decision_config=config,
        )
        self.assertEqual(
            [
                (
                    item.facet_id,
                    item.pool_supported,
                    item.initial_supported,
                    item.delivered_supported,
                )
                for item in without.facets
            ],
            [
                (
                    item.facet_id,
                    item.pool_supported,
                    item.initial_supported,
                    item.delivered_supported,
                )
                for item in with_extra.facets
            ],
        )

    def test_delivery_fitting_changes_final_but_not_pool_coverage(self) -> None:
        fixture, judgment = _compiled("delivery-overhead")
        ample = DecisionConfig()
        pinned = replace(ample, delivery=fixture.budget)
        ample_outcome, ample_evaluation = _evaluate(fixture, judgment, ample)
        pinned_outcome, pinned_evaluation = _evaluate(fixture, judgment, pinned)
        self.assertEqual(
            [item.pool_supported for item in ample_evaluation.facets],
            [item.pool_supported for item in pinned_evaluation.facets],
        )
        self.assertEqual(ample_evaluation.fitting_events, 0)
        self.assertGreaterEqual(pinned_evaluation.fitting_events, 1)
        assert isinstance(pinned_outcome, DecisionDelivered)
        assert pinned_outcome.bundle.selection is not None
        self.assertLess(
            len(pinned_outcome.bundle.selection.selected),
            len(pinned_outcome.initial_selection.selected),
        )
        del ample_outcome


class NegativeDeliveryTests(unittest.TestCase):
    def test_known_negative_and_unjudged_material_are_distinct(self) -> None:
        fixture, judgment = _compiled("lexical-decoy")
        _, evaluation = _evaluate(fixture, judgment, DecisionConfig())
        self.assertGreater(evaluation.known_irrelevant_variants_delivered, 0)
        self.assertGreater(evaluation.known_irrelevant_source_chars_delivered, 0)
        self.assertGreater(evaluation.delivered_source_chars, 0)
        self.assertLessEqual(
            evaluation.judged_source_chars_delivered,
            evaluation.delivered_source_chars,
        )
        # The case delivers material nobody judged; it is not negative.
        self.assertGreater(evaluation.unjudged_fraction_of_delivery, 0)
        self.assertGreater(evaluation.judged_fraction_of_delivery, 0)
        self.assertIsNotNone(evaluation.final_output_tokens)

    def test_summary_promotes_negative_and_cost_metrics(self) -> None:
        fixture, judgment = _compiled("lexical-decoy")
        _, evaluation = _evaluate(fixture, judgment, DecisionConfig())
        summary = summarize((evaluation,))
        self.assertEqual(
            summary["known_irrelevant_variants_delivered"],
            evaluation.known_irrelevant_variants_delivered,
        )
        self.assertEqual(
            summary["known_irrelevant_source_chars_delivered"],
            evaluation.known_irrelevant_source_chars_delivered,
        )
        self.assertEqual(
            summary["final_output_tokens"], evaluation.final_output_tokens
        )
        self.assertIsNotNone(summary["judged_fraction_of_delivery"])
        self.assertIsNotNone(summary["unjudged_fraction_of_delivery"])

    def test_noncritical_coverage_counts_pool_supported_facets(self) -> None:
        evaluation = _evaluation_with_facets(
            FacetCoverage("critical", True, True, True, True),
            FacetCoverage("delivered", False, True, True, True),
            FacetCoverage("undelivered", False, True, True, False),
            FacetCoverage("unsupported", False, False, False, False),
        )
        self.assertEqual(evaluation.noncritical_total, 2)
        self.assertEqual(evaluation.noncritical_delivered, 1)

    def test_known_irrelevant_chars_cannot_exceed_judged_chars(self) -> None:
        with self.assertRaises(ContractError):
            replace(
                _evaluation_with_facets(),
                delivered_source_chars=100,
                judged_source_chars_delivered=10,
                known_irrelevant_source_chars_delivered=20,
            )

    def test_a_failed_delivery_has_no_fractions_or_tokens(self) -> None:
        fixture, judgment = _compiled("basic-edit")
        config = replace(
            DecisionConfig(),
            delivery=DeliveryBudget(max_chars=1, envelope_chars=0),
        )
        outcome, evaluation = _evaluate(fixture, judgment, config)
        self.assertNotIsInstance(outcome, DecisionDelivered)
        self.assertEqual(evaluation.delivered_source_chars, 0)
        self.assertIsNone(evaluation.judged_fraction_of_delivery)
        self.assertIsNone(evaluation.unjudged_fraction_of_delivery)
        self.assertIsNone(evaluation.final_output_tokens)


if __name__ == "__main__":
    unittest.main()
