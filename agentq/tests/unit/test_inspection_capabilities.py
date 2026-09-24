"""Capability registry semantics: unsupported, unavailable, empty, and partial."""

from __future__ import annotations

import unittest
from dataclasses import replace

from agentq.core import PARTIAL, typed_coverage
from agentq.inspection.budgeting import AcquisitionLimits
from agentq.inspection.capabilities import CapabilityRegistry
from agentq.inspection.contracts import (
    AcquisitionRecord,
    AvailabilityStatus,
    CandidateTarget,
    Capability,
    CapabilityAvailability,
    CapabilityResult,
    CollectionStatus,
    DeclarationCandidate,
    EvidenceRequest,
    InspectionRequest,
    Intent,
    PathTarget,
    SourceSpan,
    SymbolTarget,
    make_declaration_candidate,
)
from agentq.inspection.execution import ExecutionLedger
from tests.support.inspection_fakes import (
    FakeHandler,
    declaration_result,
    empty_result,
    fake_context,
)


def _symbol_request(symbol: str = "listOrders") -> InspectionRequest:
    return InspectionRequest(
        target=SymbolTarget(name=symbol), intent=Intent.UNDERSTAND, request_id="req-1"
    )


def _request(capability: Capability, target=None) -> EvidenceRequest:
    return EvidenceRequest(
        request_id="req-1",
        capability=capability,
        target=target or SymbolTarget(name="listOrders"),
        limit=10,
    )


class UnsupportedCapabilityTests(unittest.TestCase):
    def test_missing_handler_reports_unsupported(self) -> None:
        registry = CapabilityRegistry()
        report = registry.describe(_symbol_request(), fake_context(None))
        for capability in Capability:
            entry = report.entries_for(capability)[0]
            self.assertIs(entry.status, AvailabilityStatus.UNSUPPORTED)
        gap = report.gap_for(Capability.FIND_DECLARATIONS)
        self.assertIsNotNone(gap)
        self.assertIs(gap.status, AvailabilityStatus.UNSUPPORTED)  # type: ignore[union-attr]

    def test_handlers_for_excludes_unsupported(self) -> None:
        registry = CapabilityRegistry()
        self.assertEqual(
            registry.handlers_for(
                Capability.FIND_DECLARATIONS,
                SymbolTarget(name="listOrders"),
                fake_context(None),
            ),
            (),
        )


class AvailabilityTests(unittest.TestCase):
    def test_unavailable_runtime_is_not_unsupported(self) -> None:
        handler = FakeHandler(
            name="fake-ts",
            supported=frozenset({Capability.FIND_DECLARATIONS}),
            availability_by_capability={
                Capability.FIND_DECLARATIONS: CapabilityAvailability(
                    available=False, reason="node runtime missing"
                )
            },
        )
        report = CapabilityRegistry((handler,)).describe(
            _symbol_request(), fake_context(handler)
        )
        entry = report.entries_for(Capability.FIND_DECLARATIONS)[0]
        self.assertIs(entry.status, AvailabilityStatus.UNAVAILABLE)
        self.assertEqual(entry.reason, "node runtime missing")
        gap = report.gap_for(Capability.FIND_DECLARATIONS)
        self.assertEqual(gap.reason, "node runtime missing")  # type: ignore[union-attr]

    def test_not_applicable_adapter_stays_distinct(self) -> None:
        handler = FakeHandler(
            name="fake-py",
            supported=frozenset({Capability.FIND_DECLARATIONS}),
            applicable_to=lambda target: isinstance(target, PathTarget),
        )
        registry = CapabilityRegistry((handler,))
        report = registry.describe(_symbol_request(), fake_context(handler))
        entry = report.entries_for(Capability.FIND_DECLARATIONS)[0]
        self.assertIs(entry.status, AvailabilityStatus.NOT_APPLICABLE)
        self.assertEqual(
            registry.handlers_for(
                Capability.FIND_DECLARATIONS,
                CandidateTarget(candidate_id="c", symbol="s"),
                fake_context(handler),
            ),
            (),
        )


