"""Capability registry semantics: unsupported, unavailable, empty, and partial."""

from __future__ import annotations

import unittest

from agentq.core import PARTIAL, typed_coverage
from agentq.inspection.capabilities import CapabilityRegistry
from agentq.inspection.contracts import (
    AvailabilityStatus,
    CandidateTarget,
    Capability,
    CapabilityAvailability,
    CapabilityResult,
    CollectionStatus,
    EvidenceRequest,
    InspectionRequest,
    Intent,
    PathTarget,
    SymbolTarget,
)
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


class ThirdLanguageIndependenceTests(unittest.TestCase):
    def test_adapter_applicability_is_target_dependent(self) -> None:
        python_fake = FakeHandler(
            name="fake-py",
            supported=frozenset({Capability.FIND_DECLARATIONS}),
            applicable_to=lambda target: isinstance(target, SymbolTarget)
            and target.name.endswith("Py"),
            results={Capability.FIND_DECLARATIONS: declaration_result("thingPy")},
        )
        ts_fake = FakeHandler(
            name="fake-ts",
            supported=frozenset({Capability.FIND_DECLARATIONS}),
            applicable_to=lambda target: isinstance(target, SymbolTarget)
            and target.name.endswith("Ts"),
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
