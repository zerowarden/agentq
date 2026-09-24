"""Named, versioned relevance heuristics.

Scoring is a deliberately small linear rule over extracted features:

    score(e) = role_priority(e, intent) + binding_bonus(e)

Every term is a named contribution on the score breakdown, so a reviewer can
see exactly why an item scored what it did. Provider identity, token costs, and
redundancy are not inputs here: those belong to provenance, budgeting, and
selection.
"""

from __future__ import annotations

from dataclasses import dataclass

from agentq.core import ContractError, require_int, require_str, require_unique_strings

from .contracts import (
    Binding,
    EvidenceFeatures,
    EvidenceRole,
    Intent,
    ScoreBreakdown,
    ScoreContribution,
    ScoredEvidence,
)

SCORING_PROFILE = "scoring-v1"
CONTRIBUTION_BINDING_RESOLVED = "binding_resolved"
DEFAULT_BINDING_BONUS = 2

# Roles whose resolved binding is observable; a lexical mention cannot be
# binding-resolved, and required source evidence does not compete here.
RELATIONSHIP_ROLES = frozenset(
    {
        EvidenceRole.REFERENCE,
        EvidenceRole.IMPLEMENTATION,
        EvidenceRole.TEST,
    }
)

ROLE_CONTRIBUTION_NAMES: dict[EvidenceRole, str] = {
    EvidenceRole.REFERENCE: "reference_priority",
    EvidenceRole.IMPLEMENTATION: "implementation_priority",
    EvidenceRole.TEST: "test_evidence_priority",
    EvidenceRole.OWNERSHIP: "ownership_priority",
    EvidenceRole.LEXICAL_MENTION: "lexical_mention_priority",
}

# Proposed baseline priorities, not empirically established coefficients.
_BASE_PRIORITIES: dict[EvidenceRole, dict[Intent, int]] = {
    EvidenceRole.REFERENCE: {
        Intent.UNDERSTAND: 6,
        Intent.EDIT: 6,
        Intent.RENAME: 8,
        Intent.REFACTOR: 6,
        Intent.IMPACT: 8,
    },
    EvidenceRole.IMPLEMENTATION: {
        Intent.UNDERSTAND: 6,
        Intent.EDIT: 4,
        Intent.RENAME: 2,
        Intent.REFACTOR: 8,
        Intent.IMPACT: 6,
    },
    EvidenceRole.TEST: {
        Intent.UNDERSTAND: 2,
        Intent.EDIT: 8,
        Intent.RENAME: 4,
        Intent.REFACTOR: 8,
        Intent.IMPACT: 6,
    },
    EvidenceRole.OWNERSHIP: {
        Intent.UNDERSTAND: 4,
        Intent.EDIT: 6,
        Intent.RENAME: 4,
        Intent.REFACTOR: 6,
        Intent.IMPACT: 6,
    },
    EvidenceRole.LEXICAL_MENTION: {
        Intent.UNDERSTAND: 2,
        Intent.EDIT: 2,
        Intent.RENAME: 4,
        Intent.REFACTOR: 2,
        Intent.IMPACT: 4,
    },
}


def _default_intent_priorities() -> tuple[
    tuple[Intent, tuple[tuple[EvidenceRole, int], ...]], ...
]:
    """The baseline table, ordered by intent and role for determinism."""
    return tuple(
        (
            intent,
            tuple(
                (role, priorities[intent])
                for role, priorities in _BASE_PRIORITIES.items()
            ),
        )
        for intent in Intent
    )


DEFAULT_INTENT_PRIORITIES = _default_intent_priorities()


@dataclass(frozen=True)
class ScoringProfile:
    """A versioned linear profile: per-intent role priorities plus a bonus."""

    profile: str = SCORING_PROFILE
    binding_bonus: int = DEFAULT_BINDING_BONUS
    intent_priorities: tuple[
        tuple[Intent, tuple[tuple[EvidenceRole, int], ...]], ...
    ] = DEFAULT_INTENT_PRIORITIES

    def __post_init__(self) -> None:
        require_str(self.profile, "scoring profile id")
        require_int(self.binding_bonus, "scoring binding bonus")
        require_unique_strings(
            tuple(intent.value for intent, _ in self.intent_priorities),
            "scoring profile intents",
        )
        for intent, priorities in self.intent_priorities:
            if not isinstance(intent, Intent):
                raise ContractError("scoring profile priorities require intents")
            require_unique_strings(
                tuple(role.value for role, _ in priorities),
                f"scoring profile roles for {intent.value}",
            )
            for role, value in priorities:
                if not isinstance(role, EvidenceRole):
                    raise ContractError(
                        "scoring profile priorities require evidence roles"
                    )
                require_int(value, f"scoring priority for {intent.value}/{role.value}")

    def priorities(self, intent: Intent) -> tuple[tuple[EvidenceRole, int], ...]:
        for candidate, priorities in self.intent_priorities:
            if candidate is intent:
                return priorities
        return ()

    def priority(self, intent: Intent, role: EvidenceRole | None) -> int:
        if role is None:
            return 0
        for candidate, value in self.priorities(intent):
            if candidate is role:
                return value
        return 0

    def to_wire(self) -> dict[str, object]:
        return {
            "profile": self.profile,
            "binding_bonus": self.binding_bonus,
            "intent_priorities": {
                intent.value: {role.value: value for role, value in priorities}
                for intent, priorities in self.intent_priorities
            },
        }


DEFAULT_SCORING = ScoringProfile()


def score_evidence(
    features: tuple[EvidenceFeatures, ...],
    profile: ScoringProfile = DEFAULT_SCORING,
    *,
    intent: Intent,
) -> tuple[ScoredEvidence, ...]:
    """Score every observation under one named profile for one intent."""
    return tuple(_score(item, profile, intent) for item in features)


def _score(
    item: EvidenceFeatures, profile: ScoringProfile, intent: Intent
) -> ScoredEvidence:
    contributions: list[ScoreContribution] = []
    priority = profile.priority(intent, item.role)
    name = ROLE_CONTRIBUTION_NAMES.get(item.role) if item.role is not None else None
    if priority and name is not None:
        contributions.append(ScoreContribution(name=name, value=priority))
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
