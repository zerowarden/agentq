"""Features, scores, selection plans, and policy assessment."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any

from agentq.core import (
    ContractError,
    is_instance_of,
    optional_str,
    require_int,
    require_str,
    require_unique_strings,
)

from .evidence import Binding, EvidenceVariant, ObservationKind
from .policy import (
    EvidenceRole,
    RequirementStrength,
)


class RequirementStatus(str, Enum):
    SATISFIED = "satisfied"
    UNSATISFIED = "unsatisfied"
    NOT_APPLICABLE = "not_applicable"


# ---------------------------------------------------------------------------
# Features, scores, selection, and assessment
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class EvidenceFeatures:
    """Observable inputs to scoring; unknown values stay unknown."""

    observation_id: str
    role: EvidenceRole | None
    observation_kind: ObservationKind
    relation: str | None = None
    binding: Binding = Binding.UNKNOWN
    domain: str | None = None
    flags: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        require_str(self.observation_id, "evidence features observation id")
        if self.role is not None and not isinstance(self.role, EvidenceRole):
            raise ContractError("evidence features role must be an EvidenceRole")
        if not isinstance(self.observation_kind, ObservationKind):
            raise ContractError(
                "evidence features observation_kind must be an ObservationKind"
            )
        if not isinstance(self.binding, Binding):
            raise ContractError("evidence features binding must be a Binding")
        optional_str(self.relation, "evidence features relation")
        optional_str(self.domain, "evidence features domain")

    def to_wire(self) -> dict[str, Any]:
        return {
            "observation_id": self.observation_id,
            "role": self.role.value if self.role else None,
            "observation_kind": self.observation_kind.value,
            "relation": self.relation,
            "binding": self.binding.value,
            "domain": self.domain,
            "flags": list(self.flags),
        }


@dataclass(frozen=True)
class ScoreContribution:
    """One named contribution to an evidence score."""

    name: str
    value: int

    def __post_init__(self) -> None:
        require_str(self.name, "score contribution name")
        require_int(self.value, "score contribution value")


@dataclass(frozen=True)
class ScoreBreakdown:
    """Total score and its named contributions under one scoring profile."""

    observation_id: str
    total: int
    contributions: tuple[ScoreContribution, ...]
    profile: str

    def __post_init__(self) -> None:
        require_str(self.observation_id, "score breakdown observation id")
        require_str(self.profile, "score breakdown profile")
        if not is_instance_of(self.contributions, tuple) or not all(
            is_instance_of(item, ScoreContribution) for item in self.contributions
        ):
            raise ContractError(
                "score breakdown contributions must be ScoreContribution records"
            )
        expected = sum(item.value for item in self.contributions)
        if self.total != expected:
            raise ContractError(
                f"score total {self.total} does not match contributions {expected}"
            )

    def contribution(self, name: str) -> int | None:
        for item in self.contributions:
            if item.name == name:
                return item.value
        return None


@dataclass(frozen=True)
class ScoredEvidence:
    """One observation's features and score."""

    features: EvidenceFeatures
    score: ScoreBreakdown

    def __post_init__(self) -> None:
        if self.features.observation_id != self.score.observation_id:
            raise ContractError("scored evidence features and score must agree")

    @property
    def observation_id(self) -> str:
        return self.features.observation_id


@dataclass(frozen=True)
class SelectedEvidence:
    """One selected representation; the reason is separate from the score."""

    variant: EvidenceVariant
    reason: str
    score: int = 0
    requirement_id: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.variant, EvidenceVariant):
            raise ContractError("selected evidence requires an EvidenceVariant")
        require_str(self.reason, "selected evidence reason")
        optional_str(self.requirement_id, "selected evidence requirement_id")

    @property
    def observation_id(self) -> str:
        return self.variant.observation_id

    @property
    def variant_id(self) -> str:
        return self.variant.variant_id


@dataclass(frozen=True)
class OmittedEvidence:
    """One representation deliberately left out of the bundle."""

    observation_id: str
    variant_id: str | None
    reason: str

    def __post_init__(self) -> None:
        require_str(self.observation_id, "omitted evidence observation id")
        optional_str(self.variant_id, "omitted evidence variant id")
        require_str(self.reason, "omitted evidence reason")


@dataclass(frozen=True)
class SelectionPlan:
    """Selected representations, omissions, and their measured cost."""

    profile: str
    selected: tuple[SelectedEvidence, ...] = ()
    omitted: tuple[OmittedEvidence, ...] = ()
    reserved: tuple[str, ...] = ()
    measured_cost: int = 0
    budget_chars: int = 0

    def __post_init__(self) -> None:
        require_str(self.profile, "selection plan profile")
        require_int(self.measured_cost, "selection measured cost", minimum=0)
        require_int(self.budget_chars, "selection budget chars", minimum=0)
        if self.measured_cost > self.budget_chars:
            raise ContractError("selection measured cost exceeds its budget")
        require_unique_strings(
            tuple(item.variant_id for item in self.selected),
            "selection selected variants",
        )
        require_unique_strings(self.reserved, "selection reserved requirements")

    def selected_observation_ids(self) -> tuple[str, ...]:
        return tuple(dict.fromkeys(item.observation_id for item in self.selected))

    def variant_ids(self) -> tuple[str, ...]:
        return tuple(item.variant_id for item in self.selected)


@dataclass(frozen=True)
class RequirementAssessment:
    requirement_id: str
    status: RequirementStatus
    strength: RequirementStrength = RequirementStrength.REQUIRED
    supporting: tuple[str, ...] = ()
    detail: str | None = None

    def to_wire(self) -> dict[str, Any]:
        return {
            "requirement_id": self.requirement_id,
            "status": self.status.value,
            "strength": self.strength.value,
            "supporting": list(self.supporting),
            "detail": self.detail,
        }


@dataclass(frozen=True)
class PolicyAssessment:
    """What the selected evidence actually satisfied."""

    profile: str
    requirements: tuple[RequirementAssessment, ...]

    def __post_init__(self) -> None:
        require_str(self.profile, "policy assessment profile")

    def unsatisfied(
        self, *, required_only: bool = False
    ) -> tuple[RequirementAssessment, ...]:
        return tuple(
            item
            for item in self.requirements
            if item.status is RequirementStatus.UNSATISFIED
            and (not required_only or item.strength is RequirementStrength.REQUIRED)
        )

    def by_id(self, requirement_id: str) -> RequirementAssessment | None:
        for item in self.requirements:
            if item.requirement_id == requirement_id:
                return item
        return None
