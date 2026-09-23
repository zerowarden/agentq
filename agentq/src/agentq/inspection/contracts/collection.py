"""Collection plans: bounded capability requests matched from policy."""

from __future__ import annotations

from dataclasses import dataclass

from agentq.core import ContractError, optional_str, require_int, require_str

from ._common import validate_scopes
from .capability import AvailabilityStatus, Capability
from .policy import EvidenceRole
from .resolution import DeclarationCandidate
from .targets import TARGET_TYPES, InspectionTarget

# ---------------------------------------------------------------------------
# Collection planning
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RequirementOmission:
    """A policy requirement that could not be planned into a collection request."""

    requirement_id: str
    capability: Capability
    status: AvailabilityStatus
    reason: str

    def __post_init__(self) -> None:
        require_str(self.requirement_id, "requirement omission id")
        if not isinstance(self.capability, Capability):
            raise ContractError("requirement omission requires a Capability")
        if not isinstance(self.status, AvailabilityStatus):
            raise ContractError("requirement omission requires an AvailabilityStatus")
        require_str(self.reason, "requirement omission reason")


@dataclass(frozen=True)
class CollectionRequest:
    """One bounded capability request matched from a policy requirement."""

    request_id: str
    capability: Capability
    role: EvidenceRole
    requirement_id: str
    target: InspectionTarget | None = None
    subject: DeclarationCandidate | None = None
    scope: tuple[str, ...] = ()
    domain: str | None = None
    limit: int = 1
    detail: str = ""

    def __post_init__(self) -> None:
        require_str(self.request_id, "collection request id")
        if not isinstance(self.capability, Capability):
            raise ContractError("collection request requires a Capability")
        if not isinstance(self.role, EvidenceRole):
            raise ContractError("collection request role must be an EvidenceRole")
        require_str(self.requirement_id, "collection request requirement_id")
        if self.target is not None and not isinstance(self.target, TARGET_TYPES):
            raise ContractError("collection request target must be a typed target")
        if self.subject is not None and not isinstance(
            self.subject, DeclarationCandidate
        ):
            raise ContractError(
                "collection request subject must be a DeclarationCandidate"
            )
        validate_scopes(self.scope, "collection request scope")
        optional_str(self.domain, "collection request domain")
        require_int(self.limit, "collection request limit", minimum=1)


@dataclass(frozen=True)
class CollectionPlan:
    """The bounded collection the policy compiled, with explicit gaps."""

    profile: str
    request_id: str
    target: InspectionTarget
    requests: tuple[CollectionRequest, ...] = ()
    omissions: tuple[RequirementOmission, ...] = ()

    def __post_init__(self) -> None:
        require_str(self.profile, "collection plan profile")
        require_str(self.request_id, "collection plan request id")
        if not isinstance(self.target, TARGET_TYPES):
            raise ContractError("collection plan requires a typed target")

    def capabilities(self) -> tuple[Capability, ...]:
        return tuple(dict.fromkeys(item.capability for item in self.requests))
