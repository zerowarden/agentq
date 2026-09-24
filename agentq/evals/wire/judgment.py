"""Wire codecs for judgments, locks, cases, and capture attempts.

Expected outcomes, facets, witnesses, compiled judgments, suite locks,
authored case suites, and capture attempts.
"""

from __future__ import annotations

import hashlib

from agentq.core import ContractError, canonical_json

from ..models import (
    ATTEMPTS_SCHEMA,
    CASE_SCHEMA,
    CASE_SUITE_SCHEMA,
    JUDGMENT_SCHEMA,
    LOCK_SCHEMA,
    AttemptOutcome,
    CaptureAttempt,
    CaseSource,
    CaseSpec,
    CaseSuite,
    ExpectedOutcome,
    ExpectedOutcomeKind,
    JudgmentFacet,
    JudgmentSet,
    JudgmentWitness,
    LockedCase,
    SuiteLock,
)
from .config import _delivery_from_wire, _delivery_wire
from .contracts import _request_from_wire, _request_wire
from .json import (
    as_list,
    as_mapping,
    decode_json,
    exact_keys,
    optional_int,
    optional_str,
    read_bool,
    read_enum,
    read_number,
    read_str,
    read_strings,
    reject_duplicates,
)


def _lock_wire(lock: SuiteLock) -> dict[str, object]:
    return {
        "schema": lock.schema,
        "suite_id": lock.suite_id,
        "cases": [
            {
                "case_id": case.case_id,
                "capture_id": case.capture_id,
                "judgment_id": case.judgment_id,
                "delivery": (
                    None
                    if case.delivery is None
                    else _delivery_wire(case.delivery)
                ),
            }
            for case in lock.cases
        ],
    }


def _expected_wire(outcome: ExpectedOutcome) -> dict[str, object]:
    return {
        "kind": outcome.kind.value,
        "requirement_id": outcome.requirement_id,
        "status": outcome.status,
        "code": outcome.code,
        "minimum": outcome.minimum,
        "audit_note": outcome.audit_note,
    }


def _expected_from_wire(value: object, what: str) -> ExpectedOutcome:
    mapping = as_mapping(value, what)
    exact_keys(
        mapping,
        {"kind", "requirement_id", "status", "code", "minimum", "audit_note"},
        what,
    )
    return ExpectedOutcome(
        kind=read_enum(ExpectedOutcomeKind, mapping["kind"], f"{what}.kind"),
        requirement_id=optional_str(
            mapping["requirement_id"], f"{what}.requirement_id"
        ),
        status=optional_str(mapping["status"], f"{what}.status"),
        code=optional_str(mapping["code"], f"{what}.code"),
        minimum=optional_int(mapping["minimum"], f"{what}.minimum", minimum=1),
        audit_note=read_str(
            mapping["audit_note"], f"{what}.audit_note", allow_empty=True
        ),
    )


def _witness_wire(witness: JudgmentWitness) -> dict[str, object]:
    return {
        "witness_id": witness.witness_id,
        "acceptable_variant_ids": list(witness.acceptable_variant_ids),
    }


def _witness_from_wire(value: object, what: str) -> JudgmentWitness:
    mapping = as_mapping(value, what)
    exact_keys(mapping, {"witness_id", "acceptable_variant_ids"}, what)
    return JudgmentWitness(
        witness_id=read_str(mapping["witness_id"], f"{what}.witness_id"),
        acceptable_variant_ids=read_strings(
            mapping["acceptable_variant_ids"], f"{what}.acceptable_variant_ids"
        ),
    )


def _facet_wire(facet: JudgmentFacet) -> dict[str, object]:
    return {
        "facet_id": facet.facet_id,
        "critical": facet.critical,
        "audit_note": facet.audit_note,
        "witness_sets": [list(clause) for clause in facet.witness_sets],
    }


