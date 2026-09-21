#!/usr/bin/env python3
from __future__ import annotations

import copy
import sys
import unittest
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))

from agentq_lib import evidence  # noqa: E402
from agentq_lib.contracts import ContractError  # noqa: E402


class CoverageWireTests(unittest.TestCase):
    def test_legacy_wire_shape_is_preserved(self) -> None:
        self.assertEqual(evidence.complete(), {"status": "complete", "reason": []})
        self.assertEqual(
            evidence.coverage(evidence.PARTIAL, evidence.PARSE_ERROR),
            {"status": "partial", "reason": ["parse_error"]},
        )

    def test_reasons_are_ordered_and_unique(self) -> None:
        block = evidence.typed_coverage(
            evidence.PARTIAL,
            evidence.PARSE_ERROR,
            evidence.RESULT_LIMIT,
            evidence.PARSE_ERROR,
        )
        self.assertEqual(block.reasons, (evidence.PARSE_ERROR, evidence.RESULT_LIMIT))
        self.assertEqual(block.to_wire()["reason"], ["parse_error", "result_limit"])

    def test_unknown_counts_stay_unknown_not_zero(self) -> None:
        block = evidence.typed_coverage(evidence.COMPLETE)
        self.assertIsNone(block.matched)
        self.assertNotIn("matched", block.to_wire())
        self.assertNotIn("count_quality", block.to_wire())
        exact = evidence.typed_coverage(
            evidence.COMPLETE,
            matched=3,
            scanned=9,
            count_quality=evidence.EXACT,
        )
        wire = exact.to_wire()
        self.assertEqual(wire["matched"], 3)
        self.assertEqual(wire["count_quality"], "exact")

    def test_invalid_coverage_inputs_are_rejected(self) -> None:
        cases = {
            "boolean count": lambda: evidence.typed_coverage(
                evidence.COMPLETE, matched=True
            ),
            "negative count": lambda: evidence.typed_coverage(
                evidence.COMPLETE, scanned=-1
            ),
            "unknown status": lambda: evidence.Coverage(status="finished"),
            "unknown count quality": lambda: evidence.Coverage(
                status=evidence.COMPLETE, count_quality="sort-of"
            ),
        }
        for label, call in cases.items():
            with self.subTest(label=label):
                with self.assertRaises(ContractError):
                    call()


class CoverageMergeLawTests(unittest.TestCase):
    def test_merge_is_idempotent(self) -> None:
        complete = evidence.typed_coverage(evidence.COMPLETE)
        self.assertEqual(evidence.merge_typed(complete, complete), complete)
        sampled = evidence.typed_coverage(
            evidence.SAMPLED, evidence.RESULT_LIMIT, matched=5
        )
        self.assertEqual(
            evidence.merge_typed(sampled, sampled).to_wire(), sampled.to_wire()
        )

    def test_merge_never_promotes(self) -> None:
        merged = evidence.merge_typed(
            evidence.typed_coverage(evidence.COMPLETE),
            evidence.typed_coverage(evidence.PARTIAL, evidence.PARSE_ERROR),
        )
        self.assertEqual(merged.status, evidence.PARTIAL)
        self.assertEqual(merged.reasons, (evidence.PARSE_ERROR,))
        unknown = evidence.merge_typed(
            evidence.typed_coverage(evidence.UNKNOWN), merged
        )
        self.assertEqual(unknown.status, evidence.UNKNOWN)

    def test_merge_without_input_is_unknown(self) -> None:
        self.assertEqual(evidence.merge_typed().status, evidence.UNKNOWN)
        self.assertEqual(evidence.merge_coverage(), {"status": "unknown", "reason": []})

    def test_merge_unions_reasons_in_order(self) -> None:
        merged = evidence.merge_typed(
            evidence.typed_coverage(evidence.SAMPLED, evidence.RESULT_LIMIT),
            evidence.typed_coverage(evidence.PARTIAL, evidence.PARSE_ERROR),
            evidence.typed_coverage(evidence.UNKNOWN, evidence.PROVIDER_UNAVAILABLE),
        )
        self.assertEqual(
            merged.reasons,
            (
                evidence.RESULT_LIMIT,
                evidence.PARSE_ERROR,
                evidence.PROVIDER_UNAVAILABLE,
            ),
        )
        self.assertEqual(merged.status, evidence.UNKNOWN)

    def test_merge_preserves_known_failures(self) -> None:
        merged = evidence.merge_typed(
            evidence.typed_coverage(evidence.COMPLETE),
            evidence.typed_coverage(evidence.PARTIAL, evidence.PARSE_ERROR),
            evidence.typed_coverage(evidence.PARTIAL, evidence.PROVIDER_UNAVAILABLE),
        )
        self.assertIn(evidence.PARSE_ERROR, merged.reasons)
        self.assertIn(evidence.PROVIDER_UNAVAILABLE, merged.reasons)

    def test_counts_merge_idempotently_and_unknown_dominates(self) -> None:
        merged = evidence.merge_typed(
            evidence.typed_coverage(
                evidence.COMPLETE, matched=2, count_quality=evidence.EXACT
            ),
            evidence.typed_coverage(
                evidence.COMPLETE, matched=3, count_quality=evidence.EXACT
            ),
        )
        self.assertEqual(merged.matched, 3)
        self.assertEqual(merged.count_quality, evidence.EXACT)
        unknown = evidence.merge_typed(
            evidence.typed_coverage(
                evidence.COMPLETE, matched=2, count_quality=evidence.EXACT
            ),
            evidence.typed_coverage(evidence.COMPLETE),
        )
        self.assertIsNone(unknown.matched)
        self.assertEqual(unknown.count_quality, evidence.UNKNOWN_COUNT)

    def test_sampled_plus_parse_error_keeps_both_causes(self) -> None:
        merged = evidence.merge_typed(
            evidence.typed_coverage(
                evidence.SAMPLED,
                evidence.RESULT_LIMIT,
                matched=5,
                count_quality=evidence.EXACT,
            ),
            evidence.typed_coverage(evidence.PARTIAL, evidence.PARSE_ERROR),
        )
        self.assertEqual(merged.status, evidence.PARTIAL)
        self.assertEqual(merged.reasons, (evidence.RESULT_LIMIT, evidence.PARSE_ERROR))
        self.assertFalse(merged.is_complete())


