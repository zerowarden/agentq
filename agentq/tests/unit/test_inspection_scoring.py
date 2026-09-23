"""Scoring profiles, features, and named contributions (parametrized)."""

from __future__ import annotations

from collections.abc import Callable

import pytest

from agentq.core import ContractError, SourceRef
from agentq.inspection.contracts import (
    Binding,
    EvidenceFeatures,
    EvidencePool,
    EvidenceRole,
    Fidelity,
    Intent,
    MentionPayload,
    ObservationKind,
    ReferencePayload,
    RepresentationKind,
    ScoreBreakdown,
    ScoreContribution,
    make_observation,
    make_variant,
)
from agentq.inspection.features import extract_features
from agentq.inspection.scoring import (
    CONTRIBUTION_BINDING_RESOLVED,
    DEFAULT_SCORING,
    RELATIONSHIP_ROLES,
    SCORING_PROFILE,
    ScoringProfile,
    score_evidence,
)

# The M1 plan's baseline optional-evidence priority table.
PRIORITY_TABLE = {
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

PRIORITY_CASES = tuple(
    pytest.param(intent, role, expected, id=f"{intent.value}-{role.value}")
    for role, priorities in PRIORITY_TABLE.items()
    for intent, expected in priorities.items()
)

CONTRIBUTION_NAMES = {
    EvidenceRole.REFERENCE: "reference_priority",
    EvidenceRole.IMPLEMENTATION: "implementation_priority",
    EvidenceRole.TEST: "test_evidence_priority",
    EvidenceRole.OWNERSHIP: "ownership_priority",
    EvidenceRole.LEXICAL_MENTION: "lexical_mention_priority",
}


def _features(
    *,
    observation_id: str,
    role: EvidenceRole,
    kind: ObservationKind,
    binding: Binding = Binding.UNKNOWN,
    domain: str | None = None,
) -> EvidenceFeatures:
    return EvidenceFeatures(
        observation_id=observation_id,
        role=role,
        observation_kind=kind,
        binding=binding,
        domain=domain,
    )


def _observation(
    *,
    kind: ObservationKind,
    path: str,
    line: int,
    relationship: str = "reference",
    binding: Binding = Binding.RESOLVED,
    domain: str | None = None,
    text: str = "listOrders()",
):
    return make_observation(
        kind=kind,
        payload=ReferencePayload(
            relationship=relationship,
            text=text,
            binding=binding,
            domain=domain,
        ),
        source=SourceRef(path=path, start_line=line, end_line=line),
    )


@pytest.mark.parametrize("intent,role,expected", PRIORITY_CASES)
def test_baseline_priority_table(
    intent: Intent, role: EvidenceRole, expected: int
) -> None:
    assert DEFAULT_SCORING.priority(intent, role) == expected


@pytest.mark.parametrize(
    "role,expected_name",
    tuple(
        pytest.param(role, name, id=role.value)
        for role, name in CONTRIBUTION_NAMES.items()
    ),
)
def test_priority_contribution_names(role: EvidenceRole, expected_name: str) -> None:
    features = _features(
        observation_id="obs-1", role=role, kind=ObservationKind.SEMANTIC_REFERENCE
    )
    scored = score_evidence((features,), DEFAULT_SCORING, intent=Intent.RENAME)[0]
    expected = DEFAULT_SCORING.priority(Intent.RENAME, role)
    assert scored.score.contribution(expected_name) == expected
    assert scored.score.contribution("role_priority") is None


@pytest.mark.parametrize(
    "role,binding,expected_bonus",
    (
        pytest.param(EvidenceRole.REFERENCE, Binding.RESOLVED, 2, id="reference"),
        pytest.param(EvidenceRole.TEST, Binding.RESOLVED, 2, id="test"),
        pytest.param(
            EvidenceRole.IMPLEMENTATION, Binding.RESOLVED, 2, id="implementation"
        ),
        pytest.param(
            EvidenceRole.LEXICAL_MENTION, Binding.RESOLVED, 0, id="lexical-mention"
        ),
        pytest.param(EvidenceRole.DECLARATION, Binding.RESOLVED, 0, id="declaration"),
        pytest.param(EvidenceRole.REFERENCE, Binding.UNRESOLVED, 0, id="unresolved"),
    ),
)
def test_binding_bonus_applies_only_to_relationship_evidence(
    role: EvidenceRole, binding: Binding, expected_bonus: int
) -> None:
    features = _features(
        observation_id="obs-1",
        role=role,
        kind=ObservationKind.SEMANTIC_REFERENCE,
        binding=binding,
    )
    scored = score_evidence((features,), DEFAULT_SCORING, intent=Intent.EDIT)[0]
    assert scored.score.contribution(CONTRIBUTION_BINDING_RESOLVED) == (
        expected_bonus or None
    )


def test_total_is_the_sum_of_named_contributions() -> None:
    features = _features(
        observation_id="obs-1",
        role=EvidenceRole.TEST,
        kind=ObservationKind.TEST_MENTION,
        binding=Binding.RESOLVED,
    )
    scored = score_evidence((features,), DEFAULT_SCORING, intent=Intent.EDIT)[0]
    assert scored.score.total == 10
    assert scored.score.contributions == (
        ScoreContribution(name="test_evidence_priority", value=8),
        ScoreContribution(name=CONTRIBUTION_BINDING_RESOLVED, value=2),
    )


def test_test_domain_references_are_test_evidence() -> None:
    pool = EvidencePool(
        request_id="req-1",
        observations=(
            _observation(
                kind=ObservationKind.SEMANTIC_REFERENCE,
                path="tests/test_orders.py",
                line=4,
                domain="test",
            ),
            _observation(
                kind=ObservationKind.SEMANTIC_REFERENCE,
                path="src/use.py",
                line=9,
            ),
        ),
    )
    features = {item.observation_id: item for item in extract_features(pool)}
    test_feature = next(item for item in features.values() if item.domain == "test")
    source_feature = next(item for item in features.values() if item.domain is None)
    assert test_feature.role is EvidenceRole.TEST
    assert test_feature.observation_kind is ObservationKind.SEMANTIC_REFERENCE
    assert source_feature.role is EvidenceRole.REFERENCE


def test_test_domain_reference_reaches_the_plan_example_score() -> None:
    pool = EvidencePool(
        request_id="req-1",
        observations=(
            _observation(
                kind=ObservationKind.SEMANTIC_REFERENCE,
                path="tests/test_orders.py",
                line=4,
                domain="test",
            ),
        ),
    )
    features = extract_features(pool)
    scored = score_evidence(features, DEFAULT_SCORING, intent=Intent.EDIT)[0]
    assert scored.score.total == 10
    assert scored.score.contribution("test_evidence_priority") == 8
    assert scored.score.contribution(CONTRIBUTION_BINDING_RESOLVED) == 2


def test_unknown_feature_values_stay_unknown() -> None:
    pool = EvidencePool(
        request_id="req-1",
        observations=(
            make_observation(
                kind=ObservationKind.LEXICAL_MENTION,
                payload=MentionPayload(text="orders()"),
                source=SourceRef(path="src/a.py", start_line=1, end_line=1),
            ),
        ),
    )
    feature = extract_features(pool)[0]
    assert feature.relation is None
    assert feature.binding is Binding.UNKNOWN
    assert feature.domain is None


def test_profile_without_priorities_scores_only_binding() -> None:
    features = _features(
        observation_id="obs-1",
        role=EvidenceRole.REFERENCE,
        kind=ObservationKind.SEMANTIC_REFERENCE,
        binding=Binding.RESOLVED,
    )
    profile = ScoringProfile(profile="test-v1", intent_priorities=())
    scored = score_evidence((features,), profile, intent=Intent.EDIT)[0]
    assert scored.score.total == 2
    assert scored.score.contributions == (
        ScoreContribution(name=CONTRIBUTION_BINDING_RESOLVED, value=2),
    )


def test_scoring_is_deterministic_and_provider_free() -> None:
    features = (
        _features(
            observation_id="obs-1",
            role=EvidenceRole.REFERENCE,
            kind=ObservationKind.SEMANTIC_REFERENCE,
            binding=Binding.RESOLVED,
        ),
    )
    first = score_evidence(features, DEFAULT_SCORING, intent=Intent.IMPACT)
    second = score_evidence(features, DEFAULT_SCORING, intent=Intent.IMPACT)
    assert first == second
    assert "provider" not in str(DEFAULT_SCORING.to_wire())


@pytest.mark.parametrize(
    "build",
    (
        pytest.param(
            lambda: ScoringProfile(
                profile="dup",
                intent_priorities=((Intent.EDIT, ()), (Intent.EDIT, ())),
            ),
            id="duplicate-intent",
        ),
        pytest.param(
            lambda: ScoringProfile(
                profile="dup-role",
                intent_priorities=(
                    (
                        Intent.EDIT,
                        (
                            (EvidenceRole.REFERENCE, 1),
                            (EvidenceRole.REFERENCE, 2),
                        ),
                    ),
                ),
            ),
            id="duplicate-role",
        ),
    ),
)
def test_invalid_profiles_are_rejected(build: Callable[[], ScoringProfile]) -> None:
    with pytest.raises(ContractError):
        build()


def test_breakdown_rejects_mismatched_totals() -> None:
    with pytest.raises(ContractError):
        ScoreBreakdown(
            observation_id="obs-1",
            total=5,
            contributions=(ScoreContribution(name="reference_priority", value=3),),
            profile="test",
        )


def test_profile_id_and_relationship_roles_are_explicit() -> None:
    assert DEFAULT_SCORING.profile == SCORING_PROFILE == "scoring-v1"
    assert EvidenceRole.LEXICAL_MENTION not in RELATIONSHIP_ROLES
    assert {
        EvidenceRole.REFERENCE,
        EvidenceRole.IMPLEMENTATION,
        EvidenceRole.TEST,
    } <= RELATIONSHIP_ROLES


def test_variant_construction_is_not_scoring_input() -> None:
    observation = _observation(
        kind=ObservationKind.SEMANTIC_REFERENCE, path="src/a.py", line=1
    )
    variant = make_variant(
        observation_id=observation.observation_id,
        representation=RepresentationKind.REFERENCE,
        fidelity=Fidelity.BOUNDED,
        source=observation.source,
        text="listOrders()",
    )
    assert variant.text == "listOrders()"
