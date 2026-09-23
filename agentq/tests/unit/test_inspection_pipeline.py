"""End-to-end fake inspection traversal and stage isolation."""

from __future__ import annotations

import io
import json
import unittest
from contextlib import redirect_stderr, redirect_stdout

from agentq.core import COMPLETE, Coverage, typed_coverage
from agentq.inspection.acquisition import acquire, plan_collection
from agentq.inspection.budgeting import DeliveryBudget
from agentq.inspection.contracts import (
    AmbiguousTarget,
    Capability,
    CapabilityResult,
    CollectionStatus,
    EvidenceRole,
    InspectionRequest,
    Intent,
    PathTarget,
    RangeTarget,
    RepresentationKind,
    RequirementStatus,
    ResolvedTarget,
    SourceSpan,
    UnresolvedReason,
    UnresolvedTarget,
)
from agentq.inspection.debug import TraceRecorder
from agentq.inspection.features import extract_features
from agentq.inspection.policy import POLICY_PROFILE, compile_policy
from agentq.inspection.resolution import resolve
from agentq.inspection.scoring import DEFAULT_SCORING, ScoringProfile, score_evidence
from agentq.inspection.selection import SelectionProfile, select_evidence
from agentq.inspection.service import (
    describe_capabilities,
    inspect,
    normalize_request,
)
from tests.support.inspection_fakes import (
    CHANGED_VERSION,
    DEFAULT_PATH,
    PACKAGE_PATH,
    REFERENCE_PATH,
    TEST_PATH,
    VERSION,
    candidate_request,
    declaration_result,
    default_symbol_handler,
    fake_context,
    location_request,
    source_result,
    symbol_request,
)

EXPECTED_STAGES = [
    "normalize",
    "capabilities",
    "resolution",
    "policy",
    "collection",
    "scoring",
    "selection",
    "assessment",
    "render",
]


class PipelineTraversalTests(unittest.TestCase):
    def test_fake_inspection_traverses_every_stage(self) -> None:
        handler = default_symbol_handler()
        recorder = TraceRecorder()
        context = fake_context(handler, trace=recorder)
        stdout, stderr = io.StringIO(), io.StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            bundle = inspect(symbol_request(), context)
        self.assertEqual(stdout.getvalue(), "")
        self.assertEqual(stderr.getvalue(), "")

        resolution = bundle.resolution
        self.assertIsInstance(resolution, ResolvedTarget)
        assert isinstance(resolution, ResolvedTarget)
        self.assertEqual(resolution.method.value, "unique_candidate")
        self.assertEqual(resolution.declaration.path, DEFAULT_PATH)  # type: ignore[union-attr]

        assert bundle.collection is not None
        assert bundle.selection is not None
        assert bundle.assessment is not None
        self.assertEqual(bundle.policy.profile, POLICY_PROFILE)  # type: ignore[union-attr]
        collected = {item.capability for item in bundle.collection.requests}
        self.assertIn(Capability.READ_SOURCE, collected)
        self.assertIn(Capability.SEMANTIC_REFERENCES, collected)

        selected_representations = {
            item.variant.representation for item in bundle.selection.selected
        }
        self.assertIn(RepresentationKind.EXACT_SOURCE, selected_representations)
        self.assertIn(RepresentationKind.REFERENCE, selected_representations)
        self.assertIs(
            bundle.assessment.by_id("declaration_identity").status,  # type: ignore[union-attr]
            RequirementStatus.SATISFIED,
        )
        self.assertIs(
            bundle.assessment.by_id("target_source").status,  # type: ignore[union-attr]
            RequirementStatus.SATISFIED,
        )
        self.assertIsNotNone(bundle.render)
        self.assertIn("function listOrders", bundle.render.text)  # type: ignore[union-attr]

        stages = [event.stage for event in recorder.snapshot().events]
        self.assertEqual(stages, EXPECTED_STAGES)

    def test_provider_call_log_follows_the_collection_plan(self) -> None:
        handler = default_symbol_handler()
        bundle = inspect(symbol_request(), fake_context(handler))
        assert bundle.collection is not None
        planned = {item.capability.value for item in bundle.collection.requests}
        called = {name for name, _ in handler.calls}
        self.assertTrue(planned)
        self.assertEqual(called - planned, {"find_declarations"})

    def test_debug_flag_does_not_change_the_bundle(self) -> None:
        plain = inspect(symbol_request(), fake_context(default_symbol_handler()))
        debugged = inspect(
            symbol_request(), fake_context(default_symbol_handler(), debug=True)
        )
        self.assertEqual(plain.render, debugged.render)
        self.assertEqual(plain.resolution, debugged.resolution)
        self.assertEqual(plain.selection, debugged.selection)

    def test_bundle_render_never_exceeds_the_delivery_budget(self) -> None:
        for output_format in ("text", "json", "compact-json"):
            with self.subTest(output_format=output_format):
                handler = default_symbol_handler()
                context = fake_context(handler, output_format=output_format)
                bundle = inspect(symbol_request(), context)
                assert bundle.render is not None
                self.assertLessEqual(bundle.render.chars, context.delivery.max_chars)
                assert bundle.selection is not None
                self.assertLessEqual(
                    bundle.selection.measured_cost,
                    context.delivery.available_chars(),
                )

    def test_adapter_replacement_keeps_the_pipeline_contract(self) -> None:
        handler = default_symbol_handler(name="fake-alt")
        bundle = inspect(symbol_request(), fake_context(handler))
        assert isinstance(bundle.resolution, ResolvedTarget)
        self.assertEqual(bundle.resolution.declaration.provider, "fake-alt")  # type: ignore[union-attr]