class AcquisitionOutcomeTests(unittest.TestCase):
    def test_empty_is_a_completed_explicit_outcome(self) -> None:
        handler = FakeHandler(
            name="fake",
            supported=frozenset({Capability.FIND_DECLARATIONS}),
            results={Capability.FIND_DECLARATIONS: empty_result()},
        )
        registry = CapabilityRegistry((handler,))
        results = registry.acquire(
            _request(Capability.FIND_DECLARATIONS), fake_context(handler)
        )
        self.assertEqual(len(results), 1)
        record = results[0].record
        self.assertIs(record.status, CollectionStatus.EMPTY)
        self.assertTrue(record.coverage.is_complete())
        self.assertEqual(results[0].observations, ())

    def test_failed_acquisition_records_a_diagnostic(self) -> None:
        handler = FakeHandler(
            name="fake",
            supported=frozenset({Capability.FIND_DECLARATIONS}),
            acquire_error=RuntimeError("bridge crashed"),
        )
        registry = CapabilityRegistry((handler,))
        results = registry.acquire(
            _request(Capability.FIND_DECLARATIONS), fake_context(handler)
        )
        record = results[0].record
        self.assertIs(record.status, CollectionStatus.FAILED)
        self.assertFalse(record.coverage.is_complete())
        self.assertEqual(record.diagnostics[0].message, "bridge crashed")
        self.assertEqual(results[0].observations, ())

    def test_partial_acquisition_keeps_its_coverage(self) -> None:
        handler = FakeHandler(
            name="fake",
            supported=frozenset({Capability.FIND_DECLARATIONS}),
            results={
                Capability.FIND_DECLARATIONS: CapabilityResult(
                    status=CollectionStatus.PARTIAL,
                    observations=declaration_result().observations,
                    variants=declaration_result().variants,
                    coverage=typed_coverage(PARTIAL, "result_limit"),
                )
            },
        )
        registry = CapabilityRegistry((handler,))
        results = registry.acquire(
            _request(Capability.FIND_DECLARATIONS), fake_context(handler)
        )
        self.assertIs(results[0].record.status, CollectionStatus.PARTIAL)
        self.assertFalse(results[0].record.coverage.is_complete())

    def test_observations_carry_the_acquisition_id(self) -> None:
        handler = FakeHandler(
            name="fake",
            supported=frozenset({Capability.FIND_DECLARATIONS}),
            results={Capability.FIND_DECLARATIONS: declaration_result()},
        )
        acquired = CapabilityRegistry((handler,)).acquire(
            _request(Capability.FIND_DECLARATIONS), fake_context(handler)
        )[0]
        for observation in acquired.observations:
            self.assertEqual(observation.acquisition_id, acquired.record.acquisition_id)

    def test_two_handlers_are_both_acquired(self) -> None:
        first = FakeHandler(
            name="fake-a",
            supported=frozenset({Capability.FIND_DECLARATIONS}),
            results={Capability.FIND_DECLARATIONS: declaration_result("first")},
        )
        second = FakeHandler(
            name="fake-b",
            supported=frozenset({Capability.FIND_DECLARATIONS}),
            results={Capability.FIND_DECLARATIONS: declaration_result("second")},
        )
        registry = CapabilityRegistry((first, second))
        context = fake_context((first, second))
        results = registry.acquire(_request(Capability.FIND_DECLARATIONS), context)
        self.assertEqual(
            {item.record.provider for item in results}, {"fake-a", "fake-b"}
        )
        self.assertNotEqual(
            results[0].record.acquisition_id, results[1].record.acquisition_id
        )


def _explode(*_args, **_kwargs):
    raise RuntimeError("broken adapter")


class BrokenAdapterTests(unittest.TestCase):
    def test_applicability_failure_is_reported_not_raised(self) -> None:
        handler = FakeHandler(
            name="broken",
            supported=frozenset({Capability.FIND_DECLARATIONS}),
            applicable_to=_explode,
        )
        registry = CapabilityRegistry((handler,))
        report = registry.describe(_symbol_request(), fake_context(handler))
        entry = report.entries_for(Capability.FIND_DECLARATIONS)[0]
        self.assertIs(entry.status, AvailabilityStatus.UNAVAILABLE)
        self.assertIn("applicability check failed", entry.reason or "")
        self.assertEqual(
            registry.handlers_for(
                Capability.FIND_DECLARATIONS,
                SymbolTarget(name="listOrders"),
                fake_context(handler),
            ),
            (),
        )

    def test_availability_failure_is_reported_not_raised(self) -> None:
        handler = FakeHandler(
            name="broken",
            supported=frozenset({Capability.FIND_DECLARATIONS}),
        )
        handler.availability = _explode  # type: ignore[method-assign]
        registry = CapabilityRegistry((handler,))
        report = registry.describe(_symbol_request(), fake_context(handler))
        entry = report.entries_for(Capability.FIND_DECLARATIONS)[0]
        self.assertIs(entry.status, AvailabilityStatus.UNAVAILABLE)
        self.assertIn("availability check failed", entry.reason or "")


