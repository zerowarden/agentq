"""Typed result contracts: provider outcomes, rendered output, delivery.

``ProviderResult`` keeps ``ok``, ``empty``, ``not_applicable``, ``unavailable``
and ``failed`` distinct. A missing payload is never silently complete: an
unavailable or failed provider must carry partial (or weaker) coverage.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Generic, TypeVar

from ..evidence import (
    PROVENANCES,
    Coverage,
    Diagnostic,
    EvidenceRecord,
    SourceRef,
    typed_from_wire,
)
from ._base import (
    ContractError,
    canonical_digest,
    optional_int,
    optional_str,
    reject_unknown_keys,
    require_bool,
    require_int,
    require_mapping,
    require_sha256,
    require_str,
    require_unique_strings,
)

RENDER_VERSION = "agentq.render/v1"
RECEIPT_VERSION = "agentq.receipt/v1"

PayloadT = TypeVar("PayloadT")


class ProviderStatus(str, Enum):
    OK = "ok"
    EMPTY = "empty"
    NOT_APPLICABLE = "not_applicable"
    UNAVAILABLE = "unavailable"
    FAILED = "failed"


@dataclass(frozen=True)
class ProviderResult(Generic[PayloadT]):
    """One provider query: an explicit outcome plus payload, or its absence.

    ``provenance`` and ``candidate_count`` are the provider's own declarations,
    carried on the result so orchestrators never re-derive them from a provider
    name or by guessing a payload key.
    """

    provider: str
    status: ProviderStatus
    payload: PayloadT | None = None
    provider_version: str | None = None
    provenance: str | None = None
    candidate_count: int | None = None
    evidence: tuple[EvidenceRecord, ...] = ()
    coverage: Coverage = field(default_factory=Coverage)
    diagnostics: tuple[Diagnostic, ...] = ()
    source_snapshot: str | None = None

    def __post_init__(self) -> None:
        require_str(self.provider, "provider name")
        if not isinstance(self.status, ProviderStatus):
            raise ContractError("provider status must be a ProviderStatus")
        optional_str(self.provider_version, "provider version")
        if self.provenance is not None and self.provenance not in PROVENANCES:
            raise ContractError(f"unsupported provider provenance: {self.provenance!r}")
        optional_int(self.candidate_count, "provider candidate count", minimum=0)
        optional_str(self.source_snapshot, "provider source snapshot")
        if not isinstance(self.coverage, Coverage):
            object.__setattr__(self, "coverage", typed_from_wire(self.coverage))
        if not isinstance(self.evidence, tuple) or not all(
            isinstance(item, EvidenceRecord) for item in self.evidence
        ):
            raise ContractError("provider evidence must be a tuple of EvidenceRecord")
        if not isinstance(self.diagnostics, tuple) or not all(
            isinstance(item, Diagnostic) for item in self.diagnostics
        ):
            raise ContractError("provider diagnostics must be a tuple of Diagnostic")
        if self.status is ProviderStatus.OK and self.payload is None:
            raise ContractError("ok provider result must carry a payload")
        if self.status in {
            ProviderStatus.NOT_APPLICABLE,
            ProviderStatus.UNAVAILABLE,
            ProviderStatus.FAILED,
        }:
            if self.payload is not None:
                raise ContractError(
                    f"{self.status.value} provider result must not carry a payload"
                )
        if (
            self.status in {ProviderStatus.UNAVAILABLE, ProviderStatus.FAILED}
            and self.coverage.is_complete()
        ):
            raise ContractError(
                f"{self.status.value} provider result cannot claim complete coverage"
            )

    @property
    def result(self) -> Any:
        """Compatibility accessor for callers that read the payload as a result."""
        return self.payload

    def is_deliverable(self) -> bool:
        return self.status in {ProviderStatus.OK, ProviderStatus.EMPTY}

    def to_wire(self) -> dict[str, Any]:
        return {
            "provider": self.provider,
            "status": self.status.value,
            "payload": self.payload,
            "provider_version": self.provider_version,
            "provenance": self.provenance,
            "candidate_count": self.candidate_count,
            "evidence": [item.to_wire() for item in self.evidence],
            "coverage": self.coverage.to_wire(),
            "diagnostics": [item.to_wire() for item in self.diagnostics],
            "source_snapshot": self.source_snapshot,
        }

    @classmethod
    def from_wire(cls, value: Any, *, what: str = "provider result") -> ProviderResult:
        payload = require_mapping(value, what)
        reject_unknown_keys(
            payload,
            (
                "provider",
                "status",
                "payload",
                "provider_version",
                "provenance",
                "candidate_count",
                "evidence",
                "coverage",
                "diagnostics",
                "source_snapshot",
            ),
            what,
        )
        status_text = require_str(payload.get("status"), f"{what}.status")
        try:
            status = ProviderStatus(status_text)
        except ValueError as exc:
            raise ContractError(
                f"{what}.status is not a provider outcome: {status_text!r}"
            ) from exc
        diagnostics = payload.get("diagnostics") or []
        if not isinstance(diagnostics, list):
            raise ContractError(f"{what}.diagnostics must be an array")
        evidence_entries = payload.get("evidence") or []
        if not isinstance(evidence_entries, list):
            raise ContractError(f"{what}.evidence must be an array")
        return cls(
            provider=require_str(payload.get("provider"), f"{what}.provider"),
            status=status,
            payload=payload.get("payload"),
            provider_version=optional_str(
                payload.get("provider_version"), f"{what}.provider_version"
            ),
            provenance=optional_str(payload.get("provenance"), f"{what}.provenance"),
            candidate_count=optional_int(
                payload.get("candidate_count"), f"{what}.candidate_count", minimum=0
            ),
            evidence=tuple(
                EvidenceRecord.from_wire(item, what=f"{what}.evidence[{index}]")
                for index, item in enumerate(evidence_entries)
            ),
            coverage=typed_from_wire(payload.get("coverage")),
            diagnostics=tuple(
                _diagnostic_from_wire(item, what) for item in diagnostics
            ),
            source_snapshot=optional_str(
                payload.get("source_snapshot"), f"{what}.source_snapshot"
            ),
        )


def ok_result(
    provider: str,
    payload: Any,
    *,
    coverage: Any = None,
    version: str | None = None,
    provenance: str | None = None,
) -> ProviderResult:
    return ProviderResult(
        provider=provider,
        status=ProviderStatus.OK,
        payload=payload,
        provider_version=version,
        provenance=provenance,
        coverage=(
            typed_from_wire(coverage)
            if coverage is not None
            else typed_from_wire("complete")
        ),
    )


def empty_result(
    provider: str,
    *,
    coverage: Any = None,
    payload: Any = None,
    version: str | None = None,
    provenance: str | None = None,
) -> ProviderResult:
    return ProviderResult(
        provider=provider,
        status=ProviderStatus.EMPTY,
        payload=payload,
        provider_version=version,
        provenance=provenance,
        coverage=(
            typed_from_wire(coverage)
            if coverage is not None
            else typed_from_wire("complete")
        ),
    )


def not_applicable_result(
    provider: str,
    *,
    version: str | None = None,
    provenance: str | None = None,
) -> ProviderResult:
    return ProviderResult(
        provider=provider,
        status=ProviderStatus.NOT_APPLICABLE,
        provider_version=version,
        provenance=provenance,
    )


def unavailable_result(
    provider: str,
    reason: str,
    *,
    version: str | None = None,
    provenance: str | None = None,
) -> ProviderResult:
    coverage = typed_from_wire(
        {"status": "partial", "reason": ["provider_unavailable"]}
    )
    return ProviderResult(
        provider=provider,
        status=ProviderStatus.UNAVAILABLE,
        provider_version=version,
        provenance=provenance,
        coverage=coverage,
        diagnostics=(
            Diagnostic(message=reason, code="provider_unavailable", severity="error"),
        ),
    )


def failed_result(
    provider: str,
    reason: str,
    *,
    version: str | None = None,
    provenance: str | None = None,
) -> ProviderResult:
    coverage = typed_from_wire({"status": "partial", "reason": ["provider_error"]})
    return ProviderResult(
        provider=provider,
        status=ProviderStatus.FAILED,
        provider_version=version,
        provenance=provenance,
        coverage=coverage,
        diagnostics=(
            Diagnostic(message=reason, code="provider_error", severity="error"),
        ),
    )


def _diagnostic_from_wire(value: Any, what: str) -> Diagnostic:
    payload = require_mapping(value, f"{what}.diagnostics entry")
    reject_unknown_keys(
        payload, ("message", "code", "path", "severity"), f"{what}.diagnostics entry"
    )
    return Diagnostic(
        message=require_str(payload.get("message"), f"{what}.diagnostics message"),
        code=require_str(
            payload.get("code", "provider_error"), f"{what}.diagnostics code"
        ),
        path=optional_str(payload.get("path"), f"{what}.diagnostics path"),
        severity=require_str(
            payload.get("severity", "error"), f"{what}.diagnostics severity"
        ),
    )


@dataclass(frozen=True)
class EvidenceFragment:
    """A piece of evidence actually present in final output, including variant."""

    evidence_id: str
    kind: str
    source: SourceRef = field(default_factory=SourceRef)
    source_version: str | None = None
    variant: str = "full"
    rendered_chars: int = 0
    redacted: bool = False

    def __post_init__(self) -> None:
        require_str(self.evidence_id, "fragment evidence id")
        require_str(self.kind, "fragment kind")
        require_str(self.variant, "fragment variant")
        require_int(self.rendered_chars, "fragment rendered chars", minimum=0)
        require_bool(self.redacted, "fragment redacted")
        if not isinstance(self.source, SourceRef):
            raise ContractError("fragment source must be a SourceRef")

    def identity(self) -> str:
        return canonical_digest(
            {
                "id": self.evidence_id,
                "variant": self.variant,
                "version": self.source_version,
                "chars": self.rendered_chars,
                "redacted": self.redacted,
                "source": self.source.to_wire(),
            }
        )

    def to_wire(self) -> dict[str, Any]:
        return {
            "evidence_id": self.evidence_id,
            "kind": self.kind,
            "source": self.source.to_wire(),
            "source_version": self.source_version,
            "variant": self.variant,
            "rendered_chars": self.rendered_chars,
            "redacted": self.redacted,
        }


@dataclass(frozen=True)
class OutputBlock:
    """Typed metadata for one rendered block; fragments are what was visible."""

    block_id: str
    kind: str
    fragments: tuple[EvidenceFragment, ...] = ()
    variant: str = "full"
    visible: bool = True
    coverage: Coverage = field(default_factory=Coverage)

    def __post_init__(self) -> None:
        require_str(self.block_id, "block id")
        require_str(self.kind, "block kind")
        require_str(self.variant, "block variant")
        require_bool(self.visible, "block visible")
        if not isinstance(self.coverage, Coverage):
            object.__setattr__(self, "coverage", typed_from_wire(self.coverage))

    def to_wire(self) -> dict[str, Any]:
        return {
            "block_id": self.block_id,
            "kind": self.kind,
            "fragments": [item.to_wire() for item in self.fragments],
            "variant": self.variant,
            "visible": self.visible,
            "coverage": self.coverage.to_wire(),
        }


@dataclass(frozen=True)
class RenderResult:
    """The final serialized output: fragments present, omissions, visible coverage."""

    final_output: str
    fragments: tuple[EvidenceFragment, ...] = ()
    blocks: tuple[OutputBlock, ...] = ()
    omitted_evidence_ids: tuple[str, ...] = ()
    omitted_count: int = 0
    visible_coverage: Coverage = field(default_factory=Coverage)
    terminal_reason: str | None = None
    encoding: str = "utf-8"
    prebudget_chars: int | None = None
    truncated: bool = False
    rendering_version: str = RENDER_VERSION

    def __post_init__(self) -> None:
        require_str(self.final_output, "final output", allow_empty=True)
        require_str(self.encoding, "render encoding")
        require_int(self.omitted_count, "render omitted count", minimum=0)
        require_bool(self.truncated, "render truncated")
        optional_int(self.prebudget_chars, "render prebudget chars", minimum=0)
        optional_str(self.terminal_reason, "render terminal reason")
        if not isinstance(self.visible_coverage, Coverage):
            object.__setattr__(
                self, "visible_coverage", typed_from_wire(self.visible_coverage)
            )
        require_unique_strings(self.omitted_evidence_ids, "render omitted evidence ids")
        if (
            self.prebudget_chars is not None
            and self.prebudget_chars < self.visible_chars
        ):
            raise ContractError(
                "render prebudget size cannot be smaller than visible size"
            )
        if self.omitted_count and not self.truncated:
            raise ContractError("render omissions require truncated=true")
        if sum(item.rendered_chars for item in self.fragments) > self.visible_chars:
            raise ContractError("rendered fragments cannot exceed the visible output")

    @property
    def visible_chars(self) -> int:
        return len(self.final_output)

    @property
    def rendered_bytes(self) -> int:
        return len(self.final_output.encode(self.encoding))

    def digest(self) -> str:
        return hashlib.sha256(self.final_output.encode(self.encoding)).hexdigest()

    def to_wire(self) -> dict[str, Any]:
        return {
            "rendering_version": self.rendering_version,
            "fragments": [item.to_wire() for item in self.fragments],
            "blocks": [item.to_wire() for item in self.blocks],
            "omitted_evidence_ids": list(self.omitted_evidence_ids),
            "omitted_count": self.omitted_count,
            "visible_coverage": self.visible_coverage.to_wire(),
            "terminal_reason": self.terminal_reason,
            "encoding": self.encoding,
            "prebudget_chars": self.prebudget_chars,
            "visible_chars": self.visible_chars,
            "rendered_bytes": self.rendered_bytes,
            "truncated": self.truncated,
        }


class TransportStatus(str, Enum):
    EMITTED = "emitted"
    PARTIAL = "partial"
    FAILED = "failed"


class AcknowledgmentStatus(str, Enum):
    UNACKNOWLEDGED = "unacknowledged"
    ACKNOWLEDGED = "acknowledged"
    NOT_APPLICABLE = "not_applicable"


@dataclass(frozen=True)
class DeliveryReceipt:
    """What was actually written for one request; ``emitted`` != ``acknowledged``."""

    receipt_id: str
    request_id: str
    repo_id: str
    output_digest: str
    written_bytes: int
    transport_status: TransportStatus
    acknowledgment_status: AcknowledgmentStatus
    emitted_at: str
    context_id: str | None = None
    consumer_id: str | None = None
    fragments: tuple[EvidenceFragment, ...] = ()
    receipt_version: str = RECEIPT_VERSION

    def __post_init__(self) -> None:
        require_str(self.receipt_id, "receipt id")
        require_str(self.request_id, "receipt request id")
        require_str(self.repo_id, "receipt repo id")
        require_sha256(self.output_digest, "receipt output digest")
        require_int(self.written_bytes, "receipt written bytes", minimum=0)
        require_str(self.emitted_at, "receipt timestamp")
        if not isinstance(self.transport_status, TransportStatus):
            raise ContractError("receipt transport status must be a TransportStatus")
        if not isinstance(self.acknowledgment_status, AcknowledgmentStatus):
            raise ContractError(
                "receipt acknowledgment status must be an AcknowledgmentStatus"
            )
        if (
            self.transport_status is not TransportStatus.EMITTED
            and self.acknowledgment_status is AcknowledgmentStatus.ACKNOWLEDGED
        ):
            raise ContractError("a partial or failed transport cannot be acknowledged")

    def is_delivered(self) -> bool:
        return self.transport_status is TransportStatus.EMITTED

    def to_wire(self) -> dict[str, Any]:
        return {
            "receipt_version": self.receipt_version,
            "receipt_id": self.receipt_id,
            "request_id": self.request_id,
            "repo_id": self.repo_id,
            "context_id": self.context_id,
            "consumer_id": self.consumer_id,
            "output_digest": self.output_digest,
            "written_bytes": self.written_bytes,
            "fragments": [item.to_wire() for item in self.fragments],
            "transport_status": self.transport_status.value,
            "acknowledgment_status": self.acknowledgment_status.value,
            "emitted_at": self.emitted_at,
        }


def receipt_identity(
    request_id: str, output_digest: str, consumer_id: str | None
) -> str:
    """Deterministic receipt id so repeated emission inserts idempotently."""
    return canonical_digest(
        {"request_id": request_id, "digest": output_digest, "consumer": consumer_id},
        length=32,
    )


def build_receipt(
    render: RenderResult,
    *,
    request_id: str,
    repo_id: str,
    emitted_at: str,
    transport_status: TransportStatus = TransportStatus.EMITTED,
    acknowledgment_status: AcknowledgmentStatus = AcknowledgmentStatus.UNACKNOWLEDGED,
    context_id: str | None = None,
    consumer_id: str | None = None,
) -> DeliveryReceipt:
    digest = render.digest()
    return DeliveryReceipt(
        receipt_id=receipt_identity(request_id, digest, consumer_id),
        request_id=request_id,
        repo_id=repo_id,
        context_id=context_id,
        consumer_id=consumer_id,
        output_digest=digest,
        written_bytes=render.rendered_bytes,
        fragments=render.fragments,
        transport_status=transport_status,
        acknowledgment_status=acknowledgment_status,
        emitted_at=emitted_at,
    )