class ResolutionOutcomeTests(unittest.TestCase):
    def test_missing_registry_is_an_explicit_unavailable_response(self) -> None:
        bundle = inspect(symbol_request(), fake_context(None))
        self.assertIsInstance(bundle.resolution, UnresolvedTarget)
        assert isinstance(bundle.resolution, UnresolvedTarget)
        self.assertIs(bundle.resolution.reason, UnresolvedReason.UNAVAILABLE)
        self.assertIsNone(bundle.policy)
        self.assertIsNone(bundle.selection)
        self.assertIsNone(bundle.assessment)
        self.assertTrue(bundle.gaps)
        self.assertIn("unresolved", bundle.render.text)  # type: ignore[union-attr]

    def test_duplicate_declarations_stay_ambiguous(self) -> None:
        handler = default_symbol_handler()
        first = declaration_result("listOrders", path="src/a.ts", line=1, end_line=3)
        second = declaration_result("listOrders", path="src/b.ts", line=5, end_line=7)
        handler.results[Capability.FIND_DECLARATIONS] = CapabilityResult(
            status=CollectionStatus.COMPLETED,
            observations=(*first.observations, *second.observations),
            variants=(*first.variants, *second.variants),
            coverage=typed_coverage(COMPLETE),
        )
        bundle = inspect(symbol_request(), fake_context(handler))
        self.assertIsInstance(bundle.resolution, AmbiguousTarget)
        assert isinstance(bundle.resolution, AmbiguousTarget)
        self.assertEqual(len(bundle.resolution.candidates), 2)
        self.assertEqual(bundle.resolution.count_quality, "exact")
        self.assertIsNone(bundle.selection)

    def test_changed_source_version_is_a_stale_candidate(self) -> None:
        handler = default_symbol_handler()
        context = fake_context(
            handler,
            versions={
                DEFAULT_PATH: CHANGED_VERSION,
                REFERENCE_PATH: VERSION,
                PACKAGE_PATH: VERSION,
                TEST_PATH: VERSION,
            },
        )
        bundle = inspect(symbol_request(), context)
        self.assertIsInstance(bundle.resolution, UnresolvedTarget)
        assert isinstance(bundle.resolution, UnresolvedTarget)
        self.assertIs(bundle.resolution.reason, UnresolvedReason.STALE_CANDIDATE)

    def test_explicit_candidate_is_reacquired_and_validated(self) -> None:
        first = inspect(symbol_request(), fake_context(default_symbol_handler()))
        assert isinstance(first.resolution, ResolvedTarget)
        candidate_id = first.resolution.declaration.candidate_id  # type: ignore[union-attr]
        second = inspect(
            candidate_request(candidate_id), fake_context(default_symbol_handler())
        )
        assert isinstance(second.resolution, ResolvedTarget)
        self.assertEqual(second.resolution.method.value, "explicit_candidate")
        self.assertEqual(
            second.resolution.declaration.candidate_id, candidate_id  # type: ignore[union-attr]
        )

    def test_unknown_candidate_id_is_stale_not_silently_reselected(self) -> None:
        bundle = inspect(
            candidate_request("cand-not-issued"), fake_context(default_symbol_handler())
        )
        assert isinstance(bundle.resolution, UnresolvedTarget)
        self.assertIs(bundle.resolution.reason, UnresolvedReason.STALE_CANDIDATE)

    def test_location_target_resolves_through_the_capability(self) -> None:
        bundle = inspect(location_request(), fake_context(default_symbol_handler()))
        assert isinstance(bundle.resolution, ResolvedTarget)
        self.assertEqual(bundle.resolution.method.value, "exact_location")
        self.assertEqual(bundle.resolution.declaration.path, DEFAULT_PATH)  # type: ignore[union-attr]

    def test_path_target_is_direct_and_structural(self) -> None:
        request = InspectionRequest(target=PathTarget(path="src"))
        bundle = inspect(request, fake_context(default_symbol_handler()))
        assert isinstance(bundle.resolution, ResolvedTarget)
        self.assertEqual(bundle.resolution.method.value, "direct_target")
        assert bundle.assessment is not None
        self.assertIs(
            bundle.assessment.by_id("target_structure").status,  # type: ignore[union-attr]
            RequirementStatus.SATISFIED,
        )

    def test_range_target_requires_exact_source(self) -> None:
        request = InspectionRequest(
            target=RangeTarget(
                path=DEFAULT_PATH, ranges=(SourceSpan(start_line=10, end_line=12),)
            )
        )
        bundle = inspect(request, fake_context(default_symbol_handler()))
        assert bundle.assessment is not None
        self.assertIs(
            bundle.assessment.by_id("requested_source").status,  # type: ignore[union-attr]
            RequirementStatus.SATISFIED,
        )


