"""Capability vocabulary, interfaces, and normalized acquisition results."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import TYPE_CHECKING, Protocol, runtime_checkable

from agentq.core import (
    ContractError,
    Coverage,
    Diagnostic,
    is_instance_of,
    optional_str,
    require_bool,
    require_int,
    require_str,
    typed_from_wire,
)

from ._common import validate_scopes
from .evidence import EvidenceVariant, Observation
from .resolution import DeclarationCandidate
from .targets import TARGET_TYPES, InspectionTarget, describe_target

if TYPE_CHECKING:
    from .requests import InspectionContext, InspectionRequest


class Capability(str, Enum):
    """Operations a concrete adapter can provide; never language names."""

    FIND_DECLARATIONS = "find_declarations"
    RESOLVE_LOCATION = "resolve_location"
    READ_SOURCE = "read_source"
    OUTLINE = "outline"
    SEMANTIC_REFERENCES = "semantic_references"
    SYNTACTIC_MENTIONS = "syntactic_mentions"
    LEXICAL_MENTIONS = "lexical_mentions"
    IMPLEMENTATIONS = "implementations"
    OWNING_PACKAGE = "owning_package"


class AvailabilityStatus(str, Enum):
    """Whether an operation can run now, separately from what it returned."""

    AVAILABLE = "available"
    UNAVAILABLE = "unavailable"
    NOT_APPLICABLE = "not_applicable"
    UNSUPPORTED = "unsupported"


class CollectionStatus(str, Enum):
    """The explicit outcome of one bounded acquisition."""

    COMPLETED = "completed"
    EMPTY = "empty"
    PARTIAL = "partial"
    UNAVAILABLE = "unavailable"
    FAILED = "failed"


# ---------------------------------------------------------------------------
# Capability interfaces and reports
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CapabilityAvailability:
    """One adapter's declaration about one capability for this request."""

    available: bool
    reason: str | None = None
    provider_version: str | None = None
    diagnostics: tuple[Diagnostic, ...] = ()

    def __post_init__(self) -> None:
        require_bool(self.available, "capability availability")
        optional_str(self.reason, "capability availability reason")
        optional_str(self.provider_version, "capability availability provider_version")
        if not is_instance_of(self.diagnostics, tuple) or not all(
            is_instance_of(item, Diagnostic) for item in self.diagnostics
        ):
            raise ContractError(
                "capability availability diagnostics must be a tuple of Diagnostic"
            )


@dataclass(frozen=True)
class CapabilityEntry:
    """One (capability, adapter) applicability fact for one request."""

    capability: Capability
    status: AvailabilityStatus
    provider: str | None = None
    provider_version: str | None = None
    reason: str | None = None
    diagnostics: tuple[Diagnostic, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.capability, Capability):
            raise ContractError("capability entry requires a Capability")
        if not isinstance(self.status, AvailabilityStatus):
            raise ContractError("capability entry requires an AvailabilityStatus")
        optional_str(self.provider, "capability entry provider")
        optional_str(self.provider_version, "capability entry provider_version")
        optional_str(self.reason, "capability entry reason")
        if not is_instance_of(self.diagnostics, tuple) or not all(
            is_instance_of(item, Diagnostic) for item in self.diagnostics
        ):
            raise ContractError(
                "capability entry diagnostics must be a tuple of Diagnostic"
            )

    def to_wire(self) -> dict[str, object]:
        return {
            "capability": self.capability.value,
            "status": self.status.value,
            "provider": self.provider,
            "provider_version": self.provider_version,
            "reason": self.reason,
            "diagnostics": [item.to_wire() for item in self.diagnostics],
        }


@dataclass(frozen=True)
class CapabilityGap:
    """Why a capability is not available for this request."""

    capability: Capability
    status: AvailabilityStatus
    reason: str | None = None
    requirement_ids: tuple[str, ...] = ()

    def to_wire(self) -> dict[str, object]:
        return {
            "capability": self.capability.value,
            "status": self.status.value,
            "reason": self.reason,
            "requirement_ids": list(self.requirement_ids),
        }


