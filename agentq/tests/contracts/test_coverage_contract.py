#!/usr/bin/env python3
from __future__ import annotations

import copy
import unittest

from agentq.core import ContractError, evidence


class CoverageWireTests(unittest.TestCase):
    def test_legacy_wire_shape_and_reasons_are_preserved(self) -> None:
        self.assertEqual(evidence.complete(), {"status": "complete", "reason": []})
        self.assertEqual(
            evidence.coverage(evidence.PARTIAL, evidence.PARSE_ERROR),
            {"status": "partial", "reason": ["parse_error"]},
        )
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
    def test_merge_is_idempotent_and_without_input_is_unknown(self) -> None:
        complete = evidence.typed_coverage(evidence.COMPLETE)
        self.assertEqual(evidence.merge_typed(complete, complete), complete)
        sampled = evidence.typed_coverage(
            evidence.SAMPLED, evidence.RESULT_LIMIT, matched=5
        )
        self.assertEqual(
            evidence.merge_typed(sampled, sampled).to_wire(), sampled.to_wire()
        )
        self.assertEqual(evidence.merge_typed().status, evidence.UNKNOWN)
        self.assertEqual(evidence.merge_coverage(), {"status": "unknown", "reason": []})

    def test_merge_takes_the_weakest_status_and_unions_reasons_in_order(self) -> None:
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
        union = evidence.merge_typed(
            evidence.typed_coverage(evidence.SAMPLED, evidence.RESULT_LIMIT),
            evidence.typed_coverage(evidence.PARTIAL, evidence.PARSE_ERROR),
            evidence.typed_coverage(evidence.UNKNOWN, evidence.PROVIDER_UNAVAILABLE),
        )
        self.assertEqual(union.status, evidence.UNKNOWN)
        self.assertEqual(
            union.reasons,
            (
                evidence.RESULT_LIMIT,
                evidence.PARSE_ERROR,
                evidence.PROVIDER_UNAVAILABLE,
            ),
        )
        # A stronger operand never repairs a weaker one.
        self.assertEqual(
            evidence.merge_typed(
                evidence.typed_coverage(evidence.COMPLETE),
                evidence.typed_coverage(evidence.PARTIAL, evidence.PARSE_ERROR),
            ).status,
            evidence.PARTIAL,
        )

    def test_measurement_identity_matrix(self) -> None:
        """Counts survive only under an identical measurement identity and report."""
        cases = (
            ("same", "same", "same", "preserve"),
            ("same", "same", "different", "counts"),
            ("same", "different", "same", "measurement"),
            ("different", "same", "same", "measurement"),
            ("different", "different", "same", "measurement"),
        )
        for domain, scope, counts, expected in cases:
            with self.subTest(domain=domain, scope=scope, counts=counts):
                left = self._measurement(
                    domain="references", scope="package-a", matched=12
                )
                right = self._measurement(
                    domain="references" if domain == "same" else "files",
                    scope="package-a" if scope == "same" else "package-b",
                    matched=12 if counts == "same" else 7,
                )
                merged = evidence.merge_typed(left, right)
                if expected == "preserve":
                    self.assertEqual(merged.to_wire(), left.to_wire())
                    continue
                if expected == "counts":
                    self.assertEqual(merged.domain, "references")
                    self.assertEqual(merged.scope, "package-a")
                else:
                    self.assertIsNone(merged.domain)
                    self.assertIsNone(merged.scope)
                self.assertIsNone(merged.matched)
                self.assertIsNone(merged.scanned)
                self.assertEqual(merged.count_quality, evidence.UNKNOWN_COUNT)

    @staticmethod
    def _measurement(*, domain: str, scope: str, matched: int) -> evidence.Coverage:
        return evidence.typed_coverage(
            evidence.COMPLETE,
            domain=domain,
            scope=scope,
            matched=matched,
            count_quality=evidence.EXACT,
        )


class CoverageFailureAndOmissionTests(unittest.TestCase):
    def test_failure_and_omission_never_promote_their_input(self) -> None:
        complete = evidence.typed_from_wire(evidence.complete())
        failed = evidence.with_failure(complete, evidence.PARSE_ERROR)
        self.assertEqual(failed.status, evidence.PARTIAL)
        still_partial = evidence.with_failure(failed, evidence.PROVIDER_UNAVAILABLE)
        self.assertEqual(still_partial.status, evidence.PARTIAL)
        self.assertEqual(
            still_partial.reasons,
            (evidence.PARSE_ERROR, evidence.PROVIDER_UNAVAILABLE),
        )
        omitted = evidence.with_omission(complete, evidence.RESULT_LIMIT, omitted=4)
        self.assertEqual(omitted.status, evidence.SAMPLED)
        self.assertEqual(omitted.omitted, 4)
        kept_partial = evidence.with_omission(failed, evidence.RENDER_OMISSION)
        self.assertEqual(kept_partial.status, evidence.PARTIAL)
        self.assertEqual(
            kept_partial.reasons, (evidence.PARSE_ERROR, evidence.RENDER_OMISSION)
        )
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

    def test_partial_empty_is_not_complete(self) -> None:
        empty_partial = evidence.typed_from_wire(
            {"status": "partial", "reason": ["parse_error"]}
        )
        self.assertEqual(empty_partial.status, evidence.PARTIAL)
        self.assertEqual(empty_partial.reasons, (evidence.PARSE_ERROR,))
        self.assertIsNone(empty_partial.retained)
        self.assertFalse(empty_partial.is_complete())

    def test_is_complete_requires_complete_status(self) -> None:
        self.assertTrue(evidence.typed_from_wire(evidence.complete()).is_complete())
        self.assertFalse(
            evidence.typed_from_wire(evidence.coverage(evidence.SAMPLED)).is_complete()
        )
        self.assertFalse(evidence.typed_from_wire(None).is_complete())


class CoverageDecodeTests(unittest.TestCase):
    def test_decodes_legacy_string_dict_and_garbage(self) -> None:
        self.assertEqual(evidence.typed_from_wire("sampled").status, evidence.SAMPLED)
        self.assertEqual(
            evidence.typed_from_wire(
                {"status": "partial", "reason": ["parse_error"]}
            ).status,
            evidence.PARTIAL,
        )
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


class ProvenanceVocabularyTests(unittest.TestCase):
    def test_best_provenance_prefers_strongest_evidence(self) -> None:
        self.assertEqual(
            evidence.best_provenance("lexical", "semantic", "syntactic"), "semantic"
        )
        self.assertEqual(evidence.best_provenance("heuristic"), "heuristic")
        self.assertIsNone(evidence.best_provenance(None, None))


if __name__ == "__main__":
    unittest.main(verbosity=2)
