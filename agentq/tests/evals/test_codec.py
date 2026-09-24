"""Strict, lossless capture and decision-configuration codec behavior."""

from __future__ import annotations

import json
import unittest
from dataclasses import replace
from typing import cast

from agentq.core import (
    COMPLETE,
    PARTIAL,
    PROVIDER_ERROR,
    ContractError,
    Diagnostic,
    SourceRef,
    canonical_json,
    typed_coverage,
)
from agentq.inspection.budgeting import AcquisitionLimits, DeliveryBudget
from agentq.inspection.contracts import (
    AcquisitionRecord,
    Binding,
    CandidateTarget,
    Capability,
    CollectionStatus,
    DeclarationPayload,
    EvidencePool,
    EvidenceVariant,
    Fidelity,
    LocationTarget,
    MentionPayload,
    Observation,
    ObservationKind,
    OutlinePayload,
    OutlineSymbolRef,
    PackagePayload,
    PathKind,
    PathTarget,
    RangeTarget,
    ReferencePayload,
    RepresentationKind,
    SourceSpan,
    SourceVersion,
    SourceWindowPayload,
    SymbolTarget,
    make_observation,
    make_variant,
)
from agentq.inspection.decision import DecisionConfig
from agentq.inspection.selection import SelectionProfile
from evals.build_fixtures import build_fixture
from evals.codec import (
    config_digest,
    decode_attempts,
    decode_capture,
    decode_case_suite,
    decode_config,
    decode_json,
    encode_attempts,
    encode_capture,
    encode_case_suite,
    encode_config,
)
from evals.models import AttemptOutcome, CaptureAttempt, ReplayCapture
from tests.evals.support import BASELINE_PROFILE, CASES

SELECTION_CHALLENGER = BASELINE_PROFILE.parent / "selection-challenger.json"

UNICODE_TEXT = "line one\n\tline two  \nemoji 😀 CJK 漢字 trailing  "


def _version(path: str = "orders.py") -> SourceVersion:
    return SourceVersion(path=path, version="fixture-v1")


def _observed(
    kind: ObservationKind,
    payload: object,
    *,
    acquisition_id: str,
    path: str = "orders.py",
    start: int = 1,
    end: int = 1,
) -> Observation:
    return make_observation(
        kind=kind,
        payload=payload,  # type: ignore[arg-type]
        source=SourceRef(path=path, start_line=start, end_line=end),
        source_versions=(_version(path),),
        acquisition_id=acquisition_id,
    )


def _variant(
    observation: Observation,
    representation: RepresentationKind,
    fidelity: Fidelity,
    text: str,
    span: SourceSpan | None = None,
) -> EvidenceVariant:
    return make_variant(
        observation_id=observation.observation_id,
        representation=representation,
        fidelity=fidelity,
        source=observation.source,
        text=text,
        span=span,
    )


def _acquisition(
    acquisition_id: str,
    capability: Capability,
    status: CollectionStatus,
    coverage: object,
) -> AcquisitionRecord:
    return AcquisitionRecord(
        acquisition_id=acquisition_id,
        capability=capability,
        provider="fixture",
        provider_version="fixture-1.0",
        method=capability.value,
        effective_scope=("orders.py",),
        coverage=coverage,  # type: ignore[arg-type]
        status=status,
    )


