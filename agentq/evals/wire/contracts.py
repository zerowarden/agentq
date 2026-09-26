"""Wire codecs for the runtime inspection contracts.

Targets, requests, observations, payloads, acquisitions, pools,
policies, collection plans, resolutions, and the decision input.
"""

from __future__ import annotations

import hashlib

from agentq.core import ContractError, Coverage, Diagnostic, SourceRef, canonical_json
from agentq.inspection.contracts import (
    AcquiredEvidence,
    AcquisitionRecord,
    AvailabilityStatus,
    Binding,
    CandidateTarget,
    Capability,
    CollectionPlan,
    CollectionRequest,
    CollectionStatus,
    DecisionInput,
    DeclarationCandidate,
    DeclarationPayload,
    EvidencePolicy,
    EvidencePool,
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

from .json import (
    as_list,
    as_mapping,
    object_fields,
    optional_int,
    optional_str,
    read_bool,
    read_enum,
    read_int,
    read_str,
    read_strings,
    reject_duplicates,
)

PAYLOAD_KINDS = {
    DeclarationPayload: "declaration",
    ReferencePayload: "reference",
    SourceWindowPayload: "source_window",
    OutlinePayload: "outline",
    PackagePayload: "package",
    MentionPayload: "mention",
}


def _span_from_wire(value: object, what: str) -> SourceSpan:
    mapping = object_fields(
        value, what, {"start_line", "start_column", "end_line", "end_column"}
    )
    return SourceSpan(
        start_line=read_int(mapping["start_line"], f"{what}.start_line", minimum=1),
        start_column=optional_int(
            mapping["start_column"], f"{what}.start_column", minimum=1
        ),
        end_line=read_int(mapping["end_line"], f"{what}.end_line", minimum=1),
        end_column=optional_int(
            mapping["end_column"], f"{what}.end_column", minimum=1
        ),
    )


def _source_from_wire(value: object, what: str) -> SourceRef:
    mapping = object_fields(value, what, {"path", "start_line", "end_line", "symbol"})
    return SourceRef(
        path=optional_str(mapping["path"], f"{what}.path"),
        start_line=optional_int(
            mapping["start_line"], f"{what}.start_line", minimum=1
        ),
        end_line=optional_int(mapping["end_line"], f"{what}.end_line", minimum=1),
        symbol=optional_str(mapping["symbol"], f"{what}.symbol"),
    )


def _version_from_wire(value: object, what: str) -> SourceVersion:
    mapping = object_fields(value, what, {"path", "version", "method"})
    return SourceVersion(
        path=read_str(mapping["path"], f"{what}.path", allow_empty=True),
        version=read_str(mapping["version"], f"{what}.version", allow_empty=True),
        method=read_str(mapping["method"], f"{what}.method", allow_empty=True),
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
    mapping = object_fields(value, what, {
            "status",
            "reasons",
            "domain",
            "scope",
            "count_quality",
            "scanned",
            "matched",
            "retained",
            "omitted",
        })
    return Coverage(
        status=read_str(mapping["status"], f"{what}.status"),
        reasons=read_strings(mapping["reasons"], f"{what}.reasons"),
        domain=optional_str(mapping["domain"], f"{what}.domain"),
        scope=optional_str(mapping["scope"], f"{what}.scope"),
        count_quality=read_str(mapping["count_quality"], f"{what}.count_quality"),
        scanned=optional_int(mapping["scanned"], f"{what}.scanned", minimum=0),
        matched=optional_int(mapping["matched"], f"{what}.matched", minimum=0),
        retained=optional_int(mapping["retained"], f"{what}.retained", minimum=0),
        omitted=optional_int(mapping["omitted"], f"{what}.omitted", minimum=0),
    )


def _diagnostic_from_wire(value: object, what: str) -> Diagnostic:
    mapping = object_fields(value, what, {"message", "code", "path", "severity"})
    return Diagnostic(
        message=read_str(mapping["message"], f"{what}.message", allow_empty=True),
        code=read_str(mapping["code"], f"{what}.code"),
        path=optional_str(mapping["path"], f"{what}.path"),
        severity=read_str(mapping["severity"], f"{what}.severity"),
    )


def _diagnostics_wire(items: tuple[Diagnostic, ...]) -> list[object]:
    return [item.to_wire() for item in items]


def _diagnostics_from_wire(value: object, what: str) -> tuple[Diagnostic, ...]:
    return tuple(
        _diagnostic_from_wire(item, f"{what}[{index}]")
        for index, item in enumerate(as_list(value, what))
    )


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
    mapping = as_mapping(value, what)
    kind = read_enum(TargetKind, mapping.get("kind"), f"{what}.kind")
    match kind:
        case TargetKind.SYMBOL:
            object_fields(mapping, what, {"kind", "name", "scopes"})
            return SymbolTarget(
                name=read_str(mapping["name"], f"{what}.name"),
                scopes=read_strings(mapping["scopes"], f"{what}.scopes"),
            )
        case TargetKind.PATH:
            object_fields(mapping, what, {"kind", "path", "path_kind"})
            return PathTarget(
                path=read_str(mapping["path"], f"{what}.path"),
                path_kind=read_enum(
                    PathKind, mapping["path_kind"], f"{what}.path_kind"
                ),
            )
        case TargetKind.LOCATION:
            object_fields(mapping, what, {"kind", "path", "line", "column"})
            return LocationTarget(
                path=read_str(mapping["path"], f"{what}.path"),
                line=read_int(mapping["line"], f"{what}.line", minimum=1),
                column=read_int(mapping["column"], f"{what}.column", minimum=1),
            )
        case TargetKind.RANGE:
            object_fields(mapping, what, {"kind", "path", "ranges"})
            return RangeTarget(
                path=read_str(mapping["path"], f"{what}.path"),
                ranges=tuple(
                    _span_from_wire(item, f"{what}.ranges[{index}]")
                    for index, item in enumerate(as_list(mapping["ranges"], what))
                ),
            )
        case _:
            object_fields(mapping, what, {"kind", "candidate_id", "symbol", "scopes"})
            return CandidateTarget(
                candidate_id=read_str(
                    mapping["candidate_id"], f"{what}.candidate_id"
                ),
                symbol=read_str(mapping["symbol"], f"{what}.symbol"),
                scopes=read_strings(mapping["scopes"], f"{what}.scopes"),
            )


def _candidate_from_wire(value: object, what: str) -> DeclarationCandidate:
    mapping = object_fields(value, what, {
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
        })
    declaration_span = mapping["declaration_span"]
    return DeclarationCandidate(
        candidate_id=read_str(mapping["candidate_id"], f"{what}.candidate_id"),
        provider=read_str(mapping["provider"], f"{what}.provider"),
        path=read_str(mapping["path"], f"{what}.path"),
        kind=read_str(mapping["kind"], f"{what}.kind"),
        span=_span_from_wire(mapping["span"], f"{what}.span"),
        signature=read_str(
            mapping["signature"], f"{what}.signature", allow_empty=True
        ),
        source_version=read_str(
            mapping["source_version"], f"{what}.source_version"
        ),
        scope=optional_str(mapping["scope"], f"{what}.scope"),
        external=read_bool(mapping["external"], f"{what}.external"),
        declaration_span=(
            None
            if declaration_span is None
            else _span_from_wire(declaration_span, f"{what}.declaration_span")
        ),
    )


def _request_from_wire(value: object, what: str) -> InspectionRequest:
    mapping = object_fields(
        value, what, {"target", "intent", "evidence_scopes", "request_id"}
    )
    return InspectionRequest(
        target=_target_from_wire(mapping["target"], f"{what}.target"),
        intent=read_enum(Intent, mapping["intent"], f"{what}.intent"),
        evidence_scopes=read_strings(
            mapping["evidence_scopes"], f"{what}.evidence_scopes"
        ),
        request_id=read_str(
            mapping["request_id"], f"{what}.request_id", allow_empty=True
        ),
    )


def _payload_wire(payload: ObservationPayload) -> dict[str, object]:
    kind = PAYLOAD_KINDS.get(type(payload))
    if kind is None:
        raise ContractError(f"unsupported observation payload: {type(payload).__name__}")
    return {"payload_kind": kind, **payload.to_wire()}


def _payload_from_wire(value: object, what: str) -> ObservationPayload:
    mapping = as_mapping(value, what)
    kind = read_str(mapping.get("payload_kind"), f"{what}.payload_kind")
    if kind == "declaration":
        object_fields(
            mapping,
            what,
            {
                "payload_kind",
                "name",
                "kind",
                "signature",
                "span",
                "scope",
                "declaration_span",
            },
        )
        declaration_span = mapping["declaration_span"]
        return DeclarationPayload(
            name=read_str(mapping["name"], f"{what}.name", allow_empty=True),
            kind=read_str(mapping["kind"], f"{what}.kind"),
            signature=read_str(mapping["signature"], f"{what}.signature", allow_empty=True),
            span=_span_from_wire(mapping["span"], f"{what}.span"),
            scope=optional_str(mapping["scope"], f"{what}.scope"),
            declaration_span=(
                None
                if declaration_span is None
                else _span_from_wire(
                    declaration_span, f"{what}.declaration_span"
                )
            ),
        )
    if kind == "reference":
        object_fields(
            mapping,
            what,
            {
                "payload_kind",
                "relationship",
                "text",
                "binding",
                "domain",
                "configuration",
            },
        )
        return ReferencePayload(
            relationship=read_str(mapping["relationship"], f"{what}.relationship"),
            text=read_str(mapping["text"], f"{what}.text", allow_empty=True),
            binding=read_enum(Binding, mapping["binding"], f"{what}.binding"),
            domain=optional_str(mapping["domain"], f"{what}.domain"),
            configuration=optional_str(
                mapping["configuration"], f"{what}.configuration"
            ),
        )
    if kind == "source_window":
        object_fields(mapping, what, {"payload_kind", "text", "span", "truncated"})
        return SourceWindowPayload(
            text=read_str(mapping["text"], f"{what}.text", allow_empty=True),
            span=_span_from_wire(mapping["span"], f"{what}.span"),
            truncated=read_bool(mapping["truncated"], f"{what}.truncated"),
        )
    if kind == "outline":
        object_fields(mapping, what, {"payload_kind", "symbols", "truncated"})
        return OutlinePayload(
            symbols=tuple(
                _outline_symbol_from_wire(item, f"{what}.symbols[{index}]")
                for index, item in enumerate(as_list(mapping["symbols"], what))
            ),
            truncated=read_bool(mapping["truncated"], f"{what}.truncated"),
        )
    if kind == "package":
        object_fields(
            mapping, what, {"payload_kind", "path", "kind", "name", "scripts"}
        )
        return PackagePayload(
            path=read_str(mapping["path"], f"{what}.path", allow_empty=True),
            kind=read_str(mapping["kind"], f"{what}.kind"),
            name=optional_str(mapping["name"], f"{what}.name"),
            scripts=read_strings(mapping["scripts"], f"{what}.scripts"),
        )
    if kind == "mention":
        object_fields(mapping, what, {"payload_kind", "text", "domain"})
        return MentionPayload(
            text=read_str(mapping["text"], f"{what}.text", allow_empty=True),
            domain=optional_str(mapping["domain"], f"{what}.domain"),
        )
    raise ContractError(f"unsupported observation payload kind: {kind!r}")


def _outline_symbol_from_wire(value: object, what: str) -> OutlineSymbolRef:
    mapping = object_fields(value, what, {"name", "kind", "span"})
    return OutlineSymbolRef(
        name=read_str(mapping["name"], f"{what}.name", allow_empty=True),
        kind=read_str(mapping["kind"], f"{what}.kind"),
        span=_span_from_wire(mapping["span"], f"{what}.span"),
    )


def _observation_wire(observation: Observation) -> dict[str, object]:
    return {
        "observation_id": observation.observation_id,
        "kind": observation.kind.value,
        "payload": _payload_wire(observation.payload),
        "source": observation.source.to_wire(),
        "source_versions": [
            version.to_wire() for version in observation.source_versions
        ],
        "acquisition_id": observation.acquisition_id,
    }


def _observation_from_wire(value: object, what: str) -> Observation:
    mapping = object_fields(value, what, {
            "observation_id",
            "kind",
            "payload",
            "source",
            "source_versions",
            "acquisition_id",
        })
    return Observation(
        observation_id=read_str(
            mapping["observation_id"], f"{what}.observation_id"
        ),
        kind=read_enum(ObservationKind, mapping["kind"], f"{what}.kind"),
        payload=_payload_from_wire(mapping["payload"], f"{what}.payload"),
        source=_source_from_wire(mapping["source"], f"{what}.source"),
        source_versions=tuple(
            _version_from_wire(item, f"{what}.source_versions[{index}]")
            for index, item in enumerate(
                as_list(mapping["source_versions"], what)
            )
        ),
        acquisition_id=read_str(
            mapping["acquisition_id"], f"{what}.acquisition_id", allow_empty=True
        ),
    )


def _variant_from_wire(value: object, what: str) -> EvidenceVariant:
    mapping = object_fields(value, what, {
            "variant_id",
            "observation_id",
            "representation",
            "fidelity",
            "source",
            "span",
            "text",
        })
    span = mapping["span"]
    return EvidenceVariant(
        variant_id=read_str(mapping["variant_id"], f"{what}.variant_id"),
        observation_id=read_str(
            mapping["observation_id"], f"{what}.observation_id"
        ),
        representation=read_enum(
            RepresentationKind, mapping["representation"], f"{what}.representation"
        ),
        fidelity=read_enum(Fidelity, mapping["fidelity"], f"{what}.fidelity"),
        source=_source_from_wire(mapping["source"], f"{what}.source"),
        text=read_str(mapping["text"], f"{what}.text", allow_empty=True),
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
    mapping = object_fields(value, what, {
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
        })
    return AcquisitionRecord(
        acquisition_id=read_str(
            mapping["acquisition_id"], f"{what}.acquisition_id"
        ),
        capability=read_enum(Capability, mapping["capability"], f"{what}.capability"),
        provider=read_str(mapping["provider"], f"{what}.provider"),
        provider_version=optional_str(
            mapping["provider_version"], f"{what}.provider_version"
        ),
        method=read_str(mapping["method"], f"{what}.method"),
        effective_scope=read_strings(
            mapping["effective_scope"], f"{what}.effective_scope"
        ),
        coverage=_coverage_from_wire(mapping["coverage"], f"{what}.coverage"),
        status=read_enum(CollectionStatus, mapping["status"], f"{what}.status"),
        diagnostics=_diagnostics_from_wire(
            mapping["diagnostics"], f"{what}.diagnostics"
        ),
        observed_inputs=read_strings(
            mapping["observed_inputs"], f"{what}.observed_inputs"
        ),
    )


def _acquired_wire(acquired: AcquiredEvidence) -> dict[str, object]:
    return {
        "record": _acquisition_wire(acquired.record),
        "observations": [
            _observation_wire(item) for item in acquired.observations
        ],
        "variants": [item.to_wire() for item in acquired.variants],
    }


def _acquired_from_wire(value: object, what: str) -> AcquiredEvidence:
    mapping = object_fields(value, what, {"record", "observations", "variants"})
    return AcquiredEvidence(
        record=_acquisition_from_wire(mapping["record"], f"{what}.record"),
        observations=tuple(
            _observation_from_wire(item, f"{what}.observations[{index}]")
            for index, item in enumerate(as_list(mapping["observations"], what))
        ),
        variants=tuple(
            _variant_from_wire(item, f"{what}.variants[{index}]")
            for index, item in enumerate(as_list(mapping["variants"], what))
        ),
    )


def _pool_wire(pool: EvidencePool) -> dict[str, object]:
    return {
        "request_id": pool.request_id,
        "acquisitions": [_acquisition_wire(item) for item in pool.acquisitions],
        "observations": [_observation_wire(item) for item in pool.observations],
        "variants": [item.to_wire() for item in pool.variants],
        "limitations": _diagnostics_wire(pool.limitations),
        "unstable_observation_ids": list(pool.unstable_observation_ids),
        "coverage": _coverage_wire(pool.coverage),
    }


def _pool_from_wire(value: object, what: str) -> EvidencePool:
    mapping = object_fields(value, what, {
            "request_id",
            "acquisitions",
            "observations",
            "variants",
            "limitations",
            "unstable_observation_ids",
            "coverage",
        })
    acquisitions = tuple(
        _acquisition_from_wire(item, f"{what}.acquisitions[{index}]")
        for index, item in enumerate(as_list(mapping["acquisitions"], what))
    )
    observations = tuple(
        _observation_from_wire(item, f"{what}.observations[{index}]")
        for index, item in enumerate(as_list(mapping["observations"], what))
    )
    variants = tuple(
        _variant_from_wire(item, f"{what}.variants[{index}]")
        for index, item in enumerate(as_list(mapping["variants"], what))
    )
    reject_duplicates(
        (item.acquisition_id for item in acquisitions),
        f"{what}.acquisitions",
    )
    reject_duplicates(
        (item.observation_id for item in observations),
        f"{what}.observations",
    )
    reject_duplicates((item.variant_id for item in variants), f"{what}.variants")
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
    unstable = read_strings(
        mapping["unstable_observation_ids"], f"{what}.unstable_observation_ids"
    )
    reject_duplicates(unstable, f"{what}.unstable_observation_ids")
    for observation_id in unstable:
        if observation_id not in observation_ids:
            raise ContractError(
                f"{what}.unstable_observation_ids references unknown observation "
                f"{observation_id!r}"
            )
    return EvidencePool(
        request_id=read_str(mapping["request_id"], f"{what}.request_id"),
        acquisitions=acquisitions,
        observations=observations,
        variants=variants,
        limitations=_diagnostics_from_wire(
            mapping["limitations"], f"{what}.limitations"
        ),
        unstable_observation_ids=unstable,
        coverage=_coverage_from_wire(mapping["coverage"], f"{what}.coverage"),
    )


def _requirement_from_wire(value: object, what: str) -> EvidenceRequirement:
    mapping = object_fields(value, what, {
            "requirement_id",
            "role",
            "rule",
            "strength",
            "description",
            "capabilities",
            "acceptable_kinds",
            "representations",
            "domain",
        })
    return EvidenceRequirement(
        requirement_id=read_str(
            mapping["requirement_id"], f"{what}.requirement_id"
        ),
        role=read_enum(EvidenceRole, mapping["role"], f"{what}.role"),
        rule=read_enum(RequirementRule, mapping["rule"], f"{what}.rule"),
        strength=read_enum(
            RequirementStrength, mapping["strength"], f"{what}.strength"
        ),
        description=read_str(
            mapping["description"], f"{what}.description", allow_empty=True
        ),
        capabilities=tuple(
            read_enum(Capability, item, f"{what}.capabilities[{index}]")
            for index, item in enumerate(as_list(mapping["capabilities"], what))
        ),
        acceptable_kinds=tuple(
            read_enum(ObservationKind, item, f"{what}.acceptable_kinds[{index}]")
            for index, item in enumerate(
                as_list(mapping["acceptable_kinds"], what)
            )
        ),
        representations=tuple(
            read_enum(RepresentationKind, item, f"{what}.representations[{index}]")
            for index, item in enumerate(
                as_list(mapping["representations"], what)
            )
        ),
        domain=optional_str(mapping["domain"], f"{what}.domain"),
    )


def _policy_from_wire(value: object, what: str) -> EvidencePolicy:
    mapping = object_fields(
        value, what, {"profile", "intent", "target_kind", "requirements", "limitations"}
    )
    return EvidencePolicy(
        profile=read_str(mapping["profile"], f"{what}.profile"),
        intent=read_enum(Intent, mapping["intent"], f"{what}.intent"),
        target_kind=read_enum(TargetKind, mapping["target_kind"], f"{what}.target_kind"),
        requirements=tuple(
            _requirement_from_wire(item, f"{what}.requirements[{index}]")
            for index, item in enumerate(as_list(mapping["requirements"], what))
        ),
        limitations=read_strings(mapping["limitations"], f"{what}.limitations"),
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
                    None if item.subject is None else item.subject.to_wire()
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
    mapping = object_fields(
        value, what, {"profile", "request_id", "target", "requests", "omissions"}
    )
    requests: list[CollectionRequest] = []
    for index, item in enumerate(as_list(mapping["requests"], what)):
        request = object_fields(item, f"{what}.requests[{index}]", {
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
            })
        target = request["target"]
        subject = request["subject"]
        requests.append(
            CollectionRequest(
                request_id=read_str(
                    request["request_id"], f"{what}.requests[{index}].request_id"
                ),
                capability=read_enum(
                    Capability,
                    request["capability"],
                    f"{what}.requests[{index}].capability",
                ),
                role=read_enum(
                    EvidenceRole, request["role"], f"{what}.requests[{index}].role"
                ),
                requirement_id=read_str(
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
                scope=read_strings(
                    request["scope"], f"{what}.requests[{index}].scope"
                ),
                domain=optional_str(
                    request["domain"], f"{what}.requests[{index}].domain"
                ),
                limit=read_int(
                    request["limit"], f"{what}.requests[{index}].limit", minimum=1
                ),
                detail=read_str(
                    request["detail"],
                    f"{what}.requests[{index}].detail",
                    allow_empty=True,
                ),
            )
        )
    omissions: list[RequirementOmission] = []
    for index, item in enumerate(as_list(mapping["omissions"], what)):
        omission = object_fields(
            item,
            f"{what}.omissions[{index}]",
            {"requirement_id", "capability", "status", "reason"},
        )
        omissions.append(
            RequirementOmission(
                requirement_id=read_str(
                    omission["requirement_id"],
                    f"{what}.omissions[{index}].requirement_id",
                ),
                capability=read_enum(
                    Capability,
                    omission["capability"],
                    f"{what}.omissions[{index}].capability",
                ),
                status=read_enum(
                    AvailabilityStatus,
                    omission["status"],
                    f"{what}.omissions[{index}].status",
                ),
                reason=read_str(
                    omission["reason"],
                    f"{what}.omissions[{index}].reason",
                    allow_empty=True,
                ),
            )
        )
    return CollectionPlan(
        profile=read_str(mapping["profile"], f"{what}.profile"),
        request_id=read_str(mapping["request_id"], f"{what}.request_id"),
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
            else resolution.declaration.to_wire()
        ),
        "candidate_coverage": _coverage_wire(resolution.candidate_coverage),
        "candidate_evidence": [
            _acquired_wire(item) for item in resolution.candidate_evidence
        ],
    }


def _resolution_from_wire(value: object, what: str) -> ResolvedTarget:
    mapping = object_fields(value, what, {
            "outcome",
            "method",
            "target",
            "declaration",
            "candidate_coverage",
            "candidate_evidence",
        })
    outcome = read_str(mapping["outcome"], f"{what}.outcome")
    if outcome != "resolved":
        raise ContractError(
            f"{what} must be a resolved target for a decision input"
        )
    declaration = mapping["declaration"]
    return ResolvedTarget(
        target=_target_from_wire(mapping["target"], f"{what}.target"),
        method=read_enum(SelectionMethod, mapping["method"], f"{what}.method"),
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
                as_list(mapping["candidate_evidence"], what)
            )
        ),
    )


def _decision_input_wire(decision: DecisionInput) -> dict[str, object]:
    return {
        "request": decision.request.to_wire(),
        "resolution": _resolution_wire(decision.resolution),
        "policy": decision.policy.to_wire(),
        "collection": _collection_wire(decision.collection),
        "pool": _pool_wire(decision.pool),
    }


def _decision_input_from_wire(value: object, what: str) -> DecisionInput:
    mapping = object_fields(
        value, what, {"request", "resolution", "policy", "collection", "pool"}
    )
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


def decision_input_digest(decision: DecisionInput) -> str:
    """Content identity of one decision input, independent of capture metadata."""
    return hashlib.sha256(
        canonical_json(_decision_input_wire(decision)).encode("utf-8")
    ).hexdigest()
