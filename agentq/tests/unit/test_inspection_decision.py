"""The shared post-acquisition decision stage and its typed boundary."""

from __future__ import annotations

import unittest
from collections.abc import Callable
from dataclasses import replace
from unittest import mock

from agentq.core import ContractError
from agentq.inspection import service as service_module
from agentq.inspection.budgeting import DeliveryBudget
from agentq.inspection.contracts import (
    DecisionDelivered,
    DecisionFailure,
    DecisionInput,
    DecisionOutcome,
    RequirementStatus,
)
from agentq.inspection.debug import TraceRecorder
from agentq.inspection.decision import DecisionConfig, decide_evidence
from agentq.inspection.scoring import DEFAULT_SCORING
from agentq.inspection.service import inspect
from evals.fixtures.synthetic import builders
from evals.fixtures.synthetic.builders import FixtureBuild
from tests.support.inspection_fakes import (
    default_symbol_handler,
    fake_context,
    symbol_request,
)

TIGHT = DeliveryBudget(max_chars=40, envelope_chars=10)


def _decision_input(fixture: FixtureBuild) -> DecisionInput:
    return DecisionInput(
        request=fixture.request,
        resolution=fixture.resolution,
        policy=fixture.policy,
        collection=fixture.collection,
        pool=fixture.pool,
    )


def _capturing_decide(
    captured: list[tuple[DecisionInput, DecisionConfig]],
    real: Callable[..., DecisionOutcome],
) -> Callable[..., DecisionOutcome]:
    """Record the exact input/config the service hands to the decision stage."""

    def capture(
        decision_input: DecisionInput,
        config: DecisionConfig,
        *,
        trace: TraceRecorder | None = None,
    ) -> DecisionOutcome:
        captured.append((decision_input, config))
        return real(decision_input, config, trace=trace)

    return capture


class SharedDecisionPathTests(unittest.TestCase):
    def test_live_inspection_delegates_to_the_shared_decision_function(self) -> None:
        real = decide_evidence
        cases = (
            ("text", DeliveryBudget()),
            ("json", DeliveryBudget()),
            ("text", DeliveryBudget(max_chars=1_500, envelope_chars=200)),
        )
        for output_format, delivery in cases:
            with self.subTest(output_format=output_format, chars=delivery.max_chars):
                captured: list[tuple[DecisionInput, DecisionConfig]] = []
                capture = _capturing_decide(captured, real)
                context = fake_context(
                    default_symbol_handler(),
                    output_format=output_format,
                    delivery=delivery,
                )
                with mock.patch.object(service_module, "decide_evidence", capture):
                    bundle = inspect(symbol_request(), context)

                self.assertEqual(len(captured), 1)
                decision_input, config = captured[0]
                self.assertIsInstance(decision_input, DecisionInput)
                outcome = real(decision_input, config)
                self.assertIsInstance(outcome, DecisionDelivered)
                assert isinstance(outcome, DecisionDelivered)
                self.assertEqual(outcome.bundle, bundle)

    def test_trace_is_observational_only(self) -> None:
        fixture = builders.BUILDERS["basic-edit"]()
        plain = decide_evidence(_decision_input(fixture), DecisionConfig())
        traced = decide_evidence(
            _decision_input(fixture), DecisionConfig(), trace=TraceRecorder()
        )
        self.assertEqual(plain, traced)

    def test_changing_scoring_leaves_provider_calls_unchanged(self) -> None:
        handler = default_symbol_handler()
        inspect(symbol_request(), fake_context(handler))
        altered_handler = default_symbol_handler()
        inspect(
            symbol_request(),
            fake_context(altered_handler),
            scoring=replace(
                DEFAULT_SCORING, profile="test-binding-bonus-10", binding_bonus=10
            ),
        )
        self.assertEqual(altered_handler.calls, handler.calls)