class SubjectApplicabilityTests(unittest.TestCase):
    def _candidate(self, path: str):
        return make_declaration_candidate(
            provider="fake",
            path=path,
            source_version="v1",
            kind="function",
            span=SourceSpan(start_line=1, end_line=2),
            signature="function target()",
        )

    def test_handlers_for_respects_the_resolved_subject_language(self) -> None:
        typescript = FakeHandler(
            name="fake-ts",
            supported=frozenset({Capability.SEMANTIC_REFERENCES}),
            subject_applicable_to=lambda subject: subject.path.endswith(".ts"),
        )
        python = FakeHandler(
            name="fake-py",
            supported=frozenset({Capability.SEMANTIC_REFERENCES}),
            subject_applicable_to=lambda subject: subject.path.endswith(".py"),
        )
        registry = CapabilityRegistry((typescript, python))
        context = fake_context((typescript, python))
        handlers = registry.handlers_for(
            Capability.SEMANTIC_REFERENCES,
            SymbolTarget(name="target"),
            context,
            self._candidate("src/service.py"),
        )
        self.assertEqual([handler.name for handler in handlers], ["fake-py"])

    def test_describe_reports_not_applicable_for_the_other_language(self) -> None:
        typescript = FakeHandler(
            name="fake-ts",
            supported=frozenset({Capability.SEMANTIC_REFERENCES}),
            subject_applicable_to=lambda subject: subject.path.endswith(".ts"),
        )
        report = CapabilityRegistry((typescript,)).describe(
            _symbol_request(),
            fake_context(typescript),
            subject=self._candidate("src/service.py"),
        )
        entry = report.entries_for(Capability.SEMANTIC_REFERENCES)[0]
        self.assertIs(entry.status, AvailabilityStatus.NOT_APPLICABLE)
        self.assertFalse(report.available(Capability.SEMANTIC_REFERENCES))


class BatchingTests(unittest.TestCase):
    def _request(self, capability: Capability, subject):
        return EvidenceRequest(
            request_id=f"req-{capability.value}",
            capability=capability,
            target=SymbolTarget(name="target"),
            subject=subject,
            limit=10,
        )

    def _candidate(self, path: str):
        return make_declaration_candidate(
            provider="fake",
            path=path,
            source_version="v1",
            kind="function",
            span=SourceSpan(start_line=1, end_line=2),
            signature="function target()",
        )

    def test_acquire_many_batches_one_subject_into_one_invocation(self) -> None:
        handler = FakeHandler(
            name="fake",
            supported=frozenset(
                {Capability.SEMANTIC_REFERENCES, Capability.IMPLEMENTATIONS}
            ),
            batchable=frozenset(
                {Capability.SEMANTIC_REFERENCES, Capability.IMPLEMENTATIONS}
            ),
        )
        registry = CapabilityRegistry((handler,))
        subject = self._candidate("src/service.ts")
        results = registry.acquire_many(
            (
                self._request(Capability.SEMANTIC_REFERENCES, subject),
                self._request(Capability.IMPLEMENTATIONS, subject),
            ),
            fake_context(handler),
        )
        self.assertEqual(len(results), 2)
        self.assertEqual(
            handler.batch_calls, [("semantic_references", "implementations")]
        )

    def test_acquire_many_does_not_batch_different_subjects(self) -> None:
        handler = FakeHandler(
            name="fake",
            supported=frozenset(
                {Capability.SEMANTIC_REFERENCES, Capability.IMPLEMENTATIONS}
            ),
            batchable=frozenset(
                {Capability.SEMANTIC_REFERENCES, Capability.IMPLEMENTATIONS}
            ),
        )
        registry = CapabilityRegistry((handler,))
        results = registry.acquire_many(
            (
                self._request(
                    Capability.SEMANTIC_REFERENCES, self._candidate("src/a.ts")
                ),
                self._request(Capability.IMPLEMENTATIONS, self._candidate("src/b.ts")),
            ),
            fake_context(handler),
        )
        self.assertEqual(len(results), 2)
        self.assertEqual(handler.batch_calls, [])

    def test_acquire_many_uses_singles_for_a_non_batching_handler(self) -> None:
        handler = FakeHandler(
            name="fake",
            supported=frozenset(
                {Capability.SEMANTIC_REFERENCES, Capability.IMPLEMENTATIONS}
            ),
        )
        registry = CapabilityRegistry((handler,))
        subject = self._candidate("src/service.ts")
        results = registry.acquire_many(
            (
                self._request(Capability.SEMANTIC_REFERENCES, subject),
                self._request(Capability.IMPLEMENTATIONS, subject),
            ),
            fake_context(handler),
        )
        self.assertEqual(len(results), 2)
        self.assertEqual(handler.batch_calls, [])
        self.assertEqual(
            [call[0] for call in handler.calls],
            ["semantic_references", "implementations"],
        )


