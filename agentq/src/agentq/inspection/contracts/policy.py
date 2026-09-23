"""Intent requirements and the compiled evidence policy."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from agentq.core import (
    ContractError,
    is_instance_of,
    optional_str,
    require_str,
    require_unique_strings,
)

from .capability import Capability
from .evidence import ObservationKind, RepresentationKind
from .targets import Intent, TargetKind


class EvidenceRole(str, Enum):
    """What an observation contributes to an inspection, independent of intent."""

    DECLARATION = "declaration"
    TARGET_SOURCE = "target_source"
    TARGET_STRUCTURE = "target_structure"
    REFERENCE = "reference"
    IMPLEMENTATION = "implementation"
    TEST = "test"
    OWNERSHIP = "ownership"
    LEXICAL_MENTION = "lexical_mention"


class RequirementRule(str, Enum):
    """Distinct satisfaction rules; not every requirement is an evidence count."""

    MINIMUM_EVIDENCE = "minimum_evidence"
    EXACT_SOURCE = "exact_source"
    COLLECTION_OUTCOME = "collection_outcome"
    REPRESENTATIVE_EVIDENCE = "representative_evidence"


class RequirementStrength(str, Enum):
    REQUIRED = "required"
    OPTIONAL = "optional"


# ---------------------------------------------------------------------------
# Policy
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class EvidenceRequirement:
    """One evidence requirement with its own satisfaction rule."""

    requirement_id: str
    role: EvidenceRole
    rule: RequirementRule
    strength: RequirementStrength
    description: str = ""
    capabilities: tuple[Capability, ...] = ()
    acceptable_kinds: tuple[ObservationKind, ...] = ()
    representations: tuple[RepresentationKind, ...] = ()
    domain: str | None = None

    def __post_init__(self) -> None:
        require_str(self.requirement_id, "evidence requirement id")
        if not isinstance(self.role, EvidenceRole):
            raise ContractError("evidence requirement role must be an EvidenceRole")
        if not isinstance(self.rule, RequirementRule):
            raise ContractError("evidence requirement rule must be a RequirementRule")
        if not isinstance(self.strength, RequirementStrength):
            raise ContractError(
                "evidence requirement strength must be a RequirementStrength"
            )
        require_str(
            self.description, "evidence requirement description", allow_empty=True
        )
        if not is_instance_of(self.capabilities, tuple) or not all(
            is_instance_of(item, Capability) for item in self.capabilities
        ):
            raise ContractError(
                "evidence requirement capabilities must be Capability records"
            )
        require_unique_strings(
            tuple(item.value for item in self.capabilities),
            "evidence requirement capabilities",
        )
        optional_str(self.domain, "evidence requirement domain")

    @property
    def preferred_capability(self) -> Capability | None:
        """The first acceptable capability in preference order."""
        return self.capabilities[0] if self.capabilities else None

    def is_required(self) -> bool:
        return self.strength is RequirementStrength.REQUIRED

    def to_wire(self) -> dict[str, object]:
        return {
            "requirement_id": self.requirement_id,
            "role": self.role.value,
            "rule": self.rule.value,
            "strength": self.strength.value,
            "description": self.description,
            "capabilities": [item.value for item in self.capabilities],
            "acceptable_kinds": [kind.value for kind in self.acceptable_kinds],
            "representations": [item.value for item in self.representations],
            "domain": self.domain,
        }


@dataclass(frozen=True)
class EvidencePolicy:
    """The compiled evidence requirements for one request."""

    profile: str
    intent: Intent
    target_kind: TargetKind
    requirements: tuple[EvidenceRequirement, ...]
    limitations: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        require_str(self.profile, "evidence policy profile")
        if not isinstance(self.intent, Intent):
            raise ContractError("evidence policy intent must be an Intent")
        if not isinstance(self.target_kind, TargetKind):
            raise ContractError("evidence policy target_kind must be a TargetKind")
        if not is_instance_of(self.requirements, tuple) or not all(
            is_instance_of(item, EvidenceRequirement) for item in self.requirements
        ):
            raise ContractError(
                "evidence policy requirements must be EvidenceRequirement records"
            )
        require_unique_strings(
            tuple(item.requirement_id for item in self.requirements),
            "evidence policy requirement ids",
        )

    def required(self) -> tuple[EvidenceRequirement, ...]:
        return tuple(item for item in self.requirements if item.is_required())

    def optional(self) -> tuple[EvidenceRequirement, ...]:
        return tuple(item for item in self.requirements if not item.is_required())

    def requirement(self, requirement_id: str) -> EvidenceRequirement | None:
        for item in self.requirements:
            if item.requirement_id == requirement_id:
                return item
        return None

    def to_wire(self) -> dict[str, object]:
        return {
            "profile": self.profile,
            "intent": self.intent.value,
            "target_kind": self.target_kind.value,
            "requirements": [item.to_wire() for item in self.requirements],
            "limitations": list(self.limitations),
        }
