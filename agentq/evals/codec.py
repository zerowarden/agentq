"""Strict, lossless codecs for captures, locks, and decision configuration.

The capture encoding is the full typed post-acquisition state: every
observation payload, every representation's text, acquisition statuses, scope,
versions, limitations, and unstable ids. It is canonical uncompressed UTF-8
JSON; its SHA-256 digest is the capture identity.

Decoding is deliberately strict. It rejects duplicate JSON keys, non-finite
numbers, unknown schemas and fields, invalid spans, duplicate ids, and dangling
references, and it never recomputes or repairs captured identity.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable, Mapping
from enum import Enum
from typing import TypeVar, cast

from agentq.core import ContractError, Coverage, Diagnostic, SourceRef, canonical_json
from agentq.inspection.budgeting import AcquisitionLimits, DeliveryBudget
from agentq.inspection.contracts import (
    AcquiredEvidence,
    AcquisitionRecord,
    AvailabilityStatus,
    Binding,
    CandidateTarget,
    Capability,
    CapabilityEntry,
    CapabilityReport,
    CollectionPlan,
    CollectionRequest,
    CollectionStatus,
    DecisionInput,
    DeclarationCandidate,
    DeclarationPayload,
    EvidencePool,
    EvidencePolicy,
    EvidenceRequirement,
    EvidenceRole,
    EvidenceVariant,
    Fidelity,
    InspectionRequest,
    InspectionTarget,
    Intent,
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
    RequirementOmission,
    RequirementRule,
    RequirementStrength,
    ResolvedTarget,
    SelectionMethod,
    SourceSpan,
    SourceVersion,
    SourceWindowPayload,
    SymbolTarget,
    TargetKind,
)
from agentq.inspection.contracts.evidence import ObservationPayload
from agentq.inspection.decision import DecisionConfig
from agentq.inspection.scoring import ScoringProfile
from agentq.inspection.selection import SelectionProfile

from .models import (
    CAPTURE_SCHEMA,
    CONFIG_SCHEMA,
    LOCK_SCHEMA,
    FixtureSnapshot,
    LockedCase,
    ProducerFingerprint,
    ReplayCapture,
    RepositorySnapshot,
    Snapshot,
    SuiteLock,
)

_T = TypeVar("_T", bound=Enum)

PAYLOAD_KINDS = {
    DeclarationPayload: "declaration",
    ReferencePayload: "reference",
    SourceWindowPayload: "source_window",
    OutlinePayload: "outline",
    PackagePayload: "package",
    MentionPayload: "mention",
}
PAYLOAD_TYPES: dict[str, type] = {
    kind: payload_type for payload_type, kind in PAYLOAD_KINDS.items()
}


# ---------------------------------------------------------------------------
# Strict JSON
# ---------------------------------------------------------------------------


def _no_duplicate_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ContractError(f"duplicate JSON key: {key!r}")
        result[key] = value
    return result


def _reject_constant(name: str) -> object:
    raise ContractError(f"non-finite JSON number: {name}")


def decode_json(text: str, *, what: str) -> object:
    """Parse one strict JSON document: no duplicate keys, no non-finite values."""
    try:
        return json.loads(
            text, object_pairs_hook=_no_duplicate_keys, parse_constant=_reject_constant
        )
    except ContractError:
        raise
    except ValueError as exc:
        raise ContractError(f"{what} is not valid JSON: {exc}") from exc


def _mapping(value: object, what: str) -> dict[str, object]:
    if not isinstance(value, dict):
        raise ContractError(f"{what} must be a JSON object")
    return cast("dict[str, object]", value)


def _list(value: object, what: str) -> list[object]:
    if not isinstance(value, list):
        raise ContractError(f"{what} must be a JSON array")
    return cast("list[object]", value)


def _exact(mapping: Mapping[str, object], allowed: set[str], what: str) -> None:
    extra = sorted(set(mapping) - allowed)
    if extra:
        raise ContractError(f"{what} has unknown fields: {', '.join(extra)}")


def _str(value: object, what: str, *, allow_empty: bool = False) -> str:
    if not isinstance(value, str) or (not value and not allow_empty):
        raise ContractError(f"{what} must be a string")
    return value


def _optional_str(value: object, what: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ContractError(f"{what} must be a string or null")
    return value


def _int(value: object, what: str, *, minimum: int | None = None) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ContractError(f"{what} must be an integer")
    if minimum is not None and value < minimum:
        raise ContractError(f"{what} must be >= {minimum}")
    return value


def _optional_int(value: object, what: str, *, minimum: int | None = None) -> int | None:
    if value is None:
        return None
    return _int(value, what, minimum=minimum)


def _number(value: object, what: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ContractError(f"{what} must be a number")
    return float(value)


def _bool(value: object, what: str) -> bool:
    if not isinstance(value, bool):
        raise ContractError(f"{what} must be a boolean")
    return value


def _enum(enum_type: type[_T], value: object, what: str) -> _T:
    if not isinstance(value, str):
        raise ContractError(f"{what} must be a string")
    try:
        return enum_type(value)
    except ValueError as exc:
        raise ContractError(f"unsupported {what}: {value!r}") from exc


def _strings(value: object, what: str) -> tuple[str, ...]:
    items = _list(value, what)
    return tuple(
        _str(item, f"{what}[{index}]", allow_empty=True)
        for index, item in enumerate(items)
    )


# ---------------------------------------------------------------------------
# Shared contract records
# ---------------------------------------------------------------------------


def _span_wire(span: SourceSpan) -> dict[str, object]:
    return span.to_wire()


def _span_from_wire(value: object, what: str) -> SourceSpan:
    mapping = _mapping(value, what)
    _exact(
        mapping,
        {"start_line", "start_column", "end_line", "end_column"},
        what,
    )
    return SourceSpan(
        start_line=_int(mapping["start_line"], f"{what}.start_line", minimum=1),
        start_column=_optional_int(
            mapping["start_column"], f"{what}.start_column", minimum=1
        ),
        end_line=_int(mapping["end_line"], f"{what}.end_line", minimum=1),
        end_column=_optional_int(
            mapping["end_column"], f"{what}.end_column", minimum=1
        ),
    )


def _source_wire(source: SourceRef) -> dict[str, object]:
    return source.to_wire()


def _source_from_wire(value: object, what: str) -> SourceRef:
    mapping = _mapping(value, what)
    _exact(mapping, {"path", "start_line", "end_line", "symbol"}, what)
    return SourceRef(
        path=_optional_str(mapping["path"], f"{what}.path"),
        start_line=_optional_int(
            mapping["start_line"], f"{what}.start_line", minimum=1
        ),
        end_line=_optional_int(mapping["end_line"], f"{what}.end_line", minimum=1),
        symbol=_optional_str(mapping["symbol"], f"{what}.symbol"),
    )


def _version_wire(version: SourceVersion) -> dict[str, object]:
    return version.to_wire()


def _version_from_wire(value: object, what: str) -> SourceVersion:
    mapping = _mapping(value, what)
    _exact(mapping, {"path", "version", "method"}, what)
    return SourceVersion(
        path=_str(mapping["path"], f"{what}.path", allow_empty=True),
        version=_str(mapping["version"], f"{what}.version", allow_empty=True),
        method=_str(mapping["method"], f"{what}.method", allow_empty=True),
    )


def _coverage_wire(coverage: Coverage) -> dict[str, object]:
    return {
        "status": coverage.status,
        "reasons": list(coverage.reasons),
        "domain": coverage.domain,
        "scope": coverage.scope,
        "count_quality": coverage.count_quality,
        "scanned": coverage.scanned,
        "matched": coverage.matched,
        "retained": coverage.retained,
        "omitted": coverage.omitted,
    }


def _coverage_from_wire(value: object, what: str) -> Coverage:
    mapping = _mapping(value, what)
    _exact(
        mapping,
        {
            "status",
            "reasons",
            "domain",
            "scope",
            "count_quality",
            "scanned",
            "matched",
            "retained",
            "omitted",
        },
        what,
    )
    return Coverage(
        status=_str(mapping["status"], f"{what}.status"),
        reasons=_strings(mapping["reasons"], f"{what}.reasons"),
        domain=_optional_str(mapping["domain"], f"{what}.domain"),
        scope=_optional_str(mapping["scope"], f"{what}.scope"),
        count_quality=_str(mapping["count_quality"], f"{what}.count_quality"),
        scanned=_optional_int(mapping["scanned"], f"{what}.scanned", minimum=0),
        matched=_optional_int(mapping["matched"], f"{what}.matched", minimum=0),
        retained=_optional_int(mapping["retained"], f"{what}.retained", minimum=0),
        omitted=_optional_int(mapping["omitted"], f"{what}.omitted", minimum=0),
    )


def _diagnostic_wire(diagnostic: Diagnostic) -> dict[str, object]:
    return diagnostic.to_wire()


def _diagnostic_from_wire(value: object, what: str) -> Diagnostic:
    mapping = _mapping(value, what)
    _exact(mapping, {"message", "code", "path", "severity"}, what)
    return Diagnostic(
        message=_str(mapping["message"], f"{what}.message", allow_empty=True),
        code=_str(mapping["code"], f"{what}.code"),
        path=_optional_str(mapping["path"], f"{what}.path"),
        severity=_str(mapping["severity"], f"{what}.severity"),
    )


def _diagnostics_wire(items: tuple[Diagnostic, ...]) -> list[object]:
    return [_diagnostic_wire(item) for item in items]


def _diagnostics_from_wire(value: object, what: str) -> tuple[Diagnostic, ...]:
    return tuple(
        _diagnostic_from_wire(item, f"{what}[{index}]")
        for index, item in enumerate(_list(value, what))
    )


# ---------------------------------------------------------------------------
# Targets, candidates, requests
# ---------------------------------------------------------------------------


def _target_wire(target: object) -> dict[str, object]:
    if isinstance(target, SymbolTarget):
        return target.to_wire()
    if isinstance(target, PathTarget):
        return target.to_wire()
    if isinstance(target, LocationTarget):
        return target.to_wire()
    if isinstance(target, RangeTarget):
        return target.to_wire()
    if isinstance(target, CandidateTarget):
        return target.to_wire()
    raise ContractError(f"unsupported target type: {type(target).__name__}")


def _target_from_wire(value: object, what: str) -> InspectionTarget:
    mapping = _mapping(value, what)
    kind = _enum(TargetKind, mapping.get("kind"), f"{what}.kind")
    match kind:
        case TargetKind.SYMBOL:
            _exact(mapping, {"kind", "name", "scopes"}, what)
            return SymbolTarget(
                name=_str(mapping["name"], f"{what}.name"),
                scopes=_strings(mapping["scopes"], f"{what}.scopes"),
            )
        case TargetKind.PATH:
            _exact(mapping, {"kind", "path", "path_kind"}, what)
            return PathTarget(
                path=_str(mapping["path"], f"{what}.path"),
                path_kind=_enum(
                    PathKind, mapping["path_kind"], f"{what}.path_kind"
                ),
            )
        case TargetKind.LOCATION:
            _exact(mapping, {"kind", "path", "line", "column"}, what)
            return LocationTarget(
                path=_str(mapping["path"], f"{what}.path"),
                line=_int(mapping["line"], f"{what}.line", minimum=1),
                column=_int(mapping["column"], f"{what}.column", minimum=1),
            )
        case TargetKind.RANGE:
            _exact(mapping, {"kind", "path", "ranges"}, what)
            return RangeTarget(
                path=_str(mapping["path"], f"{what}.path"),
                ranges=tuple(
                    _span_from_wire(item, f"{what}.ranges[{index}]")
                    for index, item in enumerate(_list(mapping["ranges"], what))
                ),
            )
        case _:
            _exact(mapping, {"kind", "candidate_id", "symbol", "scopes"}, what)
            return CandidateTarget(
                candidate_id=_str(
                    mapping["candidate_id"], f"{what}.candidate_id"
                ),
                symbol=_str(mapping["symbol"], f"{what}.symbol"),
                scopes=_strings(mapping["scopes"], f"{what}.scopes"),
            )


def _candidate_wire(candidate: DeclarationCandidate) -> dict[str, object]:
    return candidate.to_wire()


def _candidate_from_wire(value: object, what: str) -> DeclarationCandidate:
    mapping = _mapping(value, what)
    _exact(
        mapping,
        {
            "candidate_id",
            "provider",
            "path",
            "kind",
            "span",
            "signature",
            "source_version",
            "scope",
            "external",
            "declaration_span",
        },
        what,
    )
    declaration_span = mapping["declaration_span"]
    return DeclarationCandidate(
        candidate_id=_str(mapping["candidate_id"], f"{what}.candidate_id"),
        provider=_str(mapping["provider"], f"{what}.provider"),
        path=_str(mapping["path"], f"{what}.path"),
        kind=_str(mapping["kind"], f"{what}.kind"),
        span=_span_from_wire(mapping["span"], f"{what}.span"),
        signature=_str(
            mapping["signature"], f"{what}.signature", allow_empty=True
        ),
        source_version=_str(
            mapping["source_version"], f"{what}.source_version"
        ),
        scope=_optional_str(mapping["scope"], f"{what}.scope"),
        external=_bool(mapping["external"], f"{what}.external"),
        declaration_span=(
            None
            if declaration_span is None
            else _span_from_wire(declaration_span, f"{what}.declaration_span")
        ),
    )


def _request_wire(request: InspectionRequest) -> dict[str, object]:
    return request.to_wire()


def _request_from_wire(value: object, what: str) -> InspectionRequest:
    mapping = _mapping(value, what)
    _exact(mapping, {"target", "intent", "evidence_scopes", "request_id"}, what)
    return InspectionRequest(
        target=_target_from_wire(mapping["target"], f"{what}.target"),
        intent=_enum(Intent, mapping["intent"], f"{what}.intent"),
        evidence_scopes=_strings(
            mapping["evidence_scopes"], f"{what}.evidence_scopes"
        ),
        request_id=_str(
            mapping["request_id"], f"{what}.request_id", allow_empty=True
        ),
    )


# ---------------------------------------------------------------------------
# Observation payloads and evidence
# ---------------------------------------------------------------------------


def _payload_wire(payload: ObservationPayload) -> dict[str, object]:
    kind = PAYLOAD_KINDS.get(type(payload))
    if kind is None:
        raise ContractError(f"unsupported observation payload: {type(payload).__name__}")
    return {"payload_kind": kind, **payload.to_wire()}


def _payload_from_wire(value: object, what: str) -> ObservationPayload:
    mapping = _mapping(value, what)
    kind = _str(mapping.get("payload_kind"), f"{what}.payload_kind")
    if kind == "declaration":
        _exact(
            mapping,
            {
                "payload_kind",
                "name",
                "kind",
                "signature",
                "span",
                "scope",
                "declaration_span",
            },
            what,
        )
        declaration_span = mapping["declaration_span"]
        return DeclarationPayload(
            name=_str(mapping["name"], f"{what}.name", allow_empty=True),
            kind=_str(mapping["kind"], f"{what}.kind"),
            signature=_str(mapping["signature"], f"{what}.signature", allow_empty=True),
            span=_span_from_wire(mapping["span"], f"{what}.span"),
            scope=_optional_str(mapping["scope"], f"{what}.scope"),
            declaration_span=(
                None
                if declaration_span is None
                else _span_from_wire(
                    declaration_span, f"{what}.declaration_span"
                )
            ),
        )
    if kind == "reference":
        _exact(
            mapping,
            {
                "payload_kind",
                "relationship",
                "text",
                "binding",
                "domain",
                "configuration",
            },
            what,
        )
        return ReferencePayload(
            relationship=_str(mapping["relationship"], f"{what}.relationship"),
            text=_str(mapping["text"], f"{what}.text", allow_empty=True),
            binding=_enum(Binding, mapping["binding"], f"{what}.binding"),
            domain=_optional_str(mapping["domain"], f"{what}.domain"),
            configuration=_optional_str(
                mapping["configuration"], f"{what}.configuration"
            ),
        )
    if kind == "source_window":
        _exact(mapping, {"payload_kind", "text", "span", "truncated"}, what)
        return SourceWindowPayload(
            text=_str(mapping["text"], f"{what}.text", allow_empty=True),
            span=_span_from_wire(mapping["span"], f"{what}.span"),
            truncated=_bool(mapping["truncated"], f"{what}.truncated"),
        )
    if kind == "outline":
        _exact(mapping, {"payload_kind", "symbols", "truncated"}, what)
        return OutlinePayload(
            symbols=tuple(
                _outline_symbol_from_wire(item, f"{what}.symbols[{index}]")
                for index, item in enumerate(_list(mapping["symbols"], what))
            ),
            truncated=_bool(mapping["truncated"], f"{what}.truncated"),
        )
    if kind == "package":
        _exact(
            mapping,
            {"payload_kind", "path", "kind", "name", "scripts"},
            what,
        )
        return PackagePayload(
            path=_str(mapping["path"], f"{what}.path", allow_empty=True),
            kind=_str(mapping["kind"], f"{what}.kind"),
            name=_optional_str(mapping["name"], f"{what}.name"),
            scripts=_strings(mapping["scripts"], f"{what}.scripts"),
        )
    if kind == "mention":
        _exact(mapping, {"payload_kind", "text", "domain"}, what)
        return MentionPayload(
            text=_str(mapping["text"], f"{what}.text", allow_empty=True),
            domain=_optional_str(mapping["domain"], f"{what}.domain"),
        )
    raise ContractError(f"unsupported observation payload kind: {kind!r}")


def _outline_symbol_wire(symbol: OutlineSymbolRef) -> dict[str, object]:
    return symbol.to_wire()


def _outline_symbol_from_wire(value: object, what: str) -> OutlineSymbolRef:
    mapping = _mapping(value, what)
    _exact(mapping, {"name", "kind", "span"}, what)
    return OutlineSymbolRef(
        name=_str(mapping["name"], f"{what}.name", allow_empty=True),
        kind=_str(mapping["kind"], f"{what}.kind"),
        span=_span_from_wire(mapping["span"], f"{what}.span"),
    )


def _observation_wire(observation: Observation) -> dict[str, object]:
    return {
        "observation_id": observation.observation_id,
        "kind": observation.kind.value,
        "payload": _payload_wire(observation.payload),
        "source": _source_wire(observation.source),
        "source_versions": [
            _version_wire(version) for version in observation.source_versions
        ],
        "acquisition_id": observation.acquisition_id,
    }


def _observation_from_wire(value: object, what: str) -> Observation:
    mapping = _mapping(value, what)
    _exact(
        mapping,
        {
            "observation_id",
            "kind",
            "payload",
            "source",
            "source_versions",
            "acquisition_id",
        },
        what,
    )
    return Observation(
        observation_id=_str(
            mapping["observation_id"], f"{what}.observation_id"
        ),
        kind=_enum(ObservationKind, mapping["kind"], f"{what}.kind"),
        payload=_payload_from_wire(mapping["payload"], f"{what}.payload"),
        source=_source_from_wire(mapping["source"], f"{what}.source"),
        source_versions=tuple(
            _version_from_wire(item, f"{what}.source_versions[{index}]")
            for index, item in enumerate(
                _list(mapping["source_versions"], what)
            )
        ),
        acquisition_id=_str(
            mapping["acquisition_id"], f"{what}.acquisition_id", allow_empty=True
        ),
    )


def _variant_wire(variant: EvidenceVariant) -> dict[str, object]:
    return variant.to_wire()


def _variant_from_wire(value: object, what: str) -> EvidenceVariant:
    mapping = _mapping(value, what)
    _exact(
        mapping,
        {
            "variant_id",
            "observation_id",
            "representation",
            "fidelity",
            "source",
            "span",
            "text",
        },
        what,
    )
    span = mapping["span"]
    return EvidenceVariant(
        variant_id=_str(mapping["variant_id"], f"{what}.variant_id"),
        observation_id=_str(
            mapping["observation_id"], f"{what}.observation_id"
        ),
        representation=_enum(
            RepresentationKind, mapping["representation"], f"{what}.representation"
        ),
        fidelity=_enum(Fidelity, mapping["fidelity"], f"{what}.fidelity"),
        source=_source_from_wire(mapping["source"], f"{what}.source"),
        text=_str(mapping["text"], f"{what}.text", allow_empty=True),
        span=None if span is None else _span_from_wire(span, f"{what}.span"),
    )


def _acquisition_wire(record: AcquisitionRecord) -> dict[str, object]:
    return {
        "acquisition_id": record.acquisition_id,
        "capability": record.capability.value,
        "provider": record.provider,
        "provider_version": record.provider_version,
        "method": record.method,
        "status": record.status.value,
        "effective_scope": list(record.effective_scope),
        "coverage": _coverage_wire(record.coverage),
        "diagnostics": _diagnostics_wire(record.diagnostics),
        "observed_inputs": list(record.observed_inputs),
    }


def _acquisition_from_wire(value: object, what: str) -> AcquisitionRecord:
    mapping = _mapping(value, what)
    _exact(
        mapping,
        {
            "acquisition_id",
            "capability",
            "provider",
            "provider_version",
            "method",
            "status",
            "effective_scope",
            "coverage",
            "diagnostics",
            "observed_inputs",
        },
        what,
    )
    return AcquisitionRecord(
        acquisition_id=_str(
            mapping["acquisition_id"], f"{what}.acquisition_id"
        ),
        capability=_enum(Capability, mapping["capability"], f"{what}.capability"),
        provider=_str(mapping["provider"], f"{what}.provider"),
        provider_version=_optional_str(
            mapping["provider_version"], f"{what}.provider_version"
        ),
        method=_str(mapping["method"], f"{what}.method"),
        effective_scope=_strings(
            mapping["effective_scope"], f"{what}.effective_scope"
        ),
        coverage=_coverage_from_wire(mapping["coverage"], f"{what}.coverage"),
        status=_enum(CollectionStatus, mapping["status"], f"{what}.status"),
        diagnostics=_diagnostics_from_wire(
            mapping["diagnostics"], f"{what}.diagnostics"
        ),
        observed_inputs=_strings(
            mapping["observed_inputs"], f"{what}.observed_inputs"
        ),
    )


def _acquired_wire(acquired: AcquiredEvidence) -> dict[str, object]:
    return {
        "record": _acquisition_wire(acquired.record),
        "observations": [
            _observation_wire(item) for item in acquired.observations
        ],
        "variants": [_variant_wire(item) for item in acquired.variants],
    }


def _acquired_from_wire(value: object, what: str) -> AcquiredEvidence:
    mapping = _mapping(value, what)
    _exact(mapping, {"record", "observations", "variants"}, what)
    return AcquiredEvidence(
        record=_acquisition_from_wire(mapping["record"], f"{what}.record"),
        observations=tuple(
            _observation_from_wire(item, f"{what}.observations[{index}]")
            for index, item in enumerate(_list(mapping["observations"], what))
        ),
        variants=tuple(
            _variant_from_wire(item, f"{what}.variants[{index}]")
            for index, item in enumerate(_list(mapping["variants"], what))
        ),
    )


def _pool_wire(pool: EvidencePool) -> dict[str, object]:
    return {
        "request_id": pool.request_id,
        "acquisitions": [_acquisition_wire(item) for item in pool.acquisitions],
        "observations": [_observation_wire(item) for item in pool.observations],
        "variants": [_variant_wire(item) for item in pool.variants],
        "limitations": _diagnostics_wire(pool.limitations),
        "unstable_observation_ids": list(pool.unstable_observation_ids),
        "coverage": _coverage_wire(pool.coverage),
    }


def _pool_from_wire(value: object, what: str) -> EvidencePool:
    mapping = _mapping(value, what)
    _exact(
        mapping,
        {
            "request_id",
            "acquisitions",
            "observations",
            "variants",
            "limitations",
            "unstable_observation_ids",
            "coverage",
        },
        what,
    )
    acquisitions = tuple(
        _acquisition_from_wire(item, f"{what}.acquisitions[{index}]")
        for index, item in enumerate(_list(mapping["acquisitions"], what))
    )
    observations = tuple(
        _observation_from_wire(item, f"{what}.observations[{index}]")
        for index, item in enumerate(_list(mapping["observations"], what))
    )
    variants = tuple(
        _variant_from_wire(item, f"{what}.variants[{index}]")
        for index, item in enumerate(_list(mapping["variants"], what))
    )
    _reject_duplicates(
        (item.acquisition_id for item in acquisitions),
        f"{what}.acquisitions",
    )
    _reject_duplicates(
        (item.observation_id for item in observations),
        f"{what}.observations",
    )
    _reject_duplicates((item.variant_id for item in variants), f"{what}.variants")
    observation_ids = {item.observation_id for item in observations}
    acquisition_ids = {item.acquisition_id for item in acquisitions}
    for variant in variants:
        if variant.observation_id not in observation_ids:
            raise ContractError(
                f"{what}.variants references unknown observation "
                f"{variant.observation_id!r}"
            )
    for observation in observations:
        if (
            observation.acquisition_id
            and observation.acquisition_id not in acquisition_ids
        ):
            raise ContractError(
                f"{what}.observations references unknown acquisition "
                f"{observation.acquisition_id!r}"
            )
    unstable = _strings(
        mapping["unstable_observation_ids"], f"{what}.unstable_observation_ids"
    )
    _reject_duplicates(unstable, f"{what}.unstable_observation_ids")
    for observation_id in unstable:
        if observation_id not in observation_ids:
            raise ContractError(
                f"{what}.unstable_observation_ids references unknown observation "
                f"{observation_id!r}"
            )
    return EvidencePool(
        request_id=_str(mapping["request_id"], f"{what}.request_id"),
        acquisitions=acquisitions,
        observations=observations,
        variants=variants,
        limitations=_diagnostics_from_wire(
            mapping["limitations"], f"{what}.limitations"
        ),
        unstable_observation_ids=unstable,
        coverage=_coverage_from_wire(mapping["coverage"], f"{what}.coverage"),
    )


def _reject_duplicates(values: Iterable[str], what: str) -> None:
    seen: set[str] = set()
    for value in values:
        if value in seen:
            raise ContractError(f"{what} contains a duplicate id: {value!r}")
        seen.add(value)


# ---------------------------------------------------------------------------
# Policy, collection, resolution, decision input
# ---------------------------------------------------------------------------


def _requirement_wire(requirement: EvidenceRequirement) -> dict[str, object]:
    return requirement.to_wire()


def _requirement_from_wire(value: object, what: str) -> EvidenceRequirement:
    mapping = _mapping(value, what)
    _exact(
        mapping,
        {
            "requirement_id",
            "role",
            "rule",
            "strength",
            "description",
            "capabilities",
            "acceptable_kinds",
            "representations",
            "domain",
        },
        what,
    )
    return EvidenceRequirement(
        requirement_id=_str(
            mapping["requirement_id"], f"{what}.requirement_id"
        ),
        role=_enum(EvidenceRole, mapping["role"], f"{what}.role"),
        rule=_enum(RequirementRule, mapping["rule"], f"{what}.rule"),
        strength=_enum(
            RequirementStrength, mapping["strength"], f"{what}.strength"
        ),
        description=_str(
            mapping["description"], f"{what}.description", allow_empty=True
        ),
        capabilities=tuple(
            _enum(Capability, item, f"{what}.capabilities[{index}]")
            for index, item in enumerate(_list(mapping["capabilities"], what))
        ),
        acceptable_kinds=tuple(
            _enum(ObservationKind, item, f"{what}.acceptable_kinds[{index}]")
            for index, item in enumerate(
                _list(mapping["acceptable_kinds"], what)
            )
        ),
        representations=tuple(
            _enum(RepresentationKind, item, f"{what}.representations[{index}]")
            for index, item in enumerate(
                _list(mapping["representations"], what)
            )
        ),
        domain=_optional_str(mapping["domain"], f"{what}.domain"),
    )


def _policy_wire(policy: EvidencePolicy) -> dict[str, object]:
    return policy.to_wire()


def _policy_from_wire(value: object, what: str) -> EvidencePolicy:
    mapping = _mapping(value, what)
    _exact(
        mapping,
        {"profile", "intent", "target_kind", "requirements", "limitations"},
        what,
    )
    return EvidencePolicy(
        profile=_str(mapping["profile"], f"{what}.profile"),
        intent=_enum(Intent, mapping["intent"], f"{what}.intent"),
        target_kind=_enum(TargetKind, mapping["target_kind"], f"{what}.target_kind"),
        requirements=tuple(
            _requirement_from_wire(item, f"{what}.requirements[{index}]")
            for index, item in enumerate(_list(mapping["requirements"], what))
        ),
        limitations=_strings(mapping["limitations"], f"{what}.limitations"),
    )


def _collection_wire(plan: CollectionPlan) -> dict[str, object]:
    return {
        "profile": plan.profile,
        "request_id": plan.request_id,
        "target": _target_wire(plan.target),
        "requests": [
            {
                "request_id": item.request_id,
                "capability": item.capability.value,
                "role": item.role.value,
                "requirement_id": item.requirement_id,
                "target": None if item.target is None else _target_wire(item.target),
                "subject": (
                    None if item.subject is None else _candidate_wire(item.subject)
                ),
                "scope": list(item.scope),
                "domain": item.domain,
                "limit": item.limit,
                "detail": item.detail,
            }
            for item in plan.requests
        ],
        "omissions": [
            {
                "requirement_id": item.requirement_id,
                "capability": item.capability.value,
                "status": item.status.value,
                "reason": item.reason,
            }
            for item in plan.omissions
        ],
    }


def _collection_from_wire(value: object, what: str) -> CollectionPlan:
    mapping = _mapping(value, what)
    _exact(
        mapping, {"profile", "request_id", "target", "requests", "omissions"}, what
    )
    requests: list[CollectionRequest] = []
    for index, item in enumerate(_list(mapping["requests"], what)):
        request = _mapping(item, f"{what}.requests[{index}]")
        _exact(
            request,
            {
                "request_id",
                "capability",
                "role",
                "requirement_id",
                "target",
                "subject",
                "scope",
                "domain",
                "limit",
                "detail",
            },
            f"{what}.requests[{index}]",
        )
        target = request["target"]
        subject = request["subject"]
        requests.append(
            CollectionRequest(
                request_id=_str(
                    request["request_id"], f"{what}.requests[{index}].request_id"
                ),
                capability=_enum(
                    Capability,
                    request["capability"],
                    f"{what}.requests[{index}].capability",
                ),
                role=_enum(
                    EvidenceRole, request["role"], f"{what}.requests[{index}].role"
                ),
                requirement_id=_str(
                    request["requirement_id"],
                    f"{what}.requests[{index}].requirement_id",
                ),
                target=(
                    None
                    if target is None
                    else _target_from_wire(
                        target, f"{what}.requests[{index}].target"
                    )
                ),
                subject=(
                    None
                    if subject is None
                    else _candidate_from_wire(
                        subject, f"{what}.requests[{index}].subject"
                    )
                ),
                scope=_strings(
                    request["scope"], f"{what}.requests[{index}].scope"
                ),
                domain=_optional_str(
                    request["domain"], f"{what}.requests[{index}].domain"
                ),
                limit=_int(
                    request["limit"], f"{what}.requests[{index}].limit", minimum=1
                ),
                detail=_str(
                    request["detail"],
                    f"{what}.requests[{index}].detail",
                    allow_empty=True,
                ),
            )
        )
    omissions: list[RequirementOmission] = []
    for index, item in enumerate(_list(mapping["omissions"], what)):
        omission = _mapping(item, f"{what}.omissions[{index}]")
        _exact(
            omission,
            {"requirement_id", "capability", "status", "reason"},
            f"{what}.omissions[{index}]",
        )
        omissions.append(
            RequirementOmission(
                requirement_id=_str(
                    omission["requirement_id"],
                    f"{what}.omissions[{index}].requirement_id",
                ),
                capability=_enum(
                    Capability,
                    omission["capability"],
                    f"{what}.omissions[{index}].capability",
                ),
                status=_enum(
                    AvailabilityStatus,
                    omission["status"],
                    f"{what}.omissions[{index}].status",
                ),
                reason=_str(
                    omission["reason"],
                    f"{what}.omissions[{index}].reason",
                    allow_empty=True,
                ),
            )
        )
    return CollectionPlan(
        profile=_str(mapping["profile"], f"{what}.profile"),
        request_id=_str(mapping["request_id"], f"{what}.request_id"),
        target=_target_from_wire(mapping["target"], f"{what}.target"),
        requests=tuple(requests),
        omissions=tuple(omissions),
    )


def _resolution_wire(resolution: ResolvedTarget) -> dict[str, object]:
    return {
        "outcome": "resolved",
        "method": resolution.method.value,
        "target": _target_wire(resolution.target),
        "declaration": (
            None
            if resolution.declaration is None
            else _candidate_wire(resolution.declaration)
        ),
        "candidate_coverage": _coverage_wire(resolution.candidate_coverage),
        "candidate_evidence": [
            _acquired_wire(item) for item in resolution.candidate_evidence
        ],
    }


def _resolution_from_wire(value: object, what: str) -> ResolvedTarget:
    mapping = _mapping(value, what)
    _exact(
        mapping,
        {
            "outcome",
            "method",
            "target",
            "declaration",
            "candidate_coverage",
            "candidate_evidence",
        },
        what,
    )
    outcome = _str(mapping["outcome"], f"{what}.outcome")
    if outcome != "resolved":
        raise ContractError(
            f"{what} must be a resolved target for a decision input"
        )
    declaration = mapping["declaration"]
    return ResolvedTarget(
        target=_target_from_wire(mapping["target"], f"{what}.target"),
        method=_enum(SelectionMethod, mapping["method"], f"{what}.method"),
        declaration=(
            None
            if declaration is None
            else _candidate_from_wire(declaration, f"{what}.declaration")
        ),
        candidate_coverage=_coverage_from_wire(
            mapping["candidate_coverage"], f"{what}.candidate_coverage"
        ),
        candidate_evidence=tuple(
            _acquired_from_wire(item, f"{what}.candidate_evidence[{index}]")
            for index, item in enumerate(
                _list(mapping["candidate_evidence"], what)
            )
        ),
    )


def _decision_input_wire(decision: DecisionInput) -> dict[str, object]:
    return {
        "request": _request_wire(decision.request),
        "resolution": _resolution_wire(decision.resolution),
        "policy": _policy_wire(decision.policy),
        "collection": _collection_wire(decision.collection),
        "pool": _pool_wire(decision.pool),
    }


def _decision_input_from_wire(value: object, what: str) -> DecisionInput:
    mapping = _mapping(value, what)
    _exact(mapping, {"request", "resolution", "policy", "collection", "pool"}, what)
    return DecisionInput(
        request=_request_from_wire(mapping["request"], f"{what}.request"),
        resolution=_resolution_from_wire(
            mapping["resolution"], f"{what}.resolution"
        ),
        policy=_policy_from_wire(mapping["policy"], f"{what}.policy"),
        collection=_collection_from_wire(
            mapping["collection"], f"{what}.collection"
        ),
        pool=_pool_from_wire(mapping["pool"], f"{what}.pool"),
    )


# ---------------------------------------------------------------------------
# Capture, snapshot, limits, capability report
# ---------------------------------------------------------------------------


def _snapshot_wire(snapshot: Snapshot) -> dict[str, object]:
    if isinstance(snapshot, FixtureSnapshot):
        return {
            "kind": "fixture",
            "fixture_id": snapshot.fixture_id,
            "fixture_revision": snapshot.fixture_revision,
            "content_digest": snapshot.content_digest,
        }
    return {
        "kind": "repository",
        "repo_id": snapshot.repo_id,
        "commit": snapshot.commit,
        "tree": snapshot.tree,
        "source_manifest_digest": snapshot.source_manifest_digest,
        "configuration_manifest_digest": snapshot.configuration_manifest_digest,
    }


def _snapshot_from_wire(value: object, what: str) -> Snapshot:
    mapping = _mapping(value, what)
    kind = _str(mapping.get("kind"), f"{what}.kind")
    if kind == "fixture":
        _exact(
            mapping,
            {"kind", "fixture_id", "fixture_revision", "content_digest"},
            what,
        )
        return FixtureSnapshot(
            fixture_id=_str(mapping["fixture_id"], f"{what}.fixture_id"),
            fixture_revision=_str(
                mapping["fixture_revision"], f"{what}.fixture_revision"
            ),
            content_digest=_str(
                mapping["content_digest"], f"{what}.content_digest"
            ),
        )
    if kind == "repository":
        _exact(
            mapping,
            {
                "kind",
                "repo_id",
                "commit",
                "tree",
                "source_manifest_digest",
                "configuration_manifest_digest",
            },
            what,
        )
        return RepositorySnapshot(
            repo_id=_str(mapping["repo_id"], f"{what}.repo_id"),
            commit=_str(mapping["commit"], f"{what}.commit"),
            tree=_str(mapping["tree"], f"{what}.tree"),
            source_manifest_digest=_str(
                mapping["source_manifest_digest"],
                f"{what}.source_manifest_digest",
            ),
            configuration_manifest_digest=_str(
                mapping["configuration_manifest_digest"],
                f"{what}.configuration_manifest_digest",
            ),
        )
    raise ContractError(f"unsupported snapshot kind: {kind!r}")


LIMIT_FIELDS = (
    "max_candidates",
    "max_observations",
    "max_source_files",
    "max_source_lines",
    "max_provider_calls",
    "reference_limit",
    "lexical_test_mentions",
)


def _limits_wire(limits: AcquisitionLimits) -> dict[str, object]:
    return {
        **{name: getattr(limits, name) for name in LIMIT_FIELDS},
        "deadline_seconds": limits.deadline_seconds,
    }


def _limits_from_wire(value: object, what: str) -> AcquisitionLimits:
    mapping = _mapping(value, what)
    _exact(mapping, {*LIMIT_FIELDS, "deadline_seconds"}, what)
    return AcquisitionLimits(
        **{
            name: _int(mapping[name], f"{what}.{name}", minimum=1)
            for name in LIMIT_FIELDS
        },
        deadline_seconds=_number(
            mapping["deadline_seconds"], f"{what}.deadline_seconds"
        ),
    )


def _report_wire(report: CapabilityReport) -> dict[str, object]:
    return {
        "request_id": report.request_id,
        "entries": [
            {
                "capability": entry.capability.value,
                "status": entry.status.value,
                "provider": entry.provider,
                "provider_version": entry.provider_version,
                "reason": entry.reason,
                "diagnostics": _diagnostics_wire(entry.diagnostics),
            }
            for entry in report.entries
        ],
    }


def _report_from_wire(value: object, what: str) -> CapabilityReport:
    mapping = _mapping(value, what)
    _exact(mapping, {"request_id", "entries"}, what)
    entries: list[CapabilityEntry] = []
    for index, item in enumerate(_list(mapping["entries"], what)):
        entry = _mapping(item, f"{what}.entries[{index}]")
        _exact(
            entry,
            {
                "capability",
                "status",
                "provider",
                "provider_version",
                "reason",
                "diagnostics",
            },
            f"{what}.entries[{index}]",
        )
        entries.append(
            CapabilityEntry(
                capability=_enum(
                    Capability,
                    entry["capability"],
                    f"{what}.entries[{index}].capability",
                ),
                status=_enum(
                    AvailabilityStatus,
                    entry["status"],
                    f"{what}.entries[{index}].status",
                ),
                provider=_optional_str(
                    entry["provider"], f"{what}.entries[{index}].provider"
                ),
                provider_version=_optional_str(
                    entry["provider_version"],
                    f"{what}.entries[{index}].provider_version",
                ),
                reason=_optional_str(
                    entry["reason"], f"{what}.entries[{index}].reason"
                ),
                diagnostics=_diagnostics_from_wire(
                    entry["diagnostics"],
                    f"{what}.entries[{index}].diagnostics",
                ),
            )
        )
    return CapabilityReport(
        request_id=_str(mapping["request_id"], f"{what}.request_id"),
        entries=tuple(entries),
    )


def _producer_wire(producer: ProducerFingerprint) -> dict[str, object]:
    return {"provider": producer.provider, "version": producer.version}


def _producer_from_wire(value: object, what: str) -> ProducerFingerprint:
    mapping = _mapping(value, what)
    _exact(mapping, {"provider", "version"}, what)
    return ProducerFingerprint(
        provider=_str(mapping["provider"], f"{what}.provider"),
        version=_optional_str(mapping["version"], f"{what}.version"),
    )


def _capture_wire(capture: ReplayCapture) -> dict[str, object]:
    return {
        "schema": capture.schema,
        "case_id": capture.case_id,
        "snapshot": _snapshot_wire(capture.snapshot),
        "producers": [_producer_wire(item) for item in capture.producers],
        "limits": _limits_wire(capture.limits),
        "capability_report": (
            None
            if capture.capability_report is None
            else _report_wire(capture.capability_report)
        ),
        "decision": _decision_input_wire(capture.decision),
    }


def _capture_from_wire(value: object, what: str) -> ReplayCapture:
    mapping = _mapping(value, what)
    _exact(
        mapping,
        {
            "schema",
            "case_id",
            "snapshot",
            "producers",
            "limits",
            "capability_report",
            "decision",
        },
        what,
    )
    schema = _str(mapping["schema"], f"{what}.schema")
    if schema != CAPTURE_SCHEMA:
        raise ContractError(f"unsupported capture schema: {schema!r}")
    report = mapping["capability_report"]
    return ReplayCapture(
        case_id=_str(mapping["case_id"], f"{what}.case_id"),
        snapshot=_snapshot_from_wire(mapping["snapshot"], f"{what}.snapshot"),
        producers=tuple(
            _producer_from_wire(item, f"{what}.producers[{index}]")
            for index, item in enumerate(_list(mapping["producers"], what))
        ),
        limits=_limits_from_wire(mapping["limits"], f"{what}.limits"),
        decision=_decision_input_from_wire(
            mapping["decision"], f"{what}.decision"
        ),
        capability_report=(
            None if report is None else _report_from_wire(report, f"{what}.capability_report")
        ),
        schema=schema,
    )


def encode_capture(capture: ReplayCapture) -> bytes:
    """The canonical capture bytes whose SHA-256 is the capture identity."""
    return canonical_json(_capture_wire(capture)).encode("utf-8")


def decode_capture(data: bytes) -> ReplayCapture:
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ContractError(f"capture is not valid UTF-8: {exc}") from exc
    return _capture_from_wire(decode_json(text, what="capture"), "capture")


def capture_digest(capture: ReplayCapture) -> str:
    return hashlib.sha256(encode_capture(capture)).hexdigest()


def decision_input_digest(decision: DecisionInput) -> str:
    """Content identity of one decision input, independent of capture metadata."""
    return hashlib.sha256(
        canonical_json(_decision_input_wire(decision)).encode("utf-8")
    ).hexdigest()


# ---------------------------------------------------------------------------
# Decision configuration
# ---------------------------------------------------------------------------


def _scoring_wire(profile: ScoringProfile) -> dict[str, object]:
    return profile.to_wire()


def _scoring_from_wire(value: object, what: str) -> ScoringProfile:
    mapping = _mapping(value, what)
    _exact(mapping, {"profile", "binding_bonus", "intent_priorities"}, what)
    priorities = _mapping(mapping["intent_priorities"], f"{what}.intent_priorities")
    unknown = sorted(set(priorities) - {intent.value for intent in Intent})
    if unknown:
        raise ContractError(
            f"{what}.intent_priorities has unknown intents: {', '.join(unknown)}"
        )
    known_roles = {role.value for role in EvidenceRole}
    decoded: list[tuple[Intent, tuple[tuple[EvidenceRole, int], ...]]] = []
    for intent in Intent:
        if intent.value not in priorities:
            continue
        role_map = _mapping(
            priorities[intent.value], f"{what}.intent_priorities.{intent.value}"
        )
        unknown_roles = sorted(set(role_map) - known_roles)
        if unknown_roles:
            raise ContractError(
                f"{what}.intent_priorities.{intent.value} has unknown roles: "
                f"{', '.join(unknown_roles)}"
            )
        decoded.append(
            (
                intent,
                tuple(
                    (
                        role,
                        _int(
                            role_map[role.value],
                            f"{what}.intent_priorities.{intent.value}.{role.value}",
                        ),
                    )
                    for role in EvidenceRole
                    if role.value in role_map
                ),
            )
        )
    return ScoringProfile(
        profile=_str(mapping["profile"], f"{what}.profile"),
        binding_bonus=_int(mapping["binding_bonus"], f"{what}.binding_bonus"),
        intent_priorities=tuple(decoded),
    )


def _selection_wire(profile: SelectionProfile) -> dict[str, object]:
    return profile.to_wire()


def _selection_from_wire(value: object, what: str) -> SelectionProfile:
    mapping = _mapping(value, what)
    _exact(
        mapping,
        {
            "profile",
            "reserve_required",
            "role_diversity",
            "per_file_limit",
            "fill_by_score",
        },
        what,
    )
    return SelectionProfile(
        profile=_str(mapping["profile"], f"{what}.profile"),
        reserve_required=_bool(
            mapping["reserve_required"], f"{what}.reserve_required"
        ),
        role_diversity=_bool(mapping["role_diversity"], f"{what}.role_diversity"),
        per_file_limit=_int(
            mapping["per_file_limit"], f"{what}.per_file_limit", minimum=1
        ),
        fill_by_score=_bool(mapping["fill_by_score"], f"{what}.fill_by_score"),
    )


def _delivery_wire(delivery: DeliveryBudget) -> dict[str, object]:
    return {"max_chars": delivery.max_chars, "envelope_chars": delivery.envelope_chars}


def _delivery_from_wire(value: object, what: str) -> DeliveryBudget:
    mapping = _mapping(value, what)
    _exact(mapping, {"max_chars", "envelope_chars"}, what)
    return DeliveryBudget(
        max_chars=_int(mapping["max_chars"], f"{what}.max_chars", minimum=1),
        envelope_chars=_int(
            mapping["envelope_chars"], f"{what}.envelope_chars", minimum=0
        ),
    )


def _config_wire(config: DecisionConfig) -> dict[str, object]:
    return {
        "schema": CONFIG_SCHEMA,
        "scoring": _scoring_wire(config.scoring),
        "selection": _selection_wire(config.selection),
        "delivery": _delivery_wire(config.delivery),
        "output_format": config.output_format,
    }


def encode_config(config: DecisionConfig) -> bytes:
    return canonical_json(_config_wire(config)).encode("utf-8")


def decode_config(data: bytes) -> DecisionConfig:
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ContractError(f"decision config is not valid UTF-8: {exc}") from exc
    mapping = _mapping(decode_json(text, what="decision config"), "decision config")
    _exact(
        mapping,
        {"schema", "scoring", "selection", "delivery", "output_format"},
        "decision config",
    )
    schema = _str(mapping["schema"], "decision config.schema")
    if schema != CONFIG_SCHEMA:
        raise ContractError(f"unsupported decision config schema: {schema!r}")
    return DecisionConfig(
        scoring=_scoring_from_wire(mapping["scoring"], "decision config.scoring"),
        selection=_selection_from_wire(
            mapping["selection"], "decision config.selection"
        ),
        delivery=_delivery_from_wire(
            mapping["delivery"], "decision config.delivery"
        ),
        output_format=_str(
            mapping["output_format"], "decision config.output_format"
        ),
    )


def config_digest(config: DecisionConfig) -> str:
    return hashlib.sha256(encode_config(config)).hexdigest()


# ---------------------------------------------------------------------------
# Suite locks
# ---------------------------------------------------------------------------


def _lock_wire(lock: SuiteLock) -> dict[str, object]:
    return {
        "schema": lock.schema,
        "suite_id": lock.suite_id,
        "cases": [
            {
                "case_id": case.case_id,
                "capture_id": case.capture_id,
                "judgment_id": case.judgment_id,
            }
            for case in lock.cases
        ],
    }


def encode_lock(lock: SuiteLock) -> bytes:
    return canonical_json(_lock_wire(lock)).encode("utf-8")


def decode_lock(data: bytes) -> SuiteLock:
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ContractError(f"suite lock is not valid UTF-8: {exc}") from exc
    mapping = _mapping(decode_json(text, what="suite lock"), "suite lock")
    _exact(mapping, {"schema", "suite_id", "cases"}, "suite lock")
    schema = _str(mapping["schema"], "suite lock.schema")
    if schema != LOCK_SCHEMA:
        raise ContractError(f"unsupported suite lock schema: {schema!r}")
    cases: list[LockedCase] = []
    for index, item in enumerate(_list(mapping["cases"], "suite lock.cases")):
        case = _mapping(item, f"suite lock.cases[{index}]")
        _exact(
            case,
            {"case_id", "capture_id", "judgment_id"},
            f"suite lock.cases[{index}]",
        )
        cases.append(
            LockedCase(
                case_id=_str(
                    case["case_id"], f"suite lock.cases[{index}].case_id"
                ),
                capture_id=_str(
                    case["capture_id"], f"suite lock.cases[{index}].capture_id"
                ),
                judgment_id=_optional_str(
                    case["judgment_id"],
                    f"suite lock.cases[{index}].judgment_id",
                ),
            )
        )
    _reject_duplicates(
        (case.case_id for case in cases), "suite lock.cases case ids"
    )
    return SuiteLock(
        suite_id=_str(mapping["suite_id"], "suite lock.suite_id"),
        cases=tuple(cases),
        schema=schema,
    )