class AuthoredFixtureDecisionTests(unittest.TestCase):
    def test_every_authored_fixture_produces_a_deterministic_decision(self) -> None:
        for case_id, build in builders.BUILDERS.items():
            with self.subTest(case_id=case_id):
                fixture = build()
                config = DecisionConfig(delivery=DeliveryBudget())
                first = decide_evidence(_decision_input(fixture), config)
                second = decide_evidence(_decision_input(fixture), config)
                self.assertEqual(first, second)
                self.assertIsInstance(first, DecisionDelivered)
                assert isinstance(first, DecisionDelivered)
                render = first.bundle.render
                self.assertIsNotNone(render)
                assert render is not None
                self.assertLessEqual(render.chars + 1, config.delivery.max_chars)
                self.assertEqual(len(first.features), len(fixture.pool.observations))
                self.assertEqual(len(first.scores), len(first.features))

    def test_delivery_overhead_records_fitting_loss(self) -> None:
        fixture = builders.BUILDERS["delivery-overhead"]()
        config = DecisionConfig(delivery=fixture.budget)
        outcome = decide_evidence(_decision_input(fixture), config)
        self.assertIsInstance(outcome, DecisionDelivered)
        assert isinstance(outcome, DecisionDelivered)
        self.assertTrue(outcome.fitting_events)
        assert outcome.bundle.selection is not None
        self.assertLess(
            len(outcome.bundle.selection.selected),
            len(outcome.initial_selection.selected),
        )
        render = outcome.bundle.render
        assert render is not None
        self.assertLessEqual(render.chars + 1, config.delivery.max_chars)

    def test_unstable_evidence_is_preserved_but_inadmissible(self) -> None:
        fixture = builders.BUILDERS["unstable-source"]()
        unstable_id = fixture.variant_aliases["target.unstable_source"]
        outcome = decide_evidence(_decision_input(fixture), DecisionConfig())
        self.assertIsInstance(outcome, DecisionDelivered)
        assert isinstance(outcome, DecisionDelivered)
        self.assertTrue(fixture.pool.unstable_observation_ids)
        self.assertIn(unstable_id, {item.variant_id for item in fixture.pool.variants})
        assert outcome.bundle.selection is not None
        selected = {
            item.variant.variant_id for item in outcome.bundle.selection.selected
        }
        self.assertNotIn(unstable_id, selected)
        assessment = outcome.bundle.assessment
        assert assessment is not None
        requirement = assessment.by_id("target_source")
        assert requirement is not None
        self.assertIs(requirement.status, RequirementStatus.UNSATISFIED)


class DeliveryFailureTests(unittest.TestCase):
    def test_envelope_too_small_is_a_typed_failure(self) -> None:
        fixture = builders.BUILDERS["basic-edit"]()
        outcome = decide_evidence(
            _decision_input(fixture), DecisionConfig(delivery=TIGHT)
        )
        self.assertIsInstance(outcome, DecisionFailure)
        assert isinstance(outcome, DecisionFailure)
        self.assertEqual(outcome.reason, "delivery_budget")
        self.assertIn("cannot hold the inspection response", outcome.detail)

    def test_live_inspection_preserves_the_envelope_error(self) -> None:
        context = fake_context(default_symbol_handler(), delivery=TIGHT)
        with self.assertRaises(ContractError) as caught:
            inspect(symbol_request(), context)
        self.assertIn("cannot hold the inspection response", str(caught.exception))


class DecisionIsolationTests(unittest.TestCase):
    def test_decisions_do_not_touch_filesystem_or_subprocesses(self) -> None:
        fixture = builders.BUILDERS["basic-edit"]()
        with (
            mock.patch("subprocess.run", side_effect=AssertionError("subprocess")),
            mock.patch("subprocess.Popen", side_effect=AssertionError("subprocess")),
            mock.patch("pathlib.Path.read_bytes", side_effect=AssertionError("read")),
            mock.patch("pathlib.Path.read_text", side_effect=AssertionError("read")),
            mock.patch(
                "agentq.inspection.debug.perf_counter",
                side_effect=AssertionError("clock"),
            ),
        ):
            outcome = decide_evidence(_decision_input(fixture), DecisionConfig())
        self.assertIsInstance(outcome, DecisionDelivered)


if __name__ == "__main__":
    unittest.main()