def _facet_from_wire(value: object, what: str) -> JudgmentFacet:
    mapping = as_mapping(value, what)
    exact_keys(mapping, {"facet_id", "critical", "audit_note", "witness_sets"}, what)
    clauses = tuple(
        read_strings(clause, f"{what}.witness_sets[{index}]")
        for index, clause in enumerate(as_list(mapping["witness_sets"], what))
    )
    return JudgmentFacet(
        facet_id=read_str(mapping["facet_id"], f"{what}.facet_id"),
        critical=read_bool(mapping["critical"], f"{what}.critical"),
        audit_note=read_str(
            mapping["audit_note"], f"{what}.audit_note", allow_empty=True
        ),
        witness_sets=clauses,
    )


def _judgment_wire(judgment: JudgmentSet) -> dict[str, object]:
    return {
        "schema": judgment.schema,
        "case_id": judgment.case_id,
        "capture_id": judgment.capture_id,
        "basis": judgment.basis,
        "review_status": judgment.review_status,
        "facets": [_facet_wire(item) for item in judgment.facets],
        "witnesses": [_witness_wire(item) for item in judgment.witnesses],
        "irrelevant_variant_ids": list(judgment.irrelevant_variant_ids),
        "expected_outcomes": [
            _expected_wire(item) for item in judgment.expected_outcomes
        ],
    }


def _judgment_from_wire(value: object, what: str) -> JudgmentSet:
    mapping = as_mapping(value, what)
    exact_keys(
        mapping,
        {
            "schema",
            "case_id",
            "capture_id",
            "basis",
            "review_status",
            "facets",
            "witnesses",
            "irrelevant_variant_ids",
            "expected_outcomes",
        },
        what,
    )
    schema = read_str(mapping["schema"], f"{what}.schema")
    if schema != JUDGMENT_SCHEMA:
        raise ContractError(f"unsupported judgment schema: {schema!r}")
    return JudgmentSet(
        case_id=read_str(mapping["case_id"], f"{what}.case_id"),
        capture_id=read_str(mapping["capture_id"], f"{what}.capture_id"),
        basis=read_str(mapping["basis"], f"{what}.basis"),
        review_status=read_str(mapping["review_status"], f"{what}.review_status"),
        facets=tuple(
            _facet_from_wire(item, f"{what}.facets[{index}]")
            for index, item in enumerate(as_list(mapping["facets"], what))
        ),
        witnesses=tuple(
            _witness_from_wire(item, f"{what}.witnesses[{index}]")
            for index, item in enumerate(as_list(mapping["witnesses"], what))
        ),
        irrelevant_variant_ids=read_strings(
            mapping["irrelevant_variant_ids"], f"{what}.irrelevant_variant_ids"
        ),
        expected_outcomes=tuple(
            _expected_from_wire(item, f"{what}.expected_outcomes[{index}]")
            for index, item in enumerate(
                as_list(mapping["expected_outcomes"], what)
            )
        ),
        schema=schema,
    )


def encode_judgment(judgment: JudgmentSet) -> bytes:
    return canonical_json(_judgment_wire(judgment)).encode("utf-8")


def decode_judgment(data: bytes) -> JudgmentSet:
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ContractError(f"judgment is not valid UTF-8: {exc}") from exc
    return _judgment_from_wire(decode_json(text, what="judgment"), "judgment")


def judgment_digest(judgment: JudgmentSet) -> str:
    return hashlib.sha256(encode_judgment(judgment)).hexdigest()


def encode_lock(lock: SuiteLock) -> bytes:
    return canonical_json(_lock_wire(lock)).encode("utf-8")


def _case_source_wire(source: CaseSource) -> dict[str, object]:
    return {
        "kind": source.kind,
        "root": source.root,
        "fixture_revision": source.fixture_revision,
        "repo": source.repo,
        "commit": source.commit,
        "repo_url": source.repo_url,
    }


