"""End-to-end fake inspection traversal and stage isolation."""

from __future__ import annotations

import io
import itertools
import json
import unittest
from contextlib import redirect_stderr, redirect_stdout
from dataclasses import replace

from agentq.core import COMPLETE, Coverage, typed_coverage
from agentq.inspection.acquisition import acquire, plan_collection
from agentq.inspection.budgeting import AcquisitionLimits, DeliveryBudget
from agentq.inspection.contracts import (
    AmbiguousTarget,
    Capability,
    CapabilityResult,
    CollectionStatus,
    EvidenceRole,
    InspectionRequest,
    Intent,
    PathKind,
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
from agentq.inspection.execution import ExecutionLedger
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
    FakeHandler,
    candidate_request,
    declaration_result,
    default_symbol_handler,
    empty_result,
    fake_context,
    location_request,
    reference_result,
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


def _reference_flood(count: int) -> CapabilityResult:
    """One completed acquisition with ``count`` distinct reference observations."""
    observations = []
    variants = []
    for index in range(count):
        result = reference_result(path=f"src/use{index:03d}.ts", line=index + 1)
        observations.extend(result.observations)
        variants.extend(result.variants)
    return CapabilityResult(
        status=CollectionStatus.COMPLETED,
        observations=tuple(observations),
        variants=tuple(variants),
        coverage=typed_coverage(COMPLETE),
    )


def _declaration_flood(count: int) -> CapabilityResult:
    """One completed acquisition with ``count`` distinct declaration candidates."""
    observations = []
    variants = []
    for index in range(count):
        result = declaration_result(path=f"src/mod{index:03d}.ts", line=1, end_line=3)
        observations.extend(result.observations)
        variants.extend(result.variants)
    return CapabilityResult(
        status=CollectionStatus.COMPLETED,
        observations=tuple(observations),
        variants=tuple(variants),
        coverage=typed_coverage(COMPLETE),
    )


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
        request = InspectionRequest(
            target=PathTarget(path="src", path_kind=PathKind.DIRECTORY)
        )
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


class SubjectAffinityTests(unittest.TestCase):
    """Post-resolution capabilities follow the resolved declaration's language."""

    def test_python_subject_falls_back_to_python_syntactic_mentions(self) -> None:
        typescript = FakeHandler(
            name="fake-ts",
            supported=frozenset({Capability.SEMANTIC_REFERENCES}),
            subject_applicable_to=lambda subject: subject.path.endswith(".ts"),
            results={Capability.SEMANTIC_REFERENCES: reference_result()},
        )
        python = FakeHandler(
            name="fake-py",
            supported=frozenset(
                {Capability.FIND_DECLARATIONS, Capability.SYNTACTIC_MENTIONS}
            ),
            subject_applicable_to=lambda subject: subject.path.endswith(".py"),
            results={
                Capability.FIND_DECLARATIONS: declaration_result(path="src/service.py"),
                Capability.SYNTACTIC_MENTIONS: reference_result(path="src/use.py"),
            },
        )
        bundle = inspect(
            symbol_request(),
            fake_context(
                (typescript, python),
                versions={"src/service.py": VERSION, "src/use.py": VERSION},
            ),
        )
        assert isinstance(bundle.resolution, ResolvedTarget)
        self.assertEqual(bundle.resolution.declaration.path, "src/service.py")  # type: ignore[union-attr]
        assert bundle.collection is not None
        planned = {item.capability for item in bundle.collection.requests}
        self.assertIn(Capability.SYNTACTIC_MENTIONS, planned)
        self.assertNotIn(Capability.SEMANTIC_REFERENCES, planned)
        self.assertNotIn(
            ("semantic_references", "collect-representative_reference"),
            typescript.calls,
        )
        assert bundle.assessment is not None
        self.assertIs(
            bundle.assessment.by_id("representative_reference").status,  # type: ignore[union-attr]
            RequirementStatus.SATISFIED,
        )

    def test_typescript_subject_is_not_answered_by_python_syntactic_mentions(
        self,
    ) -> None:
        typescript = FakeHandler(
            name="fake-ts",
            supported=frozenset(
                {Capability.FIND_DECLARATIONS, Capability.SEMANTIC_REFERENCES}
            ),
            subject_applicable_to=lambda subject: subject.path.endswith(".ts"),
            results={
                Capability.FIND_DECLARATIONS: declaration_result(path="src/service.ts"),
                Capability.SEMANTIC_REFERENCES: reference_result(),
            },
        )
        python = FakeHandler(
            name="fake-py",
            supported=frozenset(
                {Capability.FIND_DECLARATIONS, Capability.SYNTACTIC_MENTIONS}
            ),
            subject_applicable_to=lambda subject: subject.path.endswith(".py"),
            results={
                Capability.FIND_DECLARATIONS: empty_result(),
                Capability.SYNTACTIC_MENTIONS: reference_result(path="src/use.py"),
            },
        )
        bundle = inspect(
            symbol_request(),
            fake_context(
                (typescript, python),
                versions={"src/service.ts": VERSION, "src/use.py": VERSION},
            ),
        )
        assert isinstance(bundle.resolution, ResolvedTarget)
        self.assertEqual(bundle.resolution.declaration.path, "src/service.ts")  # type: ignore[union-attr]
        assert bundle.collection is not None
        planned = {item.capability for item in bundle.collection.requests}
        self.assertIn(Capability.SEMANTIC_REFERENCES, planned)
        self.assertNotIn(Capability.SYNTACTIC_MENTIONS, planned)
        self.assertEqual(
            [call for call in python.calls if call[0] == "syntactic_mentions"], []
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
        # The envelope must cover the response framing; the remaining evidence
        # capacity fits the declaration signature and its provenance line but
        # not exact source.
        budget = DeliveryBudget(max_chars=920, envelope_chars=700)
        bundle = inspect(
            symbol_request(),
            fake_context(default_symbol_handler(), delivery=budget),
        )
        assert bundle.assessment is not None and bundle.render is not None
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
        self.assertLessEqual(bundle.render.chars + 1, budget.max_chars)

    def test_hundreds_of_omissions_stay_within_the_delivery_budget(self) -> None:
        for output_format in ("text", "json"):
            with self.subTest(output_format=output_format):
                handler = default_symbol_handler()
                handler.results[Capability.SEMANTIC_REFERENCES] = _reference_flood(400)
                context = fake_context(
                    handler,
                    output_format=output_format,
                    limits=AcquisitionLimits(
                        max_observations=500, max_source_files=500
                    ),
                )
                bundle = inspect(symbol_request(), context)
                assert bundle.selection is not None and bundle.render is not None
                self.assertGreaterEqual(len(bundle.selection.omitted), 300)
                self.assertLessEqual(
                    bundle.render.chars + 1, context.delivery.max_chars
                )
                if output_format == "json":
                    payload = json.loads(bundle.render.text)
                    self.assertIsNone(payload["render"])
                    self.assertEqual(len(bundle.render.text), bundle.render.chars)

    def test_selected_evidence_exposes_compact_acquisition_provenance(self) -> None:
        for output_format in ("text", "json"):
            with self.subTest(output_format=output_format):
                handler = default_symbol_handler()
                context = fake_context(handler, output_format=output_format)
                bundle = inspect(symbol_request(), context)
                assert bundle.selection is not None and bundle.render is not None
                self.assertTrue(bundle.selection.selected)
                for item in bundle.selection.selected:
                    provenance = item.provenance
                    self.assertIsNotNone(provenance)
                    assert provenance is not None
                    self.assertEqual(provenance.provider, "fake-language")
                    self.assertEqual(provenance.provider_version, "fake-1.0")
                    self.assertEqual(provenance.status, CollectionStatus.COMPLETED)
                    self.assertTrue(provenance.acquisition_id)
                    self.assertTrue(provenance.source_versions)
                    self.assertTrue(provenance.coverage.is_complete())
                    self.assertIn(
                        item.variant.source.path,
                        {stamp.path for stamp in provenance.source_versions},
                    )
                if output_format == "text":
                    self.assertIn(
                        "provenance: fake-language@fake-1.0", bundle.render.text
                    )
                else:
                    payload = json.loads(bundle.render.text)
                    selected = payload["selection"]["selected"]
                    self.assertEqual(
                        selected[0]["provenance"]["provider"], "fake-language"
                    )
                    self.assertTrue(selected[0]["provenance"]["source_versions"])
                    self.assertNotIn("acquisitions", payload)
                    self.assertNotIn("observations", payload)

    def test_ambiguous_candidate_list_stays_within_the_delivery_budget(self) -> None:
        for output_format in ("text", "json"):
            with self.subTest(output_format=output_format):
                handler = default_symbol_handler()
                handler.results[Capability.FIND_DECLARATIONS] = _declaration_flood(80)
                context = fake_context(
                    handler,
                    output_format=output_format,
                    limits=AcquisitionLimits(
                        max_observations=200, max_source_files=200
                    ),
                )
                bundle = inspect(symbol_request(), context)
                self.assertIsInstance(bundle.resolution, AmbiguousTarget)
                assert isinstance(bundle.resolution, AmbiguousTarget)
                assert bundle.render is not None
                self.assertLessEqual(
                    bundle.render.chars + 1, context.delivery.max_chars
                )
                self.assertEqual(bundle.resolution.candidate_total, 80)
                reduced = len(bundle.resolution.candidates) < 80
                self.assertEqual(reduced, output_format == "json")
                if reduced:
                    self.assertTrue(
                        any(gap.code == "delivery_budget" for gap in bundle.gaps)
                    )
                    payload = json.loads(bundle.render.text)
                    self.assertEqual(payload["render"], None)
                    self.assertEqual(len(bundle.render.text), bundle.render.chars)

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

    def test_unstable_source_cannot_satisfy_a_required_source(self) -> None:
        handler = default_symbol_handler()
        declaration = declaration_result()
        handler.results[Capability.FIND_DECLARATIONS] = CapabilityResult(
            status=declaration.status,
            observations=declaration.observations,
            variants=tuple(
                item
                for item in declaration.variants
                if item.representation is RepresentationKind.SIGNATURE
            ),
            coverage=declaration.coverage,
            provider_version=declaration.provider_version,
        )
        changed_path = "src/orders/changed.ts"
        handler.results[Capability.READ_SOURCE] = source_result(
            path=changed_path, version=VERSION
        )
        context = fake_context(
            handler,
            versions={
                DEFAULT_PATH: VERSION,
                changed_path: CHANGED_VERSION,
                REFERENCE_PATH: VERSION,
                PACKAGE_PATH: VERSION,
                TEST_PATH: VERSION,
            },
        )
        bundle = inspect(symbol_request(), context)
        assert bundle.assessment is not None
        assert bundle.selection is not None
        assert bundle.render is not None
        target_source = bundle.assessment.by_id("target_source")
        self.assertIsNotNone(target_source)
        assert target_source is not None
        self.assertIs(target_source.status, RequirementStatus.UNSATISFIED)
        self.assertIn("unstable", target_source.detail or "")
        declaration_identity = bundle.assessment.by_id("declaration_identity")
        self.assertIsNotNone(declaration_identity)
        assert declaration_identity is not None
        self.assertIs(declaration_identity.status, RequirementStatus.SATISFIED)
        self.assertTrue(
            any(item.reason == "source_unstable" for item in bundle.selection.omitted)
        )
        self.assertNotIn("repository.all()", bundle.render.text)

    def test_stable_sibling_still_satisfies_when_one_reference_is_unstable(
        self,
    ) -> None:
        handler = default_symbol_handler()
        stable_path = "src/use_stable.ts"
        changed_path = "src/use_changed.ts"
        stable = reference_result(path=stable_path, version=VERSION)
        changed = reference_result(path=changed_path, version=VERSION)
        handler.results[Capability.SEMANTIC_REFERENCES] = CapabilityResult(
            status=CollectionStatus.COMPLETED,
            observations=(*stable.observations, *changed.observations),
            variants=(*stable.variants, *changed.variants),
            coverage=stable.coverage,
            provider_version="fake-1.0",
        )
        context = fake_context(
            handler,
            versions={
                DEFAULT_PATH: VERSION,
                stable_path: VERSION,
                changed_path: CHANGED_VERSION,
                PACKAGE_PATH: VERSION,
                TEST_PATH: VERSION,
            },
        )
        bundle = inspect(symbol_request(), context)
        assert bundle.assessment is not None
        assert bundle.selection is not None
        reference = bundle.assessment.by_id("representative_reference")
        self.assertIsNotNone(reference)
        assert reference is not None
        self.assertIs(reference.status, RequirementStatus.SATISFIED)
        selected_paths = {
            item.variant.source.path for item in bundle.selection.selected
        }
        self.assertIn(stable_path, selected_paths)
        self.assertNotIn(changed_path, selected_paths)
        self.assertTrue(
            any(item.reason == "source_unstable" for item in bundle.selection.omitted)
        )


class ExecutionLimitTests(unittest.TestCase):
    def test_provider_call_limit_counts_resolution_and_collection(self) -> None:
        handler = default_symbol_handler()
        context = fake_context(handler, limits=AcquisitionLimits(max_provider_calls=3))
        bundle = inspect(symbol_request(), context)
        self.assertEqual(
            [call[0] for call in handler.calls],
            ["find_declarations", "read_source", "semantic_references"],
        )
        self.assertTrue(any(gap.code == "provider_call_limit" for gap in bundle.gaps))
        assert bundle.assessment is not None
        self.assertIs(
            bundle.assessment.by_id("test_search").status,  # type: ignore[union-attr]
            RequirementStatus.UNSATISFIED,
        )

    def test_each_inspection_starts_with_fresh_execution_accounting(self) -> None:
        handler = default_symbol_handler()
        context = fake_context(handler, limits=AcquisitionLimits(max_provider_calls=3))
        first = inspect(symbol_request(), context)
        second = inspect(symbol_request(), context)
        self.assertIsInstance(first.resolution, ResolvedTarget)
        self.assertIsInstance(second.resolution, ResolvedTarget)
        self.assertEqual(len(handler.calls), 6)

    def test_expired_deadline_stops_every_adapter_invocation(self) -> None:
        ticks = itertools.count()
        handler = default_symbol_handler()
        context = fake_context(handler)
        context = replace(
            context,
            execution=ExecutionLedger(
                context.limits, clock=lambda: float(next(ticks) * 100)
            ),
        )
        bundle = inspect(symbol_request(), context)
        self.assertEqual(handler.calls, [])
        self.assertTrue(any(gap.code == "deadline_exceeded" for gap in bundle.gaps))

    def test_observation_limit_truncates_collected_evidence(self) -> None:
        handler = default_symbol_handler()
        handler.results[Capability.SEMANTIC_REFERENCES] = _reference_flood(10)
        context = fake_context(handler, limits=AcquisitionLimits(max_observations=5))
        bundle = inspect(symbol_request(), context)
        self.assertTrue(any(gap.code == "observation_limit" for gap in bundle.gaps))

    def test_source_file_limit_bounds_aggregate_evidence_files(self) -> None:
        handler = default_symbol_handler()
        handler.results[Capability.SEMANTIC_REFERENCES] = _reference_flood(10)
        context = fake_context(
            handler,
            limits=AcquisitionLimits(max_observations=100, max_source_files=2),
        )
        bundle = inspect(symbol_request(), context)
        self.assertTrue(any(gap.code == "source_file_limit" for gap in bundle.gaps))

    def test_inspection_batches_symbol_operations_for_one_subject(self) -> None:
        handler = default_symbol_handler()
        handler.batchable = frozenset(
            {Capability.SEMANTIC_REFERENCES, Capability.IMPLEMENTATIONS}
        )
        bundle = inspect(symbol_request(intent="refactor"), fake_context(handler))
        self.assertEqual(len(handler.batch_calls), 1)
        self.assertEqual(
            set(handler.batch_calls[0]),
            {"semantic_references", "implementations"},
        )
        assert bundle.collection is not None
        self.assertEqual(
            {item.capability for item in bundle.collection.requests},
            {
                Capability.READ_SOURCE,
                Capability.SEMANTIC_REFERENCES,
                Capability.IMPLEMENTATIONS,
                Capability.LEXICAL_MENTIONS,
                Capability.OWNING_PACKAGE,
            },
        )


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
