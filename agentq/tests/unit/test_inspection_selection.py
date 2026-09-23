"""Selection: required evidence, diversity, overlap, and serialized cost."""

from __future__ import annotations

from agentq.core import SourceRef, canonical_json, typed_coverage
from agentq.inspection.budgeting import DeliveryBudget
from agentq.inspection.contracts import (
    Capability,
    DeclarationPayload,
    EvidencePolicy,
    EvidencePool,
    EvidenceRequirement,
    EvidenceRole,
    EvidenceVariant,
    Fidelity,
    Intent,
    Observation,
    ObservationKind,
    PackagePayload,
    ReferencePayload,
    RepresentationKind,
    RequirementRule,
    RequirementStrength,
    ScoredEvidence,
    SelectedEvidence,
    SelectionPlan,
    SourceSpan,
    SourceWindowPayload,
    TargetKind,
    make_observation,
    make_variant,
    selected_evidence_to_wire,
)
from agentq.inspection.features import extract_features
from agentq.inspection.rendering import evidence_block, selected_cost
from agentq.inspection.scoring import ScoringProfile, score_evidence
from agentq.inspection.selection import (
    DEFAULT_SELECTION,
    OMISSION_BUDGET,
    OMISSION_NO_REPRESENTATION,
    OMISSION_OVERLAP,
    OMISSION_SAME_FILE,
    REASON_REQUIRED,
    REASON_ROLE,
    SelectionProfile,
    select_evidence,
)

SELECTION_SCORING = ScoringProfile(
    profile="selection-test-v1",
    binding_bonus=0,
    intent_priorities=(
        (
            Intent.EDIT,
            (
                (EvidenceRole.REFERENCE, 10),
                (EvidenceRole.IMPLEMENTATION, 5),
                (EvidenceRole.TEST, 5),
                (EvidenceRole.OWNERSHIP, 5),
                (EvidenceRole.LEXICAL_MENTION, 1),
            ),
        ),
    ),
)

AMPLE = DeliveryBudget(max_chars=12_000, envelope_chars=200)


def _reference(
    path: str, line: int = 4, *, domain: str | None = None
) -> tuple[Observation, tuple[EvidenceVariant, ...]]:
    text = f"use({path})"
    observation = make_observation(
        kind=(
            ObservationKind.TEST_MENTION
            if domain == "test"
            else ObservationKind.SEMANTIC_REFERENCE
        ),
        payload=ReferencePayload(relationship="reference", text=text, domain=domain),
        source=SourceRef(path=path, start_line=line, end_line=line),
    )
    variant = make_variant(
        observation_id=observation.observation_id,
        representation=RepresentationKind.REFERENCE,
        fidelity=Fidelity.BOUNDED,
        source=observation.source,
        text=text,
        span=SourceSpan(start_line=line, end_line=line),
    )
    return observation, (variant,)


def _source(
    path: str, start: int, end: int
) -> tuple[Observation, tuple[EvidenceVariant, ...]]:
    body = "\n".join(f"line {number}" for number in range(start, end + 1))
    observation = make_observation(
        kind=ObservationKind.SOURCE_WINDOW,
        payload=SourceWindowPayload(
            text=body, span=SourceSpan(start_line=start, end_line=end)
        ),
        source=SourceRef(path=path, start_line=start, end_line=end),
    )
    variant = make_variant(
        observation_id=observation.observation_id,
        representation=RepresentationKind.EXACT_SOURCE,
        fidelity=Fidelity.EXACT,
        source=observation.source,
        text=body,
        span=SourceSpan(start_line=start, end_line=end),
    )
    return observation, (variant,)


def _bounded_source(
    path: str, start: int, end: int
) -> tuple[Observation, tuple[EvidenceVariant, ...]]:
    observation, _ = _source(path, start, end)
    variant = make_variant(
        observation_id=observation.observation_id,
        representation=RepresentationKind.EXCERPT,
        fidelity=Fidelity.BOUNDED,
        source=observation.source,
        text="line 1",
        span=SourceSpan(start_line=start, end_line=end),
    )
    return observation, (variant,)


def _declaration(
    path: str, line: int
) -> tuple[Observation, tuple[EvidenceVariant, ...]]:
    body = "def target():\n    pass"
    observation = make_observation(
        kind=ObservationKind.DECLARATION,
        payload=DeclarationPayload(
            name="target",
            kind="function",
            signature="target()",
            span=SourceSpan(start_line=line, end_line=line),
        ),
        source=SourceRef(path=path, start_line=line, end_line=line),
    )
    signature = make_variant(
        observation_id=observation.observation_id,
        representation=RepresentationKind.SIGNATURE,
        fidelity=Fidelity.SUMMARY,
        source=observation.source,
        text="target()",
        span=SourceSpan(start_line=line, end_line=line),
    )
    exact = make_variant(
        observation_id=observation.observation_id,
        representation=RepresentationKind.EXACT_SOURCE,
        fidelity=Fidelity.EXACT,
        source=observation.source,
        text=body,
        span=SourceSpan(start_line=line, end_line=line),
    )
    return observation, (signature, exact)


