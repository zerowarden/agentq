"""Wire codecs for captures and their provenance.

Snapshots, producers, acquisition limits, capability reports, and the
immutable capture whose canonical bytes define its identity.
"""

from __future__ import annotations

import hashlib

from agentq.core import ContractError, canonical_json
from agentq.inspection.budgeting import AcquisitionLimits
from agentq.inspection.contracts import (
    AvailabilityStatus,
    Capability,
    CapabilityEntry,
    CapabilityReport,
)

from ..models import (
    CAPTURE_SCHEMA,
    FixtureSnapshot,
    ProducerFingerprint,
    ReplayCapture,
    RepositorySnapshot,
    Snapshot,
)
from .contracts import (
    _decision_input_from_wire,
    _decision_input_wire,
    _diagnostics_from_wire,
    _diagnostics_wire,
)
from .json import (
    as_list,
    as_mapping,
    decode_json,
    object_fields,
    optional_str,
    read_enum,
    read_int,
    read_number,
    read_str,
)


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
    mapping = as_mapping(value, what)
    kind = read_str(mapping.get("kind"), f"{what}.kind")
    if kind == "fixture":
        object_fields(
            mapping, what, {"kind", "fixture_id", "fixture_revision", "content_digest"}
        )
        return FixtureSnapshot(
            fixture_id=read_str(mapping["fixture_id"], f"{what}.fixture_id"),
            fixture_revision=read_str(
                mapping["fixture_revision"], f"{what}.fixture_revision"
            ),
            content_digest=read_str(
                mapping["content_digest"], f"{what}.content_digest"
            ),
        )
    if kind == "repository":
        object_fields(
            mapping,
            what,
            {
                "kind",
                "repo_id",
                "commit",
                "tree",
                "source_manifest_digest",
                "configuration_manifest_digest",
            },
        )
        return RepositorySnapshot(
            repo_id=read_str(mapping["repo_id"], f"{what}.repo_id"),
            commit=read_str(mapping["commit"], f"{what}.commit"),
            tree=read_str(mapping["tree"], f"{what}.tree"),
            source_manifest_digest=read_str(
                mapping["source_manifest_digest"],
                f"{what}.source_manifest_digest",
            ),
            configuration_manifest_digest=read_str(
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
    "max_source_line_chars",
    "max_provider_calls",
    "reference_limit",
    "lexical_test_mentions",
)

# Limits introduced after captures were first recorded: a capture taken before
# them falls back to the default that was in force at the time.
_DEFAULTED_LIMIT_FIELDS = frozenset({"max_source_line_chars"})


def _limits_wire(limits: AcquisitionLimits) -> dict[str, object]:
    return {
        **{name: getattr(limits, name) for name in LIMIT_FIELDS},
        "deadline_seconds": limits.deadline_seconds,
    }


def _limits_from_wire(value: object, what: str) -> AcquisitionLimits:
    mapping = object_fields(
        value, what, {*LIMIT_FIELDS, "deadline_seconds"}, optional=_DEFAULTED_LIMIT_FIELDS
    )
    fields: dict[str, int] = {}
    for name in LIMIT_FIELDS:
        if name in mapping:
            fields[name] = read_int(mapping[name], f"{what}.{name}", minimum=1)
        elif name in _DEFAULTED_LIMIT_FIELDS:
            fields[name] = getattr(AcquisitionLimits(), name)
        else:
            raise ContractError(f"{what}.{name} is required")
    return AcquisitionLimits(
        **fields,
        deadline_seconds=read_number(
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
    mapping = object_fields(value, what, {"request_id", "entries"})
    entries: list[CapabilityEntry] = []
    for index, item in enumerate(as_list(mapping["entries"], what)):
        entry = object_fields(item, f"{what}.entries[{index}]", {
                "capability",
                "status",
                "provider",
                "provider_version",
                "reason",
                "diagnostics",
            })
        entries.append(
            CapabilityEntry(
                capability=read_enum(
                    Capability,
                    entry["capability"],
                    f"{what}.entries[{index}].capability",
                ),
                status=read_enum(
                    AvailabilityStatus,
                    entry["status"],
                    f"{what}.entries[{index}].status",
                ),
                provider=optional_str(
                    entry["provider"], f"{what}.entries[{index}].provider"
                ),
                provider_version=optional_str(
                    entry["provider_version"],
                    f"{what}.entries[{index}].provider_version",
                ),
                reason=optional_str(
                    entry["reason"], f"{what}.entries[{index}].reason"
                ),
                diagnostics=_diagnostics_from_wire(
                    entry["diagnostics"],
                    f"{what}.entries[{index}].diagnostics",
                ),
            )
        )
    return CapabilityReport(
        request_id=read_str(mapping["request_id"], f"{what}.request_id"),
        entries=tuple(entries),
    )


def _producer_wire(producer: ProducerFingerprint) -> dict[str, object]:
    return {"provider": producer.provider, "version": producer.version}


def _producer_from_wire(value: object, what: str) -> ProducerFingerprint:
    mapping = object_fields(value, what, {"provider", "version"})
    return ProducerFingerprint(
        provider=read_str(mapping["provider"], f"{what}.provider"),
        version=optional_str(mapping["version"], f"{what}.version"),
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
    mapping = object_fields(value, what, {
            "schema",
            "case_id",
            "snapshot",
            "producers",
            "limits",
            "capability_report",
            "decision",
        })
    schema = read_str(mapping["schema"], f"{what}.schema")
    if schema != CAPTURE_SCHEMA:
        raise ContractError(f"unsupported capture schema: {schema!r}")
    report = mapping["capability_report"]
    return ReplayCapture(
        case_id=read_str(mapping["case_id"], f"{what}.case_id"),
        snapshot=_snapshot_from_wire(mapping["snapshot"], f"{what}.snapshot"),
        producers=tuple(
            _producer_from_wire(item, f"{what}.producers[{index}]")
            for index, item in enumerate(as_list(mapping["producers"], what))
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