@dataclass(frozen=True)
class CapabilityReport:
    """Applicability and availability of every capability for one request."""

    request_id: str
    entries: tuple[CapabilityEntry, ...] = ()

    def __post_init__(self) -> None:
        require_str(self.request_id, "capability report request id")
        if not is_instance_of(self.entries, tuple) or not all(
            is_instance_of(item, CapabilityEntry) for item in self.entries
        ):
            raise ContractError("capability report entries must be CapabilityEntry")

    def entries_for(self, capability: Capability) -> tuple[CapabilityEntry, ...]:
        return tuple(entry for entry in self.entries if entry.capability is capability)

    def available(self, capability: Capability) -> bool:
        return any(
            entry.status is AvailabilityStatus.AVAILABLE
            for entry in self.entries_for(capability)
        )

    def gap_for(self, capability: Capability) -> CapabilityGap | None:
        if self.available(capability):
            return None
        entries = self.entries_for(capability)
        if not entries:
            return CapabilityGap(
                capability=capability,
                status=AvailabilityStatus.UNSUPPORTED,
                reason="no registered adapter implements this capability",
            )
        entry = entries[0]
        reason = entry.reason or f"{entry.status.value} for this request"
        return CapabilityGap(capability=capability, status=entry.status, reason=reason)

    def gaps(self, *capabilities: Capability) -> tuple[CapabilityGap, ...]:
        candidates = capabilities or tuple(Capability)
        return tuple(
            gap
            for capability in candidates
            if (gap := self.gap_for(capability)) is not None
        )

    def available_providers(self) -> tuple[str, ...]:
        return tuple(
            sorted(
                {
                    entry.provider
                    for entry in self.entries
                    if entry.status is AvailabilityStatus.AVAILABLE
                    and entry.provider is not None
                }
            )
        )


@dataclass(frozen=True)
class CapabilityResult:
    """One adapter's normalized output for one capability request."""

    status: CollectionStatus
    observations: tuple[Observation, ...] = ()
    variants: tuple[EvidenceVariant, ...] = ()
    coverage: Coverage = field(default_factory=Coverage)
    diagnostics: tuple[Diagnostic, ...] = ()
    provider_version: str | None = None
    effective_scope: tuple[str, ...] | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.status, CollectionStatus):
            raise ContractError("capability result requires a CollectionStatus")
        if not isinstance(self.coverage, Coverage):
            object.__setattr__(self, "coverage", typed_from_wire(self.coverage))
        _validate_result_shapes(self)
        _validate_result_outcome(self)
        optional_str(self.provider_version, "capability result provider_version")
        if self.effective_scope is not None:
            validate_scopes(self.effective_scope, "capability result effective_scope")


def _validate_result_shapes(result: CapabilityResult) -> None:
    if not is_instance_of(result.observations, tuple) or not all(
        is_instance_of(item, Observation) for item in result.observations
    ):
        raise ContractError(
            "capability result observations must be a tuple of Observation"
        )
    if not is_instance_of(result.variants, tuple) or not all(
        is_instance_of(item, EvidenceVariant) for item in result.variants
    ):
        raise ContractError(
            "capability result variants must be a tuple of EvidenceVariant"
        )


def _validate_result_outcome(result: CapabilityResult) -> None:
    empty_like = {
        CollectionStatus.EMPTY,
        CollectionStatus.UNAVAILABLE,
        CollectionStatus.FAILED,
    }
    if result.status in empty_like and result.observations:
        raise ContractError(
            f"{result.status.value} capability result must not carry observations"
        )
    if result.status is CollectionStatus.EMPTY and result.variants:
        raise ContractError("empty capability result must not carry variants")
    if (
        result.status in {CollectionStatus.UNAVAILABLE, CollectionStatus.FAILED}
        and result.coverage.is_complete()
    ):
        raise ContractError(
            f"{result.status.value} capability result cannot claim complete coverage"
        )
    observed = {item.observation_id for item in result.observations}
    for variant in result.variants:
        if variant.observation_id not in observed:
            raise ContractError(
                "capability result variants must reference a returned observation"
            )