def _kitchen_sink_pool() -> EvidencePool:
    completed = _acquisition(
        "acq-complete",
        Capability.FIND_DECLARATIONS,
        CollectionStatus.COMPLETED,
        typed_coverage(COMPLETE),
    )
    declaration = _observed(
        ObservationKind.DECLARATION,
        DeclarationPayload(
            name="list_orders",
            kind="function",
            signature="def list_orders()",
            span=SourceSpan(start_line=1, end_line=3),
            declaration_span=SourceSpan(
                start_line=1, end_line=5, start_column=1, end_column=20
            ),
        ),
        acquisition_id=completed.acquisition_id,
        start=1,
        end=3,
    )
    reference = _observed(
        ObservationKind.SEMANTIC_REFERENCE,
        ReferencePayload(
            relationship="reference",
            text="list_orders(orders) — ünïcode",
            binding=Binding.RESOLVED,
            domain="test",
            configuration="tsconfig.json",
        ),
        acquisition_id=completed.acquisition_id,
        path="tests/test_orders.py",
        start=4,
        end=4,
    )
    source_window = _observed(
        ObservationKind.SOURCE_WINDOW,
        SourceWindowPayload(
            text=UNICODE_TEXT,
            span=SourceSpan(start_line=1, end_line=3),
            truncated=True,
        ),
        acquisition_id=completed.acquisition_id,
        start=1,
        end=3,
    )
    outline = _observed(
        ObservationKind.OUTLINE,
        OutlinePayload(
            symbols=(
                OutlineSymbolRef(
                    name="list_orders", kind="function", span=SourceSpan(1, 3)
                ),
            ),
            truncated=True,
        ),
        acquisition_id=completed.acquisition_id,
        start=1,
        end=3,
    )
    package = _observed(
        ObservationKind.OWNING_PACKAGE,
        PackagePayload(
            path="pyproject.toml",
            kind="python",
            name="agentq-orders-fixture",
            scripts=("test",),
        ),
        acquisition_id=completed.acquisition_id,
        path="pyproject.toml",
    )
    mention = _observed(
        ObservationKind.TEST_MENTION,
        MentionPayload(text="expect(list_orders)  ", domain="test"),
        acquisition_id=completed.acquisition_id,
        path="tests/test_orders.py",
        start=7,
        end=7,
    )
    variants = (
        _variant(
            declaration,
            RepresentationKind.SIGNATURE,
            Fidelity.SUMMARY,
            "def list_orders()",
            SourceSpan(1, 3),
        ),
        _variant(
            declaration,
            RepresentationKind.EXACT_SOURCE,
            Fidelity.EXACT,
            "def list_orders():\n    return []",
            SourceSpan(1, 5, 1, 20),
        ),
        _variant(
            reference,
            RepresentationKind.REFERENCE,
            Fidelity.BOUNDED,
            "list_orders(orders) — ünïcode",
            SourceSpan(4, 4),
        ),
        _variant(
            source_window,
            RepresentationKind.EXACT_SOURCE,
            Fidelity.EXACT,
            UNICODE_TEXT,
            SourceSpan(1, 3),
        ),
        _variant(
            outline,
            RepresentationKind.OUTLINE,
            Fidelity.SUMMARY,
            "orders.py: function list_orders",
        ),
        _variant(
            package,
            RepresentationKind.PACKAGE,
            Fidelity.SUMMARY,
            "python package (pyproject.toml)",
        ),
        _variant(
            mention,
            RepresentationKind.REFERENCE,
            Fidelity.BOUNDED,
            "expect(list_orders)  ",
            SourceSpan(7, 7),
        ),
    )
    return EvidencePool(
        request_id="req-kitchen",
        acquisitions=(
            completed,
            _acquisition(
                "acq-empty",
                Capability.LEXICAL_MENTIONS,
                CollectionStatus.EMPTY,
                typed_coverage(COMPLETE),
            ),
            _acquisition(
                "acq-partial",
                Capability.SEMANTIC_REFERENCES,
                CollectionStatus.PARTIAL,
                typed_coverage(PARTIAL, "result_limit"),
            ),
            _acquisition(
                "acq-unavailable",
                Capability.IMPLEMENTATIONS,
                CollectionStatus.UNAVAILABLE,
                typed_coverage(PARTIAL, "provider_unavailable"),
            ),
            _acquisition(
                "acq-failed",
                Capability.OUTLINE,
                CollectionStatus.FAILED,
                typed_coverage(PARTIAL, PROVIDER_ERROR),
            ),
        ),
        observations=(
            declaration,
            reference,
            source_window,
            outline,
            package,
            mention,
        ),
        variants=variants,
        limitations=(
            Diagnostic(
                message="source changed during acquisition",
                code="source_unstable",
                path="orders.py",
                severity="warning",
            ),
        ),
        unstable_observation_ids=(mention.observation_id,),
        coverage=typed_coverage(COMPLETE, domain="repository", scope="path_role:test"),
    )