def _ownership(path: str) -> tuple[Observation, tuple[EvidenceVariant, ...]]:
    observation = make_observation(
        kind=ObservationKind.OWNING_PACKAGE,
        payload=PackagePayload(path=path, kind="python"),
        source=SourceRef(path=path),
    )
    variant = make_variant(
        observation_id=observation.observation_id,
        representation=RepresentationKind.PACKAGE,
        fidelity=Fidelity.SUMMARY,
        source=observation.source,
        text=f"python package ({path})",
    )
    return observation, (variant,)


def _pool(*pairs: tuple[Observation, tuple[EvidenceVariant, ...]]) -> EvidencePool:
    observations: list[Observation] = []
    variants = []
    for observation, items in pairs:
        observations.append(observation)
        variants.extend(items)
    return EvidencePool(
        request_id="req-1",
        observations=tuple(observations),
        variants=tuple(variants),
        coverage=typed_coverage("complete"),
    )


def _scores(pool: EvidencePool) -> tuple[ScoredEvidence, ...]:
    return score_evidence(extract_features(pool), SELECTION_SCORING, intent=Intent.EDIT)


def _policy(*requirements: EvidenceRequirement) -> EvidencePolicy:
    return EvidencePolicy(
        profile="test-policy",
        intent=Intent.EDIT,
        target_kind=TargetKind.SYMBOL,
        requirements=tuple(requirements),
    )


def _exact_source_requirement(
    strength: RequirementStrength = RequirementStrength.REQUIRED,
) -> EvidenceRequirement:
    return EvidenceRequirement(
        requirement_id="target_source",
        role=EvidenceRole.TARGET_SOURCE,
        rule=RequirementRule.EXACT_SOURCE,
        strength=strength,
        capabilities=(Capability.READ_SOURCE,),
        acceptable_kinds=(ObservationKind.SOURCE_WINDOW,),
        representations=(RepresentationKind.EXACT_SOURCE,),
    )


def _select(
    pool: EvidencePool,
    scores: tuple[ScoredEvidence, ...],
    policy: EvidencePolicy,
    budget: DeliveryBudget = AMPLE,
    *,
    profile: SelectionProfile = DEFAULT_SELECTION,
    output_format: str = "text",
) -> SelectionPlan:
    return select_evidence(
        pool,
        scores,
        policy,
        budget,
        output_format=output_format,
        profile=profile,
    )


def _selected_ids(plan: SelectionPlan) -> list[str]:
    return [item.observation_id for item in plan.selected]


def test_required_source_survives_and_is_selected_first() -> None:
    source = _source("src/a.py", 1, 3)
    noisy = [_reference(f"src/use{index}.py") for index in range(3)]
    pool = _pool(source, *noisy)
    plan = _select(pool, _scores(pool), _policy(_exact_source_requirement()))
    assert plan.selected[0].reason == REASON_REQUIRED
    assert plan.selected[0].observation_id == source[0].observation_id
    assert plan.reserved == ("target_source",)
    assert plan.measured_cost == sum(
        selected_cost(item, "text") for item in plan.selected
    )


def test_exact_requirement_never_falls_back_to_a_bounded_excerpt() -> None:
    bounded = _bounded_source("src/a.py", 1, 3)
    pool = _pool(bounded)
    plan = _select(pool, _scores(pool), _policy(_exact_source_requirement()))
    assert plan.reserved == ()
    assert all(item.reason != REASON_REQUIRED for item in plan.selected)
    assert all(item.variant.fidelity is not Fidelity.EXACT for item in plan.selected)


def test_minimum_evidence_falls_back_to_a_smaller_variant() -> None:
    declaration = _declaration("src/a.py", 1)
    requirement = EvidenceRequirement(
        requirement_id="declaration_identity",
        role=EvidenceRole.DECLARATION,
        rule=RequirementRule.MINIMUM_EVIDENCE,
        strength=RequirementStrength.REQUIRED,
        capabilities=(Capability.FIND_DECLARATIONS,),
        acceptable_kinds=(ObservationKind.DECLARATION,),
        representations=(
            RepresentationKind.SIGNATURE,
            RepresentationKind.EXACT_SOURCE,
        ),
    )
    pool = _pool(declaration)
    signature_item = SelectedEvidence(
        variant=declaration[1][0], reason=REASON_REQUIRED, score=0
    )
    budget = DeliveryBudget(
        max_chars=selected_cost(signature_item, "text") + 20, envelope_chars=10
    )
    plan = _select(pool, _scores(pool), _policy(requirement), budget=budget)
    assert len(plan.selected) == 1
    assert plan.selected[0].variant.variant_id == declaration[1][0].variant_id
    assert plan.reserved == ("declaration_identity",)


