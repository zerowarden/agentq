"""Typed delivery result contracts: rendered output, fragments, receipts.

A delivery receipt describes what was actually written. ``emitted`` is not
``acknowledged``, and a partial or failed transport is never acknowledged.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from agentq.core import (
    ContractError,
    Coverage,
    SourceRef,
    canonical_digest,
    optional_int,
    optional_str,
    require_bool,
    require_int,
    require_sha256,
    require_str,
    require_unique_strings,
    typed_from_wire,
)

RENDER_VERSION = "agentq.render/v1"
RECEIPT_VERSION = "agentq.receipt/v1"


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