def _kitchen_sink_capture() -> ReplayCapture:
    base = build_fixture("basic-edit").capture
    decision = replace(base.decision, pool=_kitchen_sink_pool())
    return replace(base, decision=decision)


def _wire(capture: ReplayCapture) -> dict[str, object]:
    value = json.loads(encode_capture(capture).decode("utf-8"))
    assert isinstance(value, dict)
    return value


def _bytes(value: object) -> bytes:
    return canonical_json(value).encode("utf-8")


def _observation_wire(wire: dict[str, object], payload_kind: str) -> dict[str, object]:
    decision = wire["decision"]
    assert isinstance(decision, dict)
    pool = decision["pool"]
    assert isinstance(pool, dict)
    for observation in pool["observations"]:  # type: ignore[union-attr]
        assert isinstance(observation, dict)
        payload = observation["payload"]
        assert isinstance(payload, dict)
        if payload["payload_kind"] == payload_kind:
            return observation
    raise AssertionError(f"no observation with payload kind {payload_kind!r}")


class RoundTripTests(unittest.TestCase):
    def test_every_fixture_capture_round_trips_losslessly(self) -> None:
        for case_id in (
            "basic-edit",
            "lexical-decoy",
            "same-file-quota",
            "variant-fallback",
            "required-upgrade",
            "empty-test-search",
            "unstable-source",
            "delivery-overhead",
        ):
            with self.subTest(case_id=case_id):
                capture = build_fixture(case_id).capture
                self.assertEqual(decode_capture(encode_capture(capture)), capture)

    def test_every_payload_and_status_round_trips(self) -> None:
        capture = _kitchen_sink_capture()
        decoded = decode_capture(encode_capture(capture))
        self.assertEqual(decoded, capture)
        pool = decoded.decision.pool
        self.assertEqual(
            {record.status for record in pool.acquisitions},
            {
                CollectionStatus.COMPLETED,
                CollectionStatus.EMPTY,
                CollectionStatus.PARTIAL,
                CollectionStatus.UNAVAILABLE,
                CollectionStatus.FAILED,
            },
        )
        self.assertEqual(
            {type(item.payload).__name__ for item in pool.observations},
            {
                "DeclarationPayload",
                "ReferencePayload",
                "SourceWindowPayload",
                "OutlinePayload",
                "PackagePayload",
                "MentionPayload",
            },
        )

    def test_unicode_and_whitespace_survive_byte_for_byte(self) -> None:
        capture = _kitchen_sink_capture()
        decoded = decode_capture(encode_capture(capture))
        texts = {variant.text for variant in decoded.decision.pool.variants}
        self.assertIn(UNICODE_TEXT, texts)
        self.assertIn("list_orders(orders) — ünïcode", texts)
        self.assertIn("expect(list_orders)  ", texts)

    def test_every_target_kind_round_trips(self) -> None:
        base = build_fixture("basic-edit").capture
        targets = (
            SymbolTarget(name="list_orders", scopes=("src",)),
            PathTarget(path="orders.py", path_kind=PathKind.FILE),
            LocationTarget(path="orders.py", line=10, column=5),
            RangeTarget(path="orders.py", ranges=(SourceSpan(1, 3), SourceSpan(5, 5))),
            CandidateTarget(
                candidate_id="cand-1", symbol="list_orders", scopes=("src",)
            ),
        )
        for target in targets:
            with self.subTest(target=type(target).__name__):
                decision = replace(
                    base.decision,
                    request=replace(base.decision.request, target=target),
                    resolution=replace(base.decision.resolution, target=target),
                )
                capture = replace(base, decision=decision)
                self.assertEqual(decode_capture(encode_capture(capture)), capture)

    def test_capture_digest_is_stable_across_re_encoding(self) -> None:
        capture = _kitchen_sink_capture()
        data = encode_capture(capture)
        self.assertEqual(data, encode_capture(decode_capture(data)))

    def test_the_source_line_width_limit_round_trips(self) -> None:
        base = build_fixture("basic-edit").capture
        capture = replace(
            base, limits=AcquisitionLimits(max_source_line_chars=400)
        )
        self.assertEqual(decode_capture(encode_capture(capture)), capture)

    def test_a_capture_predating_the_line_width_limit_decodes_to_the_default(
        self,
    ) -> None:
        base = build_fixture("basic-edit").capture
        wire = _wire(base)
        limits = cast("dict[str, object]", wire["limits"])
        del limits["max_source_line_chars"]
        decoded = decode_capture(_bytes(wire))
        self.assertEqual(
            decoded.limits.max_source_line_chars,
            AcquisitionLimits().max_source_line_chars,
        )
        self.assertEqual(
            decoded.limits,
            replace(
                base.limits,
                max_source_line_chars=AcquisitionLimits().max_source_line_chars,
            ),
        )


class StrictDecodeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.capture = _kitchen_sink_capture()

    def test_unsupported_schema_is_rejected(self) -> None:
        wire = _wire(self.capture)
        wire["schema"] = "agentq.eval.capture/v2"
        with self.assertRaises(ContractError):
            decode_capture(_bytes(wire))

    def test_unknown_field_is_rejected(self) -> None:
        wire = _wire(self.capture)
        wire["extra"] = 1
        with self.assertRaises(ContractError):
            decode_capture(_bytes(wire))

    def test_duplicate_json_keys_are_rejected(self) -> None:
        with self.assertRaises(ContractError):
            decode_json('{"case_id": "a", "case_id": "b"}', what="test")

    def test_non_finite_numbers_are_rejected(self) -> None:
        with self.assertRaises(ContractError):
            decode_json('{"value": NaN}', what="test")

    def test_dangling_variant_reference_is_rejected(self) -> None:
        wire = _wire(self.capture)
        decision = wire["decision"]
        assert isinstance(decision, dict)
        pool = decision["pool"]
        assert isinstance(pool, dict)
        pool["variants"][0]["observation_id"] = "obs-missing"  # type: ignore[index]
        with self.assertRaises(ContractError):
            decode_capture(_bytes(wire))

    def test_duplicate_observation_ids_are_rejected(self) -> None:
        wire = _wire(self.capture)
        decision = wire["decision"]
        assert isinstance(decision, dict)
        pool = decision["pool"]
        assert isinstance(pool, dict)
        observations = pool["observations"]
        assert isinstance(observations, list)
        observations.append(observations[0])
        with self.assertRaises(ContractError):
            decode_capture(_bytes(wire))

    def test_duplicate_acquisition_ids_are_rejected(self) -> None:
        wire = _wire(self.capture)
        decision = wire["decision"]
        assert isinstance(decision, dict)
        pool = decision["pool"]
        assert isinstance(pool, dict)
        acquisitions = pool["acquisitions"]
        assert isinstance(acquisitions, list)
        acquisitions.append(acquisitions[0])
        with self.assertRaises(ContractError):
            decode_capture(_bytes(wire))

    def test_invalid_span_is_rejected(self) -> None:
        wire = _wire(self.capture)
        observation = _observation_wire(wire, "source_window")
        payload = observation["payload"]
        assert isinstance(payload, dict)
        span = payload["span"]
        assert isinstance(span, dict)
        span["end_line"] = 0
        with self.assertRaises(ContractError):
            decode_capture(_bytes(wire))

    def test_unknown_unstable_observation_is_rejected(self) -> None:
        wire = _wire(self.capture)
        decision = wire["decision"]
        assert isinstance(decision, dict)
        pool = decision["pool"]
        assert isinstance(pool, dict)
        pool["unstable_observation_ids"] = ["obs-missing"]
        with self.assertRaises(ContractError):
            decode_capture(_bytes(wire))