class StageIsolationTests(unittest.TestCase):
    def test_scoring_profile_does_not_change_collection(self) -> None:
        default_handler = default_symbol_handler()
        default_bundle = inspect(symbol_request(), fake_context(default_handler))
        custom_handler = default_symbol_handler()
        custom_profile = ScoringProfile(
            profile="scoring-test-v1",
            binding_bonus=5,
            intent_priorities=((Intent.EDIT, ((EvidenceRole.REFERENCE, 8),)),),
        )
        custom_bundle = inspect(
            symbol_request(),
            fake_context(custom_handler),
            scoring=custom_profile,
        )
        self.assertEqual(default_handler.calls, custom_handler.calls)
        self.assertEqual(default_bundle.resolution, custom_bundle.resolution)
        self.assertEqual(default_bundle.policy, custom_bundle.policy)
        self.assertEqual(default_bundle.collection, custom_bundle.collection)
        assert custom_bundle.selection is not None
        self.assertTrue(
            any(item.score == 13 for item in custom_bundle.selection.selected)
        )
        assert default_bundle.selection is not None
        self.assertFalse(
            any(item.score == 13 for item in default_bundle.selection.selected)
        )

    def test_selection_profile_does_not_trigger_more_provider_calls(self) -> None:
        default_handler = default_symbol_handler()
        inspect(symbol_request(), fake_context(default_handler))
        custom_handler = default_symbol_handler()
        custom_bundle = inspect(
            symbol_request(),
            fake_context(custom_handler),
            selection=SelectionProfile(
                profile="selection-test-v1",
                reserve_required=True,
                role_diversity=False,
                fill_by_score=False,
            ),
        )
        self.assertEqual(default_handler.calls, custom_handler.calls)
        assert custom_bundle.selection is not None
        self.assertFalse(
            any(item.reason == "relevance" for item in custom_bundle.selection.selected)
        )
        assert custom_bundle.assessment is not None
        self.assertIs(
            custom_bundle.assessment.by_id("representative_reference").status,  # type: ignore[union-attr]
            RequirementStatus.UNSATISFIED,
        )

    def test_scorer_replacement_runs_on_one_unchanged_evidence_pool(self) -> None:
        handler = default_symbol_handler()
        context = fake_context(handler)
        request = normalize_request(symbol_request(), context)
        report = describe_capabilities(request, context)
        resolution = resolve(request, report, context)
        assert isinstance(resolution, ResolvedTarget)
        policy = compile_policy(request, resolution)
        plan = plan_collection(
            policy,
            resolution,
            report,
            context.limits,
            request_id=request.request_id,
            evidence_scopes=request.evidence_scopes,
        )
        pool = acquire(plan, context)
        calls_before = tuple(handler.calls)
        features = extract_features(pool)

        default = score_evidence(features, DEFAULT_SCORING, intent=request.intent)
        custom = ScoringProfile(
            profile="scoring-isolation-v1",
            binding_bonus=9,
            intent_priorities=((Intent.EDIT, ((EvidenceRole.REFERENCE, 1),)),),
        )
        replaced = score_evidence(features, custom, intent=request.intent)

        self.assertEqual(calls_before, tuple(handler.calls))
        self.assertEqual(
            tuple(item.observation_id for item in default),
            tuple(item.observation_id for item in replaced),
        )
        self.assertNotEqual(
            tuple(item.score.total for item in default),
            tuple(item.score.total for item in replaced),
        )
        self.assertEqual(len(features), len(pool.observations))

    def test_selector_replacement_never_calls_providers(self) -> None:
        handler = default_symbol_handler()
        context = fake_context(handler)
        request = normalize_request(symbol_request(), context)
        report = describe_capabilities(request, context)
        resolution = resolve(request, report, context)
        assert isinstance(resolution, ResolvedTarget)
        policy = compile_policy(request, resolution)
        plan = plan_collection(
            policy,
            resolution,
            report,
            context.limits,
            request_id=request.request_id,
            evidence_scopes=request.evidence_scopes,
        )
        pool = acquire(plan, context)
        scores = score_evidence(
            extract_features(pool), DEFAULT_SCORING, intent=request.intent
        )
        calls_before = tuple(handler.calls)

        first = select_evidence(pool, scores, policy, context.delivery)
        second = select_evidence(
            pool,
            scores,
            policy,
            context.delivery,
            profile=SelectionProfile(
                profile="selection-isolation-v1",
                reserve_required=True,
                role_diversity=False,
                fill_by_score=True,
            ),
        )

        self.assertEqual(calls_before, tuple(handler.calls))
        self.assertTrue(first.selected)
        self.assertTrue(second.selected)
        self.assertEqual(first.profile, "selection-v1")
        self.assertEqual(second.profile, "selection-isolation-v1")
        required_missing = [
            item for item in first.selected if item.reason == "required"
        ]
        self.assertTrue(required_missing)

    def test_tight_budget_reports_missing_required_source(self) -> None:
        # The fake fixture's declaration signature fits in 96 serialized
        # characters; its exact source body does not fit the 100 available here.
        budget = DeliveryBudget(max_chars=110, envelope_chars=10)
        bundle = inspect(
            symbol_request(),
            fake_context(default_symbol_handler(), delivery=budget),
        )
        assert bundle.assessment is not None
        self.assertIs(
            bundle.assessment.by_id("target_source").status,  # type: ignore[union-attr]
            RequirementStatus.UNSATISFIED,
        )
        self.assertIs(
            bundle.assessment.by_id("declaration_identity").status,  # type: ignore[union-attr]
            RequirementStatus.SATISFIED,
        )
        assert bundle.selection is not None
        self.assertTrue(
            any(item.reason == "delivery_budget" for item in bundle.selection.omitted)
        )

    def test_trace_is_bounded_and_drops_events(self) -> None:
        recorder = TraceRecorder(max_events=3)
        inspect(
            symbol_request(), fake_context(default_symbol_handler(), trace=recorder)
        )
        trace = recorder.snapshot()
        self.assertLessEqual(len(trace.events), 3)
        self.assertGreater(trace.dropped, 0)

    def test_lifecycle_marks_observations_unstable_without_dropping_them(self) -> None:
        handler = default_symbol_handler()
        context = fake_context(
            handler,
            versions={
                DEFAULT_PATH: VERSION,
                REFERENCE_PATH: CHANGED_VERSION,
                PACKAGE_PATH: VERSION,
                TEST_PATH: VERSION,
            },
        )
        bundle = inspect(symbol_request(), context)
        self.assertTrue(bundle.gaps)
        self.assertTrue(any(gap.code == "source_unstable" for gap in bundle.gaps))


