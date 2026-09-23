"""Observations, their representations, and the acquired evidence pool.

An :class:`Observation` here is an acquired repository fact at a versioned
source location. It is unrelated to telemetry's invocation observation: this
one carries source provenance and is never persisted by this package.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import TYPE_CHECKING

from agentq.core import (
    ContractError,
    Coverage,
    Diagnostic,
    SourceRef,
    canonical_digest,
    is_instance_of,
    optional_str,
    require_bool,
    require_relative_posix,
    require_str,
    typed_from_wire,
)

from .targets import SourceSpan

if TYPE_CHECKING:
    from .capability import AcquisitionRecord, Capability


class ObservationKind(str, Enum):
    DECLARATION = "declaration"
    SEMANTIC_REFERENCE = "semantic_reference"
    SYNTACTIC_MENTION = "syntactic_mention"
    LEXICAL_MENTION = "lexical_mention"
    TEST_MENTION = "test_mention"
    IMPLEMENTATION = "implementation"
    SOURCE_WINDOW = "source_window"
    OUTLINE = "outline"
    OWNING_PACKAGE = "owning_package"


class RepresentationKind(str, Enum):
    EXACT_SOURCE = "exact_source"
    SIGNATURE = "signature"
    EXCERPT = "excerpt"
    OUTLINE = "outline"
    REFERENCE = "reference"
    PACKAGE = "package"


class Fidelity(str, Enum):
    """How much of the underlying observation a representation preserves."""

    EXACT = "exact"
    BOUNDED = "bounded"
    SUMMARY = "summary"


class Binding(str, Enum):
    RESOLVED = "resolved"
    UNRESOLVED = "unresolved"
    UNKNOWN = "unknown"


# ---------------------------------------------------------------------------
# Observations, representations, and pools
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SourceVersion:
    """A version stamp for one repository path or configuration file."""

    path: str
    version: str
    method: str = "content_sha256"

    def __post_init__(self) -> None:
        require_relative_posix(self.path, "source version path", allow_root=True)
        require_str(self.version, "source version value")
        require_str(self.method, "source version method")

    def to_wire(self) -> dict[str, object]:
        return {"path": self.path, "version": self.version, "method": self.method}


@dataclass(frozen=True)
class DeclarationPayload:
    name: str
    kind: str
    signature: str
    span: SourceSpan
    scope: str | None = None

    def __post_init__(self) -> None:
        require_str(self.name, "declaration payload name", allow_empty=True)
        require_str(self.kind, "declaration payload kind")
        require_str(self.signature, "declaration payload signature", allow_empty=True)
        if not isinstance(self.span, SourceSpan):
            raise ContractError("declaration payload span must be a SourceSpan")
        optional_str(self.scope, "declaration payload scope")

    def to_wire(self) -> dict[str, object]:
        return {
            "name": self.name,
            "kind": self.kind,
            "signature": self.signature,
            "span": self.span.to_wire(),
            "scope": self.scope,
        }


@dataclass(frozen=True)
class ReferencePayload:
    relationship: str
    text: str
    binding: Binding = Binding.UNKNOWN
    domain: str | None = None
    configuration: str | None = None

    def __post_init__(self) -> None:
        require_str(self.relationship, "reference payload relationship")
        require_str(self.text, "reference payload text", allow_empty=True)
        if not isinstance(self.binding, Binding):
            raise ContractError("reference payload binding must be a Binding")
        optional_str(self.domain, "reference payload domain")
        optional_str(self.configuration, "reference payload configuration")

    def to_wire(self) -> dict[str, object]:
        return {
            "relationship": self.relationship,
            "text": self.text,
            "binding": self.binding.value,
            "domain": self.domain,
            "configuration": self.configuration,
        }


@dataclass(frozen=True)
class SourceWindowPayload:
    text: str
    span: SourceSpan
    truncated: bool = False

    def __post_init__(self) -> None:
        require_str(self.text, "source window text", allow_empty=True)
        if not isinstance(self.span, SourceSpan):
            raise ContractError("source window span must be a SourceSpan")
        require_bool(self.truncated, "source window truncated")

    def to_wire(self) -> dict[str, object]:
        return {
            "text": self.text,
            "span": self.span.to_wire(),
            "truncated": self.truncated,
        }


@dataclass(frozen=True)
class OutlineSymbolRef:
    name: str
    kind: str
    span: SourceSpan

    def __post_init__(self) -> None:
        require_str(self.name, "outline symbol name", allow_empty=True)
        require_str(self.kind, "outline symbol kind")
        if not isinstance(self.span, SourceSpan):
            raise ContractError("outline symbol span must be a SourceSpan")

    def to_wire(self) -> dict[str, object]:
        return {"name": self.name, "kind": self.kind, "span": self.span.to_wire()}


@dataclass(frozen=True)
class OutlinePayload:
    symbols: tuple[OutlineSymbolRef, ...] = ()
    truncated: bool = False

    def __post_init__(self) -> None:
        if not is_instance_of(self.symbols, tuple) or not all(
            is_instance_of(item, OutlineSymbolRef) for item in self.symbols
        ):
            raise ContractError(
                "outline payload symbols must be a tuple of OutlineSymbolRef"
            )
        require_bool(self.truncated, "outline payload truncated")

    def to_wire(self) -> dict[str, object]:
        return {
            "symbols": [item.to_wire() for item in self.symbols],
            "truncated": self.truncated,
        }


@dataclass(frozen=True)
class PackagePayload:
    path: str
    kind: str
    name: str | None = None
    scripts: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        require_relative_posix(self.path, "package payload path", allow_root=True)
        require_str(self.kind, "package payload kind")
        optional_str(self.name, "package payload name")
        if not is_instance_of(self.scripts, tuple) or not all(
            is_instance_of(item, str) for item in self.scripts
        ):
            raise ContractError("package payload scripts must be a tuple of strings")

    def to_wire(self) -> dict[str, object]:
        return {
            "path": self.path,
            "kind": self.kind,
            "name": self.name,
            "scripts": list(self.scripts),
        }


@dataclass(frozen=True)
class MentionPayload:
    text: str
    domain: str | None = None

    def __post_init__(self) -> None:
        require_str(self.text, "mention payload text", allow_empty=True)
        optional_str(self.domain, "mention payload domain")

    def to_wire(self) -> dict[str, object]:
        return {"text": self.text, "domain": self.domain}


ObservationPayload = (
    DeclarationPayload
    | ReferencePayload
    | SourceWindowPayload
    | OutlinePayload
    | PackagePayload
    | MentionPayload
)
PAYLOAD_TYPES = (
    DeclarationPayload,
    ReferencePayload,
    SourceWindowPayload,
    OutlinePayload,
    PackagePayload,
    MentionPayload,
)


def payload_to_wire(payload: ObservationPayload) -> dict[str, object]:
    return payload.to_wire()


@dataclass(frozen=True)
class Observation:
    """One discovered fact: typed payload, source reference, and versions.

    An observation is not a representation. A declaration observation may have
    several renderable representations; those are :class:`EvidenceVariant`
    records and must not be treated as independent discoveries.
    """

    observation_id: str
    kind: ObservationKind
    payload: ObservationPayload
    source: SourceRef
    source_versions: tuple[SourceVersion, ...] = ()
    acquisition_id: str = ""

    def __post_init__(self) -> None:
        require_str(self.observation_id, "observation id")
        if not isinstance(self.kind, ObservationKind):
            raise ContractError("observation requires an ObservationKind")
        if not isinstance(self.payload, PAYLOAD_TYPES):
            raise ContractError("observation payload must be a typed payload record")
        if not isinstance(self.source, SourceRef):
            raise ContractError("observation source must be a SourceRef")
        if not is_instance_of(self.source_versions, tuple) or not all(
            is_instance_of(item, SourceVersion) for item in self.source_versions
        ):
            raise ContractError(
                "observation source_versions must be a tuple of SourceVersion"
            )
        require_str(self.acquisition_id, "observation acquisition id", allow_empty=True)

    def version_of(self, path: str) -> str | None:
        for stamp in self.source_versions:
            if stamp.path == path:
                return stamp.version
        return None

    def to_wire(self) -> dict[str, object]:
        return {
            "observation_id": self.observation_id,
            "kind": self.kind.value,
            "payload": payload_to_wire(self.payload),
            "source": self.source.to_wire(),
            "source_versions": [item.to_wire() for item in self.source_versions],
            "acquisition_id": self.acquisition_id,
        }


@dataclass(frozen=True)
class EvidenceVariant:
    """One renderable representation of an observation."""

    variant_id: str
    observation_id: str
    representation: RepresentationKind
    fidelity: Fidelity
    source: SourceRef
    text: str
    span: SourceSpan | None = None

    def __post_init__(self) -> None:
        require_str(self.variant_id, "evidence variant id")
        require_str(self.observation_id, "evidence variant observation id")
        if not isinstance(self.representation, RepresentationKind):
            raise ContractError(
                "evidence variant representation must be a RepresentationKind"
            )
        if not isinstance(self.fidelity, Fidelity):
            raise ContractError("evidence variant fidelity must be a Fidelity")
        if not isinstance(self.source, SourceRef):
            raise ContractError("evidence variant source must be a SourceRef")
        require_str(self.text, "evidence variant text", allow_empty=True)
        if self.span is not None and not isinstance(self.span, SourceSpan):
            raise ContractError("evidence variant span must be a SourceSpan")

    def to_wire(self) -> dict[str, object]:
        return {
            "variant_id": self.variant_id,
            "observation_id": self.observation_id,
            "representation": self.representation.value,
            "fidelity": self.fidelity.value,
            "source": self.source.to_wire(),
            "span": self.span.to_wire() if self.span is not None else None,
            "text": self.text,
        }


@dataclass(frozen=True)
class EvidencePool:
    """Acquired observations and their acquisition provenance.

    ``unstable_observation_ids`` records observations whose source changed
    during the inspection; they are reported, never silently dropped.
    """

    request_id: str
    acquisitions: tuple[AcquisitionRecord, ...] = ()
    observations: tuple[Observation, ...] = ()
    variants: tuple[EvidenceVariant, ...] = ()
    limitations: tuple[Diagnostic, ...] = ()
    unstable_observation_ids: tuple[str, ...] = ()
    coverage: Coverage = field(default_factory=Coverage)

    def __post_init__(self) -> None:
        require_str(self.request_id, "evidence pool request id")
        if not isinstance(self.coverage, Coverage):
            object.__setattr__(self, "coverage", typed_from_wire(self.coverage))
        for name, values in (
            ("acquisitions", self.acquisitions),
            ("observations", self.observations),
            ("variants", self.variants),
        ):
            if not is_instance_of(values, tuple):
                raise ContractError(f"evidence pool {name} must be a tuple")
        if not all(is_instance_of(item, Diagnostic) for item in self.limitations):
            raise ContractError("evidence pool limitations must be Diagnostics")

    def observation(self, observation_id: str) -> Observation | None:
        for item in self.observations:
            if item.observation_id == observation_id:
                return item
        return None

    def variants_for(self, observation_id: str) -> tuple[EvidenceVariant, ...]:
        return tuple(
            item for item in self.variants if item.observation_id == observation_id
        )

    def acquisitions_for(self, capability: Capability) -> tuple[AcquisitionRecord, ...]:
        return tuple(
            item for item in self.acquisitions if item.capability is capability
        )


def make_observation(
    *,
    kind: ObservationKind,
    payload: ObservationPayload,
    source: SourceRef,
    source_versions: tuple[SourceVersion, ...] = (),
    acquisition_id: str = "",
) -> Observation:
    """Build an observation with a deterministic content identity."""
    observation_id = "obs-" + canonical_digest(
        {
            "kind": kind.value,
            "payload": payload_to_wire(payload),
            "source": source.to_wire(),
            "versions": [item.to_wire() for item in source_versions],
        },
        length=20,
    )
    return Observation(
        observation_id=observation_id,
        kind=kind,
        payload=payload,
        source=source,
        source_versions=source_versions,
        acquisition_id=acquisition_id,
    )


def make_variant(
    *,
    observation_id: str,
    representation: RepresentationKind,
    fidelity: Fidelity,
    source: SourceRef,
    text: str,
    span: SourceSpan | None = None,
) -> EvidenceVariant:
    """Build a representation with a deterministic identity."""
    variant_id = "var-" + canonical_digest(
        {
            "observation_id": observation_id,
            "representation": representation.value,
            "fidelity": fidelity.value,
            "source": source.to_wire(),
            "span": span.to_wire() if span is not None else None,
            "text": text,
        },
        length=20,
    )
    return EvidenceVariant(
        variant_id=variant_id,
        observation_id=observation_id,
        representation=representation,
        fidelity=fidelity,
        source=source,
        text=text,
        span=span,
    )