class CoverageFailureAndOmissionTests(unittest.TestCase):
    def test_partial_empty_is_not_complete(self) -> None:
        empty_partial = evidence.typed_from_wire(
            {"status": "partial", "reason": ["parse_error"]}
        )
        self.assertEqual(empty_partial.status, evidence.PARTIAL)
        self.assertEqual(empty_partial.reasons, (evidence.PARSE_ERROR,))
        self.assertIsNone(empty_partial.retained)
        self.assertFalse(empty_partial.is_complete())

    def test_omission_downgrades_and_records_reason(self) -> None:
        omitted = evidence.with_omission(
            evidence.complete(), evidence.RESULT_LIMIT, omitted=4
        )
        self.assertEqual(omitted.status, evidence.SAMPLED)
        self.assertEqual(omitted.reasons, (evidence.RESULT_LIMIT,))
        self.assertEqual(omitted.omitted, 4)

    def test_omission_never_upgrades_a_weaker_status(self) -> None:
        failed = evidence.with_failure(evidence.complete(), evidence.PARSE_ERROR)
        omitted = evidence.with_omission(failed, evidence.RENDER_OMISSION)
        self.assertEqual(omitted.status, evidence.PARTIAL)
        self.assertEqual(
            omitted.reasons, (evidence.PARSE_ERROR, evidence.RENDER_OMISSION)
        )

    def test_failure_never_promotes(self) -> None:
        failed = evidence.with_failure(evidence.complete(), evidence.PARSE_ERROR)
        self.assertEqual(failed.status, evidence.PARTIAL)
        still_partial = evidence.with_failure(failed, evidence.PROVIDER_UNAVAILABLE)
        self.assertEqual(still_partial.status, evidence.PARTIAL)
        self.assertEqual(
            still_partial.reasons,
            (evidence.PARSE_ERROR, evidence.PROVIDER_UNAVAILABLE),
        )

    def test_is_complete_requires_complete_status(self) -> None:
        self.assertTrue(evidence.typed_from_wire(evidence.complete()).is_complete())
        self.assertFalse(
            evidence.typed_from_wire(evidence.coverage(evidence.SAMPLED)).is_complete()
        )
        self.assertFalse(evidence.typed_from_wire(None).is_complete())


class CoverageDecodeTests(unittest.TestCase):
    def test_decodes_legacy_string_and_dict(self) -> None:
        self.assertEqual(evidence.typed_from_wire("sampled").status, evidence.SAMPLED)
        self.assertEqual(
            evidence.typed_from_wire(
                {"status": "partial", "reason": ["parse_error"]}
            ).status,
            evidence.PARTIAL,
        )

    def test_garbage_status_becomes_unknown_not_complete(self) -> None:
        self.assertEqual(evidence.typed_from_wire("finished").status, evidence.UNKNOWN)
        self.assertEqual(
            evidence.typed_from_wire({"status": 7}).status, evidence.UNKNOWN
        )
        self.assertEqual(evidence.typed_from_wire(None).status, evidence.UNKNOWN)

    def test_unknown_optional_fields_are_ignored(self) -> None:
        decoded = evidence.typed_from_wire(
            {
                "status": "sampled",
                "reason": ["result_limit"],
                "provider_note": "x",
            }
        )
        self.assertEqual(decoded.status, evidence.SAMPLED)

    def test_status_of_adapts_legacy_values(self) -> None:
        self.assertEqual(evidence.status_of("complete"), evidence.COMPLETE)
        self.assertEqual(evidence.status_of(None), evidence.UNKNOWN)
        self.assertEqual(evidence.status_of("bogus"), evidence.UNKNOWN)

    def test_serialization_does_not_mutate_or_alias_the_input(self) -> None:
        payload = {
            "status": "sampled",
            "reason": ["result_limit", "result_limit"],
            "matched": 3,
        }
        snapshot = copy.deepcopy(payload)
        decoded = evidence.typed_from_wire(payload)
        wire = decoded.to_wire()
        self.assertEqual(payload, snapshot)
        wire["reason"].append("tampered")
        self.assertEqual(decoded.reasons, (evidence.RESULT_LIMIT,))

    def test_downgrade_still_never_upgrades(self) -> None:
        current = evidence.coverage(evidence.PARTIAL, evidence.PARSE_ERROR)
        self.assertEqual(
            evidence.downgrade(current, evidence.COMPLETE)["status"], evidence.PARTIAL
        )
        self.assertEqual(
            evidence.downgrade(
                current, evidence.PARTIAL, evidence.PROVIDER_UNAVAILABLE
            )["reason"],
            [evidence.PARSE_ERROR, evidence.PROVIDER_UNAVAILABLE],
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