def _case_source_from_wire(value: object, what: str) -> CaseSource:
    mapping = as_mapping(value, what)
    exact_keys(
        mapping,
        {"kind", "root", "fixture_revision", "repo", "commit", "repo_url"},
        what,
    )
    return CaseSource(
        kind=read_str(mapping["kind"], f"{what}.kind"),
        root=read_str(mapping["root"], f"{what}.root"),
        fixture_revision=read_str(
            mapping.get("fixture_revision", ""),
            f"{what}.fixture_revision",
            allow_empty=True,
        ),
        repo=read_str(mapping.get("repo", ""), f"{what}.repo", allow_empty=True),
        commit=read_str(
            mapping.get("commit", ""), f"{what}.commit", allow_empty=True
        ),
        repo_url=read_str(
            mapping.get("repo_url", ""), f"{what}.repo_url", allow_empty=True
        ),
    )


def _case_spec_wire(spec: CaseSpec) -> dict[str, object]:
    return {
        "schema": spec.schema,
        "case_id": spec.case_id,
        "source": _case_source_wire(spec.source),
        "request": _request_wire(spec.request),
        "target_origin": spec.target_origin,
        "judgment_basis": spec.judgment_basis,
        "split_group": spec.split_group,
        "capture_id": spec.capture_id,
        "judgment_id": spec.judgment_id,
    }


def _case_spec_from_wire(value: object, what: str) -> CaseSpec:
    mapping = as_mapping(value, what)
    exact_keys(
        mapping,
        {
            "schema",
            "case_id",
            "source",
            "request",
            "target_origin",
            "judgment_basis",
            "split_group",
            "capture_id",
            "judgment_id",
        },
        what,
    )
    schema = read_str(mapping["schema"], f"{what}.schema")
    if schema != CASE_SCHEMA:
        raise ContractError(f"unsupported case schema: {schema!r}")
    return CaseSpec(
        case_id=read_str(mapping["case_id"], f"{what}.case_id"),
        source=_case_source_from_wire(mapping["source"], f"{what}.source"),
        request=_request_from_wire(mapping["request"], f"{what}.request"),
        target_origin=read_str(mapping["target_origin"], f"{what}.target_origin"),
        judgment_basis=read_str(mapping["judgment_basis"], f"{what}.judgment_basis"),
        split_group=read_str(
            mapping["split_group"], f"{what}.split_group", allow_empty=True
        ),
        capture_id=optional_str(mapping["capture_id"], f"{what}.capture_id"),
        judgment_id=optional_str(mapping["judgment_id"], f"{what}.judgment_id"),
        schema=schema,
    )


def encode_case_spec(spec: CaseSpec) -> bytes:
    return canonical_json(_case_spec_wire(spec)).encode("utf-8")


def encode_case_suite(suite: CaseSuite) -> bytes:
    return canonical_json(
        {
            "schema": suite.schema,
            "suite_id": suite.suite_id,
            "cases": [_case_spec_wire(item) for item in suite.cases],
        }
    ).encode("utf-8")


def decode_case_suite(data: bytes) -> CaseSuite:
    """Decode a case suite or a single case, validating the declared shape."""
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ContractError(f"case suite is not valid UTF-8: {exc}") from exc
    mapping = as_mapping(decode_json(text, what="case suite"), "case suite")
    schema = read_str(mapping.get("schema"), "case suite.schema")
    if schema == CASE_SCHEMA:
        spec = _case_spec_from_wire(mapping, "case")
        return CaseSuite(suite_id=spec.case_id, cases=(spec,))
    if schema != CASE_SUITE_SCHEMA:
        raise ContractError(f"unsupported case suite schema: {schema!r}")
    exact_keys(mapping, {"schema", "suite_id", "cases"}, "case suite")
    cases = tuple(
        _case_spec_from_wire(item, f"case suite.cases[{index}]")
        for index, item in enumerate(as_list(mapping["cases"], "case suite.cases"))
    )
    return CaseSuite(
        suite_id=read_str(mapping["suite_id"], "case suite.suite_id"),
        cases=cases,
        schema=schema,
    )


def _attempt_wire(attempt: CaptureAttempt) -> dict[str, object]:
    return {
        "case_id": attempt.case_id,
        "outcome": attempt.outcome.value,
        "reason": attempt.reason,
        "detail": attempt.detail,
        "candidates": list(attempt.candidates),
        "capture_id": attempt.capture_id,
        "checkout": attempt.checkout,
        "started_at": attempt.started_at,
        "duration_ms": attempt.duration_ms,
    }


