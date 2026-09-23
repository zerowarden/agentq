"""Named, versioned relevance heuristics.

Scoring is a deliberately small linear rule over extracted features
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from agentq.core import ContractError, require_int, require_str, require_unique_strings

from .contracts import (
    Binding,
    EvidenceFeatures,
    EvidenceRole,
    ScoreBreakdown,
    ScoreContribution,
    ScoredEvidence,
)

SCORING_PROFILE = "scoring-v0"
CONTRIBUTION_ROLE_PRIORITY = "role_priority"
CONTRIBUTION_BINDING_RESOLVED = "binding_resolved"
DEFAULT_BINDING_BONUS = 2

RELATIONSHIP_ROLES = frozenset(
    {
        EvidenceRole.REFERENCE,
        EvidenceRole.IMPLEMENTATION,
        EvidenceRole.TEST,
        EvidenceRole.LEXICAL_MENTION,
    }
)


@dataclass(frozen=True)
class ScoringProfile:
    """A versioned linear profile: role priority plus a binding bonus.

    The baseline carries no role priorities; per-intent priority tables
    replace this profile in M1.4 without changing the score contract.
    """

    profile: str = SCORING_PROFILE
    binding_bonus: int = DEFAULT_BINDING_BONUS
    role_priorities: tuple[tuple[EvidenceRole, int], ...] = ()

    def __post_init__(self) -> None:
        require_str(self.profile, "scoring profile id")
        require_int(self.binding_bonus, "scoring binding bonus")
        require_unique_strings(
            tuple(role.value for role, _ in self.role_priorities),
            "scoring profile roles",
        )
        for role, value in self.role_priorities:
            if not isinstance(role, EvidenceRole):
                raise ContractError("scoring profile priorities require roles")
            require_int(value, f"scoring priority for {role.value}")

    def priority(self, role: EvidenceRole | None) -> int:
        if role is None:
            return 0
        for candidate, value in self.role_priorities:
            if candidate is role:
                return value
        return 0

    def to_wire(self) -> dict[str, Any]:
        return {
            "profile": self.profile,
            "binding_bonus": self.binding_bonus,
            "role_priorities": {
                role.value: value for role, value in self.role_priorities
            },
        }


DEFAULT_SCORING = ScoringProfile()


def score_evidence(
    features: tuple[EvidenceFeatures, ...],
    profile: ScoringProfile = DEFAULT_SCORING,
) -> tuple[ScoredEvidence, ...]:
    """Score every observation under one named profile."""
    return tuple(_score(features_item, profile) for features_item in features)


def _score(item: EvidenceFeatures, profile: ScoringProfile) -> ScoredEvidence:
    contributions: list[ScoreContribution] = []
    priority = profile.priority(item.role)
    if priority:
        contributions.append(
            ScoreContribution(name=CONTRIBUTION_ROLE_PRIORITY, value=priority)
        )
    if (
        item.binding is Binding.RESOLVED
        and item.role in RELATIONSHIP_ROLES
        and profile.binding_bonus
    ):
        contributions.append(
            ScoreContribution(
                name=CONTRIBUTION_BINDING_RESOLVED, value=profile.binding_bonus
            )
        )
    total = sum(contribution.value for contribution in contributions)
    return ScoredEvidence(
        features=item,
        score=ScoreBreakdown(
            observation_id=item.observation_id,
            total=total,
            contributions=tuple(contributions),
            profile=profile.profile,
        ),
    )