def test_overlapping_same_kind_excerpts_are_omitted() -> None:
    first = _source("src/a.py", 1, 10)
    second = _source("src/a.py", 5, 15)
    pool = _pool(first, second)
    plan = _select(pool, _scores(pool), _policy())
    selected = _selected_ids(plan)
    assert first[0].observation_id in selected
    assert second[0].observation_id not in selected
    assert any(item.reason == OMISSION_OVERLAP for item in plan.omitted)


def test_distinct_kinds_at_one_location_both_represent() -> None:
    syntactic = _reference("tests/test_orders.py", 4)
    lexical = _reference("tests/test_orders.py", 4, domain="test")
    pool = _pool(syntactic, lexical)
    plan = _select(pool, _scores(pool), _policy())
    assert set(_selected_ids(plan)) == {
        syntactic[0].observation_id,
        lexical[0].observation_id,
    }
    assert all(item.reason == REASON_ROLE for item in plan.selected)


def test_role_diversity_prefers_unrepresented_roles() -> None:
    references = [_reference(f"src/use{index}.py") for index in range(3)]
    ownership = _ownership("pyproject.toml")
    pool = _pool(*references, ownership)
    scores = _scores(pool)
    wide = _select(pool, scores, _policy())
    first_two = wide.selected[:2]
    assert [item.reason for item in first_two] == [REASON_ROLE, REASON_ROLE]
    assert len({item.observation_id for item in first_two}) == 2

    budget = DeliveryBudget(
        max_chars=sum(selected_cost(item, "text") for item in first_two) + 20,
        envelope_chars=10,
    )
    tight = _select(pool, scores, _policy(), budget=budget)
    assert len(tight.selected) == 2
    assert ownership[0].observation_id in _selected_ids(tight)


def test_per_file_limit_omits_repeated_use_sites() -> None:
    references = [_reference("src/use.py", line=line) for line in (4, 8, 12)]
    ownership = _ownership("pyproject.toml")
    pool = _pool(*references, ownership)
    plan = _select(pool, _scores(pool), _policy())
    assert any(item.reason == OMISSION_SAME_FILE for item in plan.omitted)
    selected_paths = [item.variant.source.path for item in plan.selected]
    assert selected_paths.count("src/use.py") == 2


def test_selection_is_deterministic_for_equal_scores() -> None:
    references = [_reference("src/a.py", line=4), _reference("src/b.py", line=4)]
    pool = _pool(*references)
    scores = _scores(pool)
    assert _select(pool, scores, _policy()) == _select(pool, scores, _policy())
    plan = _select(pool, scores, _policy())
    expected = sorted(item.observation_id for item in plan.selected)
    assert [item.observation_id for item in plan.selected] == expected


def test_selection_costs_use_the_requested_format() -> None:
    observation = _reference("src/a.py")
    item = SelectedEvidence(variant=observation[1][0], reason=REASON_REQUIRED, score=10)
    assert selected_cost(item, "text") == (
        sum(len(line) + 1 for line in evidence_block(item)) + 1
    )
    assert selected_cost(item, "json") == (
        len(canonical_json(selected_evidence_to_wire(item))) + 1
    )
    assert selected_cost(item, "json") >= len(item.variant.text)


def test_selector_replacement_reuses_the_same_pool() -> None:
    source = _source("src/a.py", 1, 3)
    references = [_reference(f"src/use{index}.py") for index in range(2)]
    pool = _pool(source, *references)
    scores = _scores(pool)
    before = tuple(observation.observation_id for observation in pool.observations)
    default = _select(pool, scores, _policy(_exact_source_requirement()))
    empty = _select(
        pool,
        scores,
        _policy(_exact_source_requirement()),
        profile=SelectionProfile(
            profile="selection-test-empty",
            reserve_required=False,
            role_diversity=False,
            fill_by_score=False,
        ),
    )
    assert default.selected
    assert empty.selected == ()
    assert (
        tuple(observation.observation_id for observation in pool.observations) == before
    )
    assert len(scores) == len(pool.observations)


def test_observations_without_variants_are_omitted_explicitly() -> None:
    observation = make_observation(
        kind=ObservationKind.OWNING_PACKAGE,
        payload=PackagePayload(path="pyproject.toml", kind="python"),
        source=SourceRef(path="pyproject.toml"),
    )
    pool = _pool((observation, ()))
    plan = _select(pool, _scores(pool), _policy())
    assert plan.selected == ()
    assert any(item.reason == OMISSION_NO_REPRESENTATION for item in plan.omitted)


def test_budget_without_room_omits_everything_explicitly() -> None:
    source = _source("src/a.py", 1, 10)
    pool = _pool(source)
    budget = DeliveryBudget(max_chars=40, envelope_chars=10)
    plan = _select(
        pool, _scores(pool), _policy(_exact_source_requirement()), budget=budget
    )
    assert plan.selected == ()
    assert plan.reserved == ()
    assert plan.measured_cost == 0
    assert any(item.reason == OMISSION_BUDGET for item in plan.omitted)