class ConfigTests(unittest.TestCase):
    def test_default_config_round_trips(self) -> None:
        config = DecisionConfig()
        self.assertEqual(decode_config(encode_config(config)), config)

    def test_baseline_profile_decodes_to_the_runtime_default(self) -> None:
        self.assertEqual(decode_config(BASELINE_PROFILE.read_bytes()), DecisionConfig())

    def test_config_digest_changes_with_content_under_one_name(self) -> None:
        config = DecisionConfig()
        altered = replace(
            config,
            scoring=replace(
                config.scoring, binding_bonus=config.scoring.binding_bonus + 1
            ),
        )
        self.assertNotEqual(config_digest(config), config_digest(altered))

    def test_a_profile_missing_an_intent_is_rejected(self) -> None:
        wire = json.loads(encode_config(DecisionConfig()).decode("utf-8"))
        assert isinstance(wire, dict)
        scoring = wire["scoring"]
        assert isinstance(scoring, dict)
        priorities = scoring["intent_priorities"]
        assert isinstance(priorities, dict)
        priorities.pop("rename")
        with self.assertRaises(ContractError):
            decode_config(_bytes(wire))

    def test_unsupported_config_schema_is_rejected(self) -> None:
        wire = json.loads(encode_config(DecisionConfig()).decode("utf-8"))
        assert isinstance(wire, dict)
        wire["schema"] = "agentq.eval.decision-config/v2"
        with self.assertRaises(ContractError):
            decode_config(_bytes(wire))

    def test_delivery_budget_round_trips(self) -> None:
        config = replace(
            DecisionConfig(),
            delivery=DeliveryBudget(max_chars=6000, envelope_chars=200),
        )
        self.assertEqual(decode_config(encode_config(config)), config)

    def test_selection_challenger_flags_round_trip(self) -> None:
        config = replace(
            DecisionConfig(),
            selection=SelectionProfile(
                profile="selection-v2", variant_fallback=True, skip_zero_value=True
            ),
        )
        self.assertEqual(decode_config(encode_config(config)), config)

    def test_older_selection_profiles_decode_to_the_baseline_flags(self) -> None:
        wire = json.loads(encode_config(DecisionConfig()).decode("utf-8"))
        assert isinstance(wire, dict)
        selection = wire["selection"]
        assert isinstance(selection, dict)
        selection.pop("variant_fallback")
        selection.pop("skip_zero_value")
        decoded = decode_config(_bytes(wire))
        self.assertEqual(decoded.selection, DecisionConfig().selection)

    def test_selection_challenger_profile_decodes_to_its_flags(self) -> None:
        challenger = decode_config(SELECTION_CHALLENGER.read_bytes())
        self.assertTrue(challenger.selection.variant_fallback)
        self.assertTrue(challenger.selection.skip_zero_value)


class CaseAndAttemptCodecTests(unittest.TestCase):
    def test_case_suite_round_trips(self) -> None:
        suite = decode_case_suite(CASES.read_bytes())
        self.assertEqual(suite.suite_id, "orders-python-v1")
        self.assertEqual(len(suite.cases), 4)
        self.assertEqual(decode_case_suite(encode_case_suite(suite)), suite)

    def test_single_case_discriminator_is_accepted(self) -> None:
        raw = json.loads(CASES.read_text(encoding="utf-8"))
        single = raw["cases"][0]
        suite = decode_case_suite(canonical_json(single).encode("utf-8"))
        self.assertEqual(suite.suite_id, single["case_id"])
        self.assertEqual(len(suite.cases), 1)

    def test_unknown_case_suite_schema_is_rejected(self) -> None:
        raw = json.loads(CASES.read_text(encoding="utf-8"))
        raw["schema"] = "agentq.eval.case-suite/v2"
        with self.assertRaises(ContractError):
            decode_case_suite(canonical_json(raw).encode("utf-8"))

    def test_capture_attempts_round_trip(self) -> None:
        attempts = (
            CaptureAttempt(
                case_id="case",
                outcome=AttemptOutcome.AMBIGUOUS,
                reason="ambiguous_target",
                detail="2 declaration candidates retained",
                candidates=("orders.py:10-15", "legacy.py:1-3"),
                checkout="/tmp/checkout",
                started_at="2026-09-23T00:00:00+00:00",
                duration_ms=12.5,
            ),
        )
        self.assertEqual(decode_attempts(encode_attempts(attempts)), attempts)

    def test_captured_attempt_requires_a_capture_id(self) -> None:
        with self.assertRaises(ContractError):
            CaptureAttempt(
                case_id="case", outcome=AttemptOutcome.CAPTURED
            )


if __name__ == "__main__":
    unittest.main()
