"""Selection: required evidence, diversity, overlap, and serialized cost."""

from __future__ import annotations

from dataclasses import replace

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
    MentionPayload,
    Observation,
    ObservationKind,
    PackagePayload,
    ReferencePayload,
    RepresentationKind,
    RequirementRule,
    RequirementStatus,
    RequirementStrength,
    ScoreContribution,
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
    OMISSION_NO_VALUE,
    OMISSION_OVERLAP,
    OMISSION_SAME_FILE,
    REASON_RELEVANCE,
    REASON_REQUIRED,
    REASON_ROLE,
    SelectionProfile,
    assess_selected_evidence,
    select_evidence,
)
from tests.support.inspection_fixtures import collection_plan

CHALLENGER = SelectionProfile(
    profile="selection-test-challenger",
    variant_fallback=True,
    skip_zero_value=True,
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


def _lexical(
    path: str, line: int = 4
) -> tuple[Observation, tuple[EvidenceVariant, ...]]:
    text = f"target in {path}"
    observation = make_observation(
        kind=ObservationKind.LEXICAL_MENTION,
        payload=MentionPayload(text=text),
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


def _declaration_with(
    path: str,
    variants: tuple[tuple[RepresentationKind, Fidelity, SourceSpan, str], ...],
) -> tuple[Observation, tuple[EvidenceVariant, ...]]:
    """One declaration whose representations may cover different extents."""
    anchor = variants[0][2]
    observation = make_observation(
        kind=ObservationKind.DECLARATION,
        payload=DeclarationPayload(
            name="target",
            kind="function",
            signature="target()",
            span=anchor,
        ),
        source=SourceRef(
            path=path, start_line=anchor.start_line, end_line=anchor.end_line
        ),
    )
    return observation, tuple(
        make_variant(
            observation_id=observation.observation_id,
            representation=representation,
            fidelity=fidelity,
            source=observation.source,
            text=text,
            span=span,
        )
        for representation, fidelity, span, text in variants
    )


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


def _scored_with(
    pool: EvidencePool, totals: dict[str, int]
) -> tuple[ScoredEvidence, ...]:
    """Replace fixture scores with explicit totals for ordering tests."""
    scored: list[ScoredEvidence] = []
    for item in _scores(pool):
        total = totals.get(item.observation_id, item.score.total)
        scored.append(
            replace(
                item,
                score=replace(
                    item.score,
                    total=total,
                    contributions=(ScoreContribution(name="test", value=total),),
                ),
            )
        )
    return tuple(scored)


def _relevance_cost(variant: EvidenceVariant, score: int) -> int:
    return selected_cost(
        SelectedEvidence(
            variant=variant,
            reason=REASON_RELEVANCE,
            score=score,
            contributions=(ScoreContribution(name="test", value=score),),
        ),
        "text",
    )


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


def test_role_representatives_respect_the_per_file_limit() -> None:
    reference = _reference("src/one.py", line=4)
    lexical = _lexical("src/one.py", line=8)
    test_mention = _reference("src/one.py", line=12, domain="test")
    ownership = _ownership("pyproject.toml")
    pool = _pool(reference, lexical, test_mention, ownership)
    profile = SelectionProfile(
        profile="selection-test-file-cap",
        per_file_limit=1,
        role_diversity=True,
        fill_by_score=True,
    )
    plan = _select(pool, _scores(pool), _policy(), profile=profile)
    paths = [item.variant.source.path for item in plan.selected]
    assert paths.count("src/one.py") == 1
    assert any(item.reason == OMISSION_SAME_FILE for item in plan.omitted)


def test_required_source_does_not_consume_the_optional_file_quota() -> None:
    source = _source("src/a.py", 1, 3)
    references = [_reference("src/a.py", line=line) for line in (10, 20, 30)]
    pool = _pool(source, *references)
    plan = _select(pool, _scores(pool), _policy(_exact_source_requirement()))
    selected_references = {
        item.observation_id
        for item in plan.selected
        if item.variant.representation is RepresentationKind.REFERENCE
    }
    assert len(selected_references) == DEFAULT_SELECTION.per_file_limit
    assert any(item.reason == OMISSION_SAME_FILE for item in plan.omitted)


def test_fill_prefers_relevance_per_serialized_cost() -> None:
    """Several small artifacts can beat one large higher-scoring artifact."""
    large = _source("src/large.py", 1, 8)
    smalls = [_reference(f"src/use{index}.py") for index in range(4)]
    pool = _pool(large, *smalls)
    scores = _scored_with(
        pool,
        {
            large[0].observation_id: 8,
            **{small[0].observation_id: 6 for small in smalls},
        },
    )
    large_cost = _relevance_cost(large[1][0], 8)
    small_cost = _relevance_cost(smalls[0][1][0], 6)
    assert small_cost < large_cost <= small_cost * len(smalls)
    budget = DeliveryBudget(max_chars=small_cost * len(smalls) + 10, envelope_chars=10)
    plan = _select(
        pool,
        scores,
        _policy(),
        budget=budget,
        profile=SelectionProfile(
            profile="selection-ratio-test",
            reserve_required=False,
            role_diversity=False,
        ),
    )
    assert set(_selected_ids(plan)) == {small[0].observation_id for small in smalls}
    assert any(
        item.observation_id == large[0].observation_id
        and item.reason == OMISSION_BUDGET
        for item in plan.omitted
    )


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


def test_unstable_observations_cannot_be_reserved_or_selected() -> None:
    source = _source("src/a.py", 1, 3)
    pool = _pool(source)
    unstable = replace(pool, unstable_observation_ids=(source[0].observation_id,))
    plan = _select(unstable, _scores(unstable), _policy(_exact_source_requirement()))
    assert plan.selected == ()
    assert plan.reserved == ()
    assert any(item.reason == "source_unstable" for item in plan.omitted)


def test_stable_alternative_represents_a_requirement_an_unstable_one_cannot() -> None:
    unstable_source = _source("src/a.py", 1, 3)
    stable_source = _source("src/b.py", 1, 3)
    pool = _pool(unstable_source, stable_source)
    unstable = replace(
        pool, unstable_observation_ids=(unstable_source[0].observation_id,)
    )
    plan = _select(unstable, _scores(unstable), _policy(_exact_source_requirement()))
    assert [item.observation_id for item in plan.selected] == [
        stable_source[0].observation_id
    ]
    assert plan.reserved == ("target_source",)
    assert not any(
        item.observation_id == unstable_source[0].observation_id
        for item in plan.selected
    )


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


def _two_representations(
    path: str, start: int, end: int
) -> tuple[Observation, tuple[EvidenceVariant, ...]]:
    observation, (exact,) = _source(path, start, end)
    excerpt = make_variant(
        observation_id=observation.observation_id,
        representation=RepresentationKind.EXCERPT,
        fidelity=Fidelity.BOUNDED,
        source=observation.source,
        text="line 1",
        span=SourceSpan(start_line=start, end_line=end),
    )
    return observation, (exact, excerpt)


def test_variant_fallback_uses_a_smaller_representation_that_fits() -> None:
    implementation = _two_representations("src/service.py", 1, 40)
    exact, excerpt = implementation[1]
    pool = _pool(implementation)
    scores = _scored_with(pool, {implementation[0].observation_id: 5})
    budget = DeliveryBudget(
        max_chars=selected_cost(
            SelectedEvidence(variant=excerpt, reason=REASON_ROLE, score=0), "text"
        )
        + 20,
        envelope_chars=10,
    )
    baseline = _select(pool, scores, _policy(), budget=budget)
    assert baseline.selected == ()
    assert any(item.reason == OMISSION_BUDGET for item in baseline.omitted)
    fallback = _select(pool, scores, _policy(), budget=budget, profile=CHALLENGER)
    assert [item.variant.variant_id for item in fallback.selected] == [
        excerpt.variant_id
    ]
    assert fallback.measured_cost < selected_cost(
        SelectedEvidence(variant=exact, reason=REASON_ROLE, score=0), "text"
    )


def test_variant_fallback_never_satisfies_exact_source_with_an_excerpt() -> None:
    implementation = _two_representations("src/service.py", 1, 40)
    excerpt = implementation[1][1]
    pool = _pool(implementation)
    budget = DeliveryBudget(
        max_chars=selected_cost(
            SelectedEvidence(variant=excerpt, reason=REASON_ROLE, score=0), "text"
        )
        + 20,
        envelope_chars=10,
    )
    policy = _policy(_exact_source_requirement())
    scores = _scored_with(pool, {implementation[0].observation_id: 5})
    plan = _select(pool, scores, policy, budget=budget, profile=CHALLENGER)
    assert plan.reserved == ()
    assert [item.variant.variant_id for item in plan.selected] == [
        excerpt.variant_id
    ]
    assessment = assess_selected_evidence(policy, collection_plan(), pool, plan)
    found = assessment.by_id("target_source")
    assert found is not None
    assert found.status is RequirementStatus.UNSATISFIED


def _minimum_requirement(
    requirement_id: str, *representations: RepresentationKind
) -> EvidenceRequirement:
    return EvidenceRequirement(
        requirement_id=requirement_id,
        role=EvidenceRole.DECLARATION,
        rule=RequirementRule.MINIMUM_EVIDENCE,
        strength=RequirementStrength.REQUIRED,
        capabilities=(Capability.FIND_DECLARATIONS,),
        acceptable_kinds=(ObservationKind.DECLARATION,),
        representations=representations,
    )


def test_required_upgrade_replaces_a_short_signature_with_a_longer_span() -> None:
    short = SourceSpan(start_line=1, end_line=1)
    long = SourceSpan(start_line=1, end_line=10)
    declaration = _declaration_with(
        "src/a.py",
        (
            (RepresentationKind.SIGNATURE, Fidelity.EXACT, short, "target()"),
            (RepresentationKind.EXACT_SOURCE, Fidelity.BOUNDED, long, "line 1"),
        ),
    )
    signature, exact = declaration[1]
    pool = _pool(declaration)
    policy = _policy(
        _minimum_requirement(
            "declaration_identity",
            RepresentationKind.SIGNATURE,
            RepresentationKind.EXACT_SOURCE,
        ),
        _minimum_requirement("declaration_source", RepresentationKind.EXACT_SOURCE),
    )
    signature_cost = selected_cost(
        SelectedEvidence(variant=signature, reason=REASON_REQUIRED, score=0), "text"
    )
    exact_cost = selected_cost(
        SelectedEvidence(variant=exact, reason=REASON_REQUIRED, score=0), "text"
    )
    budget = DeliveryBudget(max_chars=exact_cost + 10, envelope_chars=10)
    assert exact_cost <= budget.available_chars()
    baseline = _select(pool, _scores(pool), policy, budget=budget)
    assert [item.variant.variant_id for item in baseline.selected] == [
        signature.variant_id
    ]
    assert baseline.reserved == ("declaration_identity",)
    assert baseline.measured_cost == signature_cost
    upgraded = _select(pool, _scores(pool), policy, budget=budget, profile=CHALLENGER)
    assert [item.variant.variant_id for item in upgraded.selected] == [
        exact.variant_id
    ]
    assert upgraded.reserved == ("declaration_identity", "declaration_source")
    assert upgraded.measured_cost == exact_cost
    assessment = assess_selected_evidence(policy, collection_plan(), pool, upgraded)
    assert (
        assessment.by_id("declaration_identity").status
        is RequirementStatus.SATISFIED
    )
    assert (
        assessment.by_id("declaration_source").status
        is RequirementStatus.SATISFIED
    )


def test_required_upgrade_keeps_previously_satisfied_requirements() -> None:
    short = SourceSpan(start_line=1, end_line=1)
    long = SourceSpan(start_line=1, end_line=10)
    declaration = _declaration_with(
        "src/a.py",
        (
            (RepresentationKind.SIGNATURE, Fidelity.EXACT, short, "target()"),
            (RepresentationKind.EXACT_SOURCE, Fidelity.EXACT, long, "line 1"),
        ),
    )
    signature, exact = declaration[1]
    pool = _pool(declaration)
    policy = _policy(
        _minimum_requirement("declaration_identity", RepresentationKind.SIGNATURE),
        EvidenceRequirement(
            requirement_id="declaration_source",
            role=EvidenceRole.DECLARATION,
            rule=RequirementRule.EXACT_SOURCE,
            strength=RequirementStrength.REQUIRED,
            capabilities=(Capability.FIND_DECLARATIONS,),
            acceptable_kinds=(ObservationKind.DECLARATION,),
            representations=(RepresentationKind.EXACT_SOURCE,),
        ),
    )
    exact_cost = selected_cost(
        SelectedEvidence(variant=exact, reason=REASON_REQUIRED, score=0), "text"
    )
    budget = DeliveryBudget(max_chars=exact_cost + 10, envelope_chars=10)
    plan = _select(pool, _scores(pool), policy, budget=budget, profile=CHALLENGER)
    # Upgrading to the exact source would break the identity requirement that
    # the signature already satisfies, so the replacement is refused.
    assert [item.variant.variant_id for item in plan.selected] == [
        signature.variant_id
    ]
    assert plan.reserved == ("declaration_identity",)
    assessment = assess_selected_evidence(policy, collection_plan(), pool, plan)
    assert (
        assessment.by_id("declaration_identity").status
        is RequirementStatus.SATISFIED
    )
    assert (
        assessment.by_id("declaration_source").status
        is RequirementStatus.UNSATISFIED
    )


def test_upgrade_recomputes_coverage_for_the_new_extent() -> None:
    short = SourceSpan(start_line=1, end_line=1)
    long = SourceSpan(start_line=1, end_line=10)
    declaration = _declaration_with(
        "src/a.py",
        (
            (RepresentationKind.SIGNATURE, Fidelity.EXACT, short, "target()"),
            (RepresentationKind.EXACT_SOURCE, Fidelity.BOUNDED, long, "line 1"),
        ),
    )
    overlapping = _declaration("src/a.py", 5)
    pool = _pool(declaration, overlapping)
    policy = _policy(
        _minimum_requirement(
            "declaration_identity",
            RepresentationKind.SIGNATURE,
            RepresentationKind.EXACT_SOURCE,
        ),
        _minimum_requirement("declaration_source", RepresentationKind.EXACT_SOURCE),
    )
    scores = _scored_with(
        pool,
        {
            declaration[0].observation_id: 5,
            overlapping[0].observation_id: 1,
        },
    )
    breakdown = next(
        item
        for item in scores
        if item.observation_id == declaration[0].observation_id
    )
    exact_cost = selected_cost(
        SelectedEvidence(
            variant=declaration[1][1],
            reason=REASON_REQUIRED,
            score=breakdown.score.total,
            contributions=breakdown.score.contributions,
        ),
        "text",
    )
    budget = DeliveryBudget(max_chars=exact_cost + 10, envelope_chars=10)
    plan = _select(pool, scores, policy, budget=budget, profile=CHALLENGER)
    assert declaration[1][1].variant_id in {
        item.variant.variant_id for item in plan.selected
    }
    # The replacement covers lines 1-10, so the declaration at line 5 overlaps
    # the upgraded extent and must be omitted; stale coverage would let it in.
    assert overlapping[0].observation_id not in _selected_ids(plan)
    assert any(
        item.observation_id == overlapping[0].observation_id
        and item.reason == OMISSION_OVERLAP
        for item in plan.omitted
    )


def test_fallback_rechecks_constraints_on_the_actual_variant() -> None:
    covered = _source("src/a.py", 1, 10)
    candidate_observation = make_observation(
        kind=ObservationKind.SOURCE_WINDOW,
        payload=SourceWindowPayload(
            text="line 5\nline 6",
            span=SourceSpan(start_line=5, end_line=15),
        ),
        source=SourceRef(path="src/a.py", start_line=5, end_line=15),
    )
    overlapping = make_variant(
        observation_id=candidate_observation.observation_id,
        representation=RepresentationKind.EXACT_SOURCE,
        fidelity=Fidelity.EXACT,
        source=candidate_observation.source,
        text="line 5\nline 6",
        span=SourceSpan(start_line=5, end_line=15),
    )
    clear = make_variant(
        observation_id=candidate_observation.observation_id,
        representation=RepresentationKind.EXCERPT,
        fidelity=Fidelity.BOUNDED,
        source=candidate_observation.source,
        text="line 5",
        span=SourceSpan(start_line=20, end_line=25),
    )
    candidate = (candidate_observation, (overlapping, clear))
    pool = _pool(covered, candidate)
    scores = _scored_with(
        pool,
        {
            covered[0].observation_id: 5,
            candidate_observation.observation_id: 1,
        },
    )
    profile = SelectionProfile(
        profile="selection-test-fallback",
        reserve_required=False,
        role_diversity=False,
        variant_fallback=True,
    )
    plan = _select(pool, scores, _policy(), profile=profile)
    assert [item.variant.variant_id for item in plan.selected] == [
        covered[1][0].variant_id,
        clear.variant_id,
    ]
    assert not any(
        item.observation_id == candidate_observation.observation_id
        and item.reason == OMISSION_OVERLAP
        for item in plan.omitted
    )


def test_zero_value_optional_evidence_is_not_filler() -> None:
    reference = _reference("src/use.py")
    pool = _pool(reference)
    scores = _scored_with(pool, {reference[0].observation_id: 0})
    profile = replace(
        CHALLENGER, reserve_required=False, role_diversity=False
    )
    baseline = _select(pool, scores, _policy())
    challenger = _select(pool, scores, _policy(), profile=profile)
    assert [item.observation_id for item in baseline.selected] == [
        reference[0].observation_id
    ]
    assert challenger.selected == ()
    assert any(item.reason == OMISSION_NO_VALUE for item in challenger.omitted)


def test_zero_value_optional_evidence_cannot_enter_through_role_diversity() -> None:
    lexical = _lexical("src/notes.md")
    pool = _pool(lexical)
    scores = _scored_with(pool, {lexical[0].observation_id: 0})
    assert CHALLENGER.role_diversity and CHALLENGER.skip_zero_value
    plan = _select(pool, scores, _policy(), profile=CHALLENGER)
    assert plan.selected == ()
    assert any(item.reason == OMISSION_NO_VALUE for item in plan.omitted)


def test_positive_value_optional_evidence_still_represents_its_role() -> None:
    lexical = _lexical("src/notes.md")
    pool = _pool(lexical)
    scores = _scored_with(pool, {lexical[0].observation_id: 3})
    plan = _select(pool, scores, _policy(), profile=CHALLENGER)
    assert [item.observation_id for item in plan.selected] == [
        lexical[0].observation_id
    ]
    assert plan.selected[0].reason == REASON_ROLE


def test_role_diversity_tries_another_file_after_a_quota_rejection() -> None:
    reference = _reference("src/a.py")
    capped = _lexical("src/a.py", line=10)
    alternative = _lexical("src/b.py")
    pool = _pool(reference, capped, alternative)
    scores = _scored_with(
        pool,
        {
            reference[0].observation_id: 10,
            capped[0].observation_id: 5,
            alternative[0].observation_id: 3,
        },
    )
    plan = _select(
        pool,
        scores,
        _policy(),
        profile=replace(CHALLENGER, per_file_limit=1, fill_by_score=False),
    )
    assert [item.observation_id for item in plan.selected] == [
        reference[0].observation_id,
        alternative[0].observation_id,
    ]
    assert all(item.reason == REASON_ROLE for item in plan.selected)
    assert any(
        item.observation_id == capped[0].observation_id
        and item.reason == OMISSION_SAME_FILE
        for item in plan.omitted
    )


def test_role_diversity_tries_a_smaller_observation_after_a_budget_rejection() -> None:
    large = _source("src/large.py", 1, 100)
    small = _source("src/small.py", 1, 1)
    pool = _pool(large, small)
    scores = _scored_with(
        pool, {large[0].observation_id: 5, small[0].observation_id: 3}
    )
    plan = _select(
        pool,
        scores,
        _policy(),
        budget=DeliveryBudget(max_chars=500, envelope_chars=10),
        profile=replace(CHALLENGER, fill_by_score=False),
    )
    assert [item.observation_id for item in plan.selected] == [small[0].observation_id]
    assert plan.selected[0].reason == REASON_ROLE


def test_required_zero_value_source_evidence_is_still_selectable() -> None:
    source = _source("src/a.py", 1, 3)
    pool = _pool(source)
    scores = _scored_with(pool, {source[0].observation_id: 0})
    plan = _select(
        pool, scores, _policy(_exact_source_requirement()), profile=CHALLENGER
    )
    assert [item.observation_id for item in plan.selected] == [
        source[0].observation_id
    ]
    assert plan.selected[0].reason == REASON_REQUIRED
    assert plan.reserved == ("target_source",)


def test_overlapping_excerpts_stay_omitted_under_fallback() -> None:
    first = _source("src/a.py", 1, 10)
    second = _source("src/a.py", 5, 15)
    pool = _pool(first, second)
    scores = _scored_with(
        pool, {first[0].observation_id: 6, second[0].observation_id: 5}
    )
    plan = _select(pool, scores, _policy(), profile=CHALLENGER)
    assert first[0].observation_id in _selected_ids(plan)
    assert second[0].observation_id not in _selected_ids(plan)
    assert any(item.reason == OMISSION_OVERLAP for item in plan.omitted)


def test_required_source_still_bypasses_the_optional_quota_under_fallback() -> None:
    source = _source("src/a.py", 1, 3)
    references = [_reference("src/a.py", line=line) for line in (10, 20, 30)]
    pool = _pool(source, *references)
    plan = _select(
        pool, _scores(pool), _policy(_exact_source_requirement()), profile=CHALLENGER
    )
    assert source[0].observation_id in _selected_ids(plan)
    assert plan.reserved == ("target_source",)
    assert any(item.reason == OMISSION_SAME_FILE for item in plan.omitted)


def test_jointly_infeasible_requirements_stay_explicit() -> None:
    source = _source("src/a.py", 1, 20)
    test_mention = _reference("tests/test_orders.py", 4, domain="test")
    pool = _pool(source, test_mention)
    test_requirement = EvidenceRequirement(
        requirement_id="test_search",
        role=EvidenceRole.TEST,
        rule=RequirementRule.MINIMUM_EVIDENCE,
        strength=RequirementStrength.REQUIRED,
        capabilities=(Capability.LEXICAL_MENTIONS,),
        acceptable_kinds=(ObservationKind.TEST_MENTION,),
        domain="test",
    )
    source_cost = selected_cost(
        SelectedEvidence(variant=source[1][0], reason=REASON_REQUIRED, score=0), "text"
    )
    budget = DeliveryBudget(max_chars=source_cost + 20, envelope_chars=10)
    policy = _policy(_exact_source_requirement(), test_requirement)
    plan = _select(pool, _scores(pool), policy, budget=budget, profile=CHALLENGER)
    assert plan.reserved == ("target_source",)
    assert any(
        item.observation_id == test_mention[0].observation_id
        and item.reason == OMISSION_BUDGET
        for item in plan.omitted
    )
    assessment = assess_selected_evidence(policy, collection_plan(), pool, plan)
    found = assessment.by_id("test_search")
    assert found is not None
    assert found.status is RequirementStatus.UNSATISFIED
    assert found.detail is not None