class RequestIdentityTests(unittest.TestCase):
    def _candidate(
        self, path: str, *, version: str = "v1", start: int = 1, end: int = 2
    ) -> DeclarationCandidate:
        return make_declaration_candidate(
            provider="fake",
            path=path,
            source_version=version,
            kind="function",
            span=SourceSpan(start_line=start, end_line=end),
            signature="function target()",
        )

    def _request(self, subject: DeclarationCandidate) -> EvidenceRequest:
        return EvidenceRequest(
            request_id="req-1",
            capability=Capability.SEMANTIC_REFERENCES,
            target=SymbolTarget(name="target"),
            subject=subject,
            limit=10,
        )

    def _handler(self, *, error: Exception | None = None) -> FakeHandler:
        return FakeHandler(
            name="fake",
            supported=frozenset({Capability.SEMANTIC_REFERENCES}),
            results={Capability.SEMANTIC_REFERENCES: declaration_result()},
            acquire_error=error,
        )

    def _record(
        self,
        request: EvidenceRequest,
        *,
        handler: FakeHandler | None = None,
        ledger: ExecutionLedger | None = None,
    ) -> AcquisitionRecord:
        handler = handler or self._handler()
        context = fake_context(handler)
        if ledger is not None:
            context = replace(context, execution=ledger)
        acquired = CapabilityRegistry((handler,)).acquire(request, context)
        return acquired[0].record

    def test_same_name_subjects_at_distinct_paths_do_not_collide(self) -> None:
        first = self._record(self._request(self._candidate("src/a.ts")))
        second = self._record(self._request(self._candidate("src/b.ts")))
        self.assertNotEqual(first.acquisition_id, second.acquisition_id)

    def test_same_file_subjects_at_distinct_spans_do_not_collide(self) -> None:
        first = self._record(self._request(self._candidate("src/a.ts", start=1, end=2)))
        second = self._record(
            self._request(self._candidate("src/a.ts", start=8, end=9))
        )
        self.assertNotEqual(first.acquisition_id, second.acquisition_id)

    def test_changed_subject_version_changes_the_fingerprint(self) -> None:
        first = self._record(self._request(self._candidate("src/a.ts", version="v1")))
        second = self._record(self._request(self._candidate("src/a.ts", version="v2")))
        self.assertNotEqual(first.acquisition_id, second.acquisition_id)

    def test_failed_acquisitions_follow_the_same_identity_rules(self) -> None:
        handler = self._handler(error=RuntimeError("bridge crashed"))
        first = self._record(
            self._request(self._candidate("src/a.ts")), handler=handler
        )
        second = self._record(
            self._request(self._candidate("src/b.ts")), handler=handler
        )
        self.assertIs(first.status, CollectionStatus.FAILED)
        self.assertNotEqual(first.acquisition_id, second.acquisition_id)

    def test_limited_acquisitions_follow_the_same_identity_rules(self) -> None:
        ledger = ExecutionLedger(AcquisitionLimits(max_provider_calls=1))
        ledger.charge_call()
        first = self._record(self._request(self._candidate("src/a.ts")), ledger=ledger)
        second = self._record(self._request(self._candidate("src/b.ts")), ledger=ledger)
        self.assertIs(first.status, CollectionStatus.UNAVAILABLE)
        self.assertNotEqual(first.acquisition_id, second.acquisition_id)


class ThirdLanguageIndependenceTests(unittest.TestCase):
    def test_adapter_applicability_is_target_dependent(self) -> None:
        python_fake = FakeHandler(
            name="fake-py",
            supported=frozenset({Capability.FIND_DECLARATIONS}),
            applicable_to=lambda target: (
                isinstance(target, SymbolTarget) and target.name.endswith("Py")
            ),
            results={Capability.FIND_DECLARATIONS: declaration_result("thingPy")},
        )
        ts_fake = FakeHandler(
            name="fake-ts",
            supported=frozenset({Capability.FIND_DECLARATIONS}),
            applicable_to=lambda target: (
                isinstance(target, SymbolTarget) and target.name.endswith("Ts")
            ),
            results={Capability.FIND_DECLARATIONS: declaration_result("thingTs")},
        )
        registry = CapabilityRegistry((python_fake, ts_fake))
        report = registry.describe(
            _symbol_request("thingPy"), fake_context((python_fake, ts_fake))
        )
        statuses = {
            (entry.provider, entry.status)
            for entry in report.entries_for(Capability.FIND_DECLARATIONS)
        }
        self.assertEqual(
            statuses,
            {
                ("fake-py", AvailabilityStatus.AVAILABLE),
                ("fake-ts", AvailabilityStatus.NOT_APPLICABLE),
            },
        )
        results = registry.acquire(
            _request(Capability.FIND_DECLARATIONS, SymbolTarget(name="thingPy")),
            fake_context((python_fake, ts_fake)),
        )
        self.assertEqual([item.record.provider for item in results], ["fake-py"])
        self.assertEqual(ts_fake.calls, [])
