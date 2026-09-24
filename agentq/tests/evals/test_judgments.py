"""Alias-based draft compilation into capture-bound judgment sets."""

from __future__ import annotations

import json
import unittest
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory

from agentq.core import ContractError
from evals.build_fixtures import build_fixture
from evals.codec import (
    capture_digest,
    decode_judgment,
    encode_judgment,
    judgment_digest,
)
from evals.judgments import compile_judgments, load_draft, parse_draft
from evals.metrics import facet_supported, witness_supported
from evals.store import CaptureStore
from tests.evals.support import (
    JUDGMENT_DIR,
)
from tests.evals.support import (
    compiled as _compiled,
)

CASE_IDS = (
    "basic-edit",
    "lexical-decoy",
    "same-file-quota",
    "variant-fallback",
    "required-upgrade",
    "empty-test-search",
    "unstable-source",
    "delivery-overhead",
)


def _draft(case_id: str = "basic-edit"):
    return load_draft(JUDGMENT_DIR / f"{case_id}.json")


def _wire(case_id: str = "basic-edit") -> dict[str, object]:
    value = json.loads((JUDGMENT_DIR / f"{case_id}.json").read_text(encoding="utf-8"))
    assert isinstance(value, dict)
    return value


class CompileTests(unittest.TestCase):
    def test_every_authored_draft_compiles_against_its_capture(self) -> None:
        for case_id in CASE_IDS:
            with self.subTest(case_id=case_id):
                fixture, judgment = _compiled(case_id)
                self.assertEqual(judgment.case_id, case_id)
                self.assertEqual(judgment.capture_id, capture_digest(fixture.capture))
                self.assertEqual(judgment.basis, "target_intent")
                self.assertEqual(judgment.review_status, "reviewed")
                self.assertTrue(judgment.facets)
                for facet in judgment.facets:
                    for clause in facet.witness_sets:
                        for witness_id in clause:
                            self.assertIsNotNone(judgment.witness(witness_id))

    def test_unknown_alias_is_a_validation_error(self) -> None:
        fixture = build_fixture("basic-edit")
        draft = replace(
            _draft(),
            witnesses=(
                replace(_draft().witnesses[0], acceptable_aliases=("missing.alias",)),
                *_draft().witnesses[1:],
            ),
        )
        with self.assertRaises(ContractError):
            compile_judgments(draft, fixture.variant_aliases, fixture.capture)

    def test_unreviewed_draft_is_rejected(self) -> None:
        fixture = build_fixture("basic-edit")
        draft = replace(_draft(), review_status="draft")
        with self.assertRaises(ContractError):
            compile_judgments(draft, fixture.variant_aliases, fixture.capture)

    def test_case_mismatch_is_rejected(self) -> None:
        fixture = build_fixture("basic-edit")
        draft = _draft("lexical-decoy")
        with self.assertRaises(ContractError):
            compile_judgments(draft, fixture.variant_aliases, fixture.capture)

    def test_absent_witness_stays_absent_and_never_satisfies(self) -> None:
        fixture, judgment = _compiled("empty-test-search")
        witness = judgment.witness("test.evidence")
        assert witness is not None
        self.assertEqual(witness.acceptable_variant_ids, ())
        every_variant = frozenset(
            variant.variant_id for variant in fixture.capture.decision.pool.variants
        )
        self.assertFalse(witness_supported(judgment, "test.evidence", every_variant))
        facet = next(item for item in judgment.facets if item.facet_id == "test-signal")
        self.assertFalse(facet_supported(facet, judgment, every_variant))

    def test_empty_clause_is_rejected(self) -> None:
        wire = _wire()
        facets = wire["facets"]
        assert isinstance(facets, list)
        facets[0]["witness_sets"] = [[]]  # type: ignore[index]
        with self.assertRaises(ContractError):
            parse_draft(wire)

    def test_unknown_witness_in_a_clause_is_rejected(self) -> None:
        wire = _wire()
        facets = wire["facets"]
        assert isinstance(facets, list)
        facets[0]["witness_sets"] = [["missing-witness"]]  # type: ignore[index]
        draft = parse_draft(wire)
        fixture = build_fixture("basic-edit")
        with self.assertRaises(ContractError):
            compile_judgments(draft, fixture.variant_aliases, fixture.capture)

    def test_unknown_fields_are_rejected(self) -> None:
        wire = _wire()
        wire["extra"] = True
        with self.assertRaises(ContractError):
            parse_draft(wire)

    def test_unsupported_schema_is_rejected(self) -> None:
        wire = _wire()
        wire["schema"] = "agentq.eval.judgment-draft/v2"
        with self.assertRaises(ContractError):
            parse_draft(wire)

    def test_prose_changes_keep_metrics_and_change_identity(self) -> None:
        fixture, judgment = _compiled("basic-edit")
        facets = tuple(
            replace(item, audit_note=item.audit_note + " (revised)")
            for item in judgment.facets
        )
        revised = replace(judgment, facets=facets)
        selected = frozenset(
            variant.variant_id for variant in fixture.capture.decision.pool.variants
        )
        for original, changed in zip(judgment.facets, revised.facets, strict=True):
            self.assertEqual(
                facet_supported(original, judgment, selected),
                facet_supported(changed, revised, selected),
            )
        self.assertNotEqual(judgment_digest(judgment), judgment_digest(revised))

    def test_witness_mapping_changes_identity(self) -> None:
        fixture, judgment = _compiled("basic-edit")
        witnesses = tuple(
            replace(item, acceptable_variant_ids=item.acceptable_variant_ids[:-1])
            if item.acceptable_variant_ids
            else item
            for item in judgment.witnesses
        )
        altered = replace(judgment, witnesses=witnesses)
        self.assertNotEqual(judgment_digest(judgment), judgment_digest(altered))
        del fixture

    def test_judgment_round_trips_through_codec_and_store(self) -> None:
        _, judgment = _compiled("unstable-source")
        self.assertEqual(decode_judgment(encode_judgment(judgment)), judgment)
        with TemporaryDirectory() as temp:
            store = CaptureStore(Path(temp))
            judgment_id = store.write_judgment(judgment)
            self.assertEqual(judgment_id, judgment_digest(judgment))
            self.assertEqual(store.read_judgment(judgment_id), judgment)
            self.assertEqual(store.write_judgment(judgment), judgment_id)


if __name__ == "__main__":
    unittest.main()