@dataclass(frozen=True)
class AcquisitionRecord:
    """What one bounded acquisition was, over what scope, with what outcome."""

    acquisition_id: str
    capability: Capability
    provider: str
    provider_version: str | None
    method: str
    effective_scope: tuple[str, ...]
    coverage: Coverage
    status: CollectionStatus = CollectionStatus.COMPLETED
    diagnostics: tuple[Diagnostic, ...] = ()
    observed_inputs: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        require_str(self.acquisition_id, "acquisition record id")
        if not isinstance(self.capability, Capability):
            raise ContractError("acquisition record requires a Capability")
        require_str(self.provider, "acquisition record provider")
        require_str(self.method, "acquisition record method")
        if not isinstance(self.coverage, Coverage):
            object.__setattr__(self, "coverage", typed_from_wire(self.coverage))
        if not isinstance(self.status, CollectionStatus):
            raise ContractError("acquisition record requires a CollectionStatus")
        if not is_instance_of(self.effective_scope, tuple) or not all(
            is_instance_of(item, str) for item in self.effective_scope
        ):
            raise ContractError(
                "acquisition record effective_scope must be a tuple of strings"
            )
        if not is_instance_of(self.diagnostics, tuple) or not all(
            is_instance_of(item, Diagnostic) for item in self.diagnostics
        ):
            raise ContractError(
                "acquisition record diagnostics must be a tuple of Diagnostic"
            )
        if not is_instance_of(self.observed_inputs, tuple) or not all(
            is_instance_of(item, str) for item in self.observed_inputs
        ):
            raise ContractError(
                "acquisition record observed_inputs must be a tuple of strings"
            )

    def to_wire(self) -> dict[str, object]:
        return {
            "acquisition_id": self.acquisition_id,
            "capability": self.capability.value,
            "provider": self.provider,
            "provider_version": self.provider_version,
            "method": self.method,
            "status": self.status.value,
            "effective_scope": list(self.effective_scope),
            "coverage": self.coverage.to_wire(),
            "diagnostics": [item.to_wire() for item in self.diagnostics],
            "observed_inputs": list(self.observed_inputs),
        }


@dataclass(frozen=True)
class EvidenceRequest:
    """One bounded capability request produced by collection planning."""

    request_id: str
    capability: Capability
    target: InspectionTarget
    subject: DeclarationCandidate | None = None
    scope: tuple[str, ...] = ()
    limit: int = 1
    domain: str | None = None
    requirement_id: str | None = None
    detail: str = ""

    def __post_init__(self) -> None:
        require_str(self.request_id, "evidence request id")
        if not isinstance(self.capability, Capability):
            raise ContractError("evidence request requires a Capability")
        if not isinstance(self.target, TARGET_TYPES):
            raise ContractError("evidence request requires a typed target")
        if self.subject is not None and not isinstance(
            self.subject, DeclarationCandidate
        ):
            raise ContractError(
                "evidence request subject must be a DeclarationCandidate"
            )
        validate_scopes(self.scope, "evidence request scope")
        require_int(self.limit, "evidence request limit", minimum=1)
        optional_str(self.domain, "evidence request domain")
        optional_str(self.requirement_id, "evidence request requirement_id")
        require_str(self.detail, "evidence request detail", allow_empty=True)

    def observed_inputs(self) -> tuple[str, ...]:
        inputs = [describe_target(self.target)]
        if self.scope:
            inputs.append(f"scope={','.join(self.scope)}")
        if self.domain:
            inputs.append(f"domain={self.domain}")
        return tuple(inputs)


@dataclass(frozen=True)
class AcquiredEvidence:
    """One adapter's acquisition record with its normalized observations."""

    record: AcquisitionRecord
    observations: tuple[Observation, ...] = ()
    variants: tuple[EvidenceVariant, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.record, AcquisitionRecord):
            raise ContractError("acquired evidence requires an AcquisitionRecord")
        for observation in self.observations:
            if observation.acquisition_id != self.record.acquisition_id:
                raise ContractError(
                    "acquired observations must carry their acquisition id"
                )
        observed = {item.observation_id for item in self.observations}
        for variant in self.variants:
            if variant.observation_id not in observed:
                raise ContractError(
                    "acquired variants must reference a returned observation"
                )


@runtime_checkable
class CapabilityHandler(Protocol):
    """A concrete adapter for one or more capabilities.

    Adapters normalize their results into inspection contracts; they never
    expose provider payloads or language-specific decisions to orchestration.
    """

    name: str

    def capabilities(self) -> frozenset[Capability]: ...

    def applicable(
        self, target: InspectionTarget, context: InspectionContext, /
    ) -> bool: ...

    def availability(
        self,
        capability: Capability,
        target: InspectionTarget,
        context: InspectionContext,
        /,
    ) -> CapabilityAvailability: ...

    def acquire(
        self, request: EvidenceRequest, context: InspectionContext, /
    ) -> CapabilityResult: ...


class CapabilityRegistry(Protocol):
    """Capability resolution and bounded execution for one inspection."""

    def describe(
        self, request: InspectionRequest, context: InspectionContext
    ) -> CapabilityReport: ...

    def handlers_for(
        self,
        capability: Capability,
        target: InspectionTarget,
        context: InspectionContext,
    ) -> tuple[CapabilityHandler, ...]: ...

    def acquire(
        self, request: EvidenceRequest, context: InspectionContext
    ) -> tuple[AcquiredEvidence, ...]: ...