def _attempt_from_wire(value: object, what: str) -> CaptureAttempt:
    mapping = as_mapping(value, what)
    exact_keys(
        mapping,
        {
            "case_id",
            "outcome",
            "reason",
            "detail",
            "candidates",
            "capture_id",
            "checkout",
            "started_at",
            "duration_ms",
        },
        what,
    )
    return CaptureAttempt(
        case_id=read_str(mapping["case_id"], f"{what}.case_id"),
        outcome=read_enum(AttemptOutcome, mapping["outcome"], f"{what}.outcome"),
        reason=read_str(mapping["reason"], f"{what}.reason", allow_empty=True),
        detail=read_str(mapping["detail"], f"{what}.detail", allow_empty=True),
        candidates=read_strings(mapping["candidates"], f"{what}.candidates"),
        capture_id=optional_str(mapping["capture_id"], f"{what}.capture_id"),
        checkout=read_str(mapping["checkout"], f"{what}.checkout", allow_empty=True),
        started_at=read_str(
            mapping["started_at"], f"{what}.started_at", allow_empty=True
        ),
        duration_ms=read_number(mapping["duration_ms"], f"{what}.duration_ms"),
    )


def encode_attempts(attempts: tuple[CaptureAttempt, ...]) -> bytes:
    return canonical_json(
        {
            "schema": ATTEMPTS_SCHEMA,
            "attempts": [_attempt_wire(item) for item in attempts],
        }
    ).encode("utf-8")


def decode_attempts(data: bytes) -> tuple[CaptureAttempt, ...]:
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ContractError(f"capture attempts are not valid UTF-8: {exc}") from exc
    mapping = as_mapping(decode_json(text, what="capture attempts"), "capture attempts")
    exact_keys(mapping, {"schema", "attempts"}, "capture attempts")
    schema = read_str(mapping["schema"], "capture attempts.schema")
    if schema != ATTEMPTS_SCHEMA:
        raise ContractError(f"unsupported capture attempts schema: {schema!r}")
    return tuple(
        _attempt_from_wire(item, f"capture attempts[{index}]")
        for index, item in enumerate(
            as_list(mapping["attempts"], "capture attempts")
        )
    )


def decode_lock(data: bytes) -> SuiteLock:
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ContractError(f"suite lock is not valid UTF-8: {exc}") from exc
    mapping = as_mapping(decode_json(text, what="suite lock"), "suite lock")
    exact_keys(mapping, {"schema", "suite_id", "cases"}, "suite lock")
    schema = read_str(mapping["schema"], "suite lock.schema")
    if schema != LOCK_SCHEMA:
        raise ContractError(f"unsupported suite lock schema: {schema!r}")
    cases: list[LockedCase] = []
    for index, item in enumerate(as_list(mapping["cases"], "suite lock.cases")):
        case = as_mapping(item, f"suite lock.cases[{index}]")
        exact_keys(
            case,
            {"case_id", "capture_id", "judgment_id", "delivery"},
            f"suite lock.cases[{index}]",
        )
        # A lock written before delivery pins existed falls back to the profile.
        delivery = case.get("delivery")
        cases.append(
            LockedCase(
                case_id=read_str(
                    case["case_id"], f"suite lock.cases[{index}].case_id"
                ),
                capture_id=read_str(
                    case["capture_id"], f"suite lock.cases[{index}].capture_id"
                ),
                judgment_id=optional_str(
                    case["judgment_id"],
                    f"suite lock.cases[{index}].judgment_id",
                ),
                delivery=(
                    None
                    if delivery is None
                    else _delivery_from_wire(
                        delivery, f"suite lock.cases[{index}].delivery"
                    )
                ),
            )
        )
    reject_duplicates(
        (case.case_id for case in cases), "suite lock.cases case ids"
    )
    return SuiteLock(
        suite_id=read_str(mapping["suite_id"], "suite lock.suite_id"),
        cases=tuple(cases),
        schema=schema,
    )