class RenderFormatTests(unittest.TestCase):
    def test_json_rendering_is_valid_and_records_metadata(self) -> None:
        handler = default_symbol_handler()
        bundle = inspect(symbol_request(), fake_context(handler, output_format="json"))
        assert bundle.render is not None
        payload = json.loads(bundle.render.text)
        self.assertEqual(payload["schema"], "agentq.inspection/v1")
        self.assertEqual(payload["resolution"]["outcome"], "resolved")
        self.assertIn("selection", payload)
        self.assertEqual(payload["selection"]["measured_cost"], bundle.selection.measured_cost)  # type: ignore[index]
        selected = payload["selection"]["selected"]
        self.assertTrue(selected)
        self.assertIn("function listOrders", selected[0]["variant"]["text"])
        self.assertIn("contributions", selected[0])

    def test_partial_collection_coverage_flows_into_the_bundle(self) -> None:
        handler = default_symbol_handler(name="fake-partial")
        handler.results[Capability.READ_SOURCE] = CapabilityResult(
            status=CollectionStatus.PARTIAL,
            observations=source_result().observations,
            variants=source_result().variants,
            coverage=Coverage(status="partial", reasons=("result_limit",)),
        )
        bundle = inspect(symbol_request(), fake_context(handler))
        assert isinstance(bundle.resolution, ResolvedTarget)
        self.assertFalse(bundle.coverage.is_complete())
        self.assertTrue(any(item.code == "result_limit" for item in bundle.gaps))
