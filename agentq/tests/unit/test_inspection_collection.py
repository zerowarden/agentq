"""Collection recipes, truthful capability gaps, and plan-aware assessment."""

from __future__ import annotations

import pytest

from agentq.core import COMPLETE, SourceRef, typed_coverage
from agentq.inspection.acquisition import plan_collection
from agentq.inspection.budgeting import AcquisitionLimits
from agentq.inspection.capabilities import CapabilityRegistry
from agentq.inspection.contracts import (
    AcquisitionRecord,
    AvailabilityStatus,
    Capability,
    CapabilityAvailability,
    CollectionPlan,
    CollectionStatus,
    EvidencePolicy,
    EvidencePool,
    EvidenceRequest,
    EvidenceRequirement,
    EvidenceRole,
    Fidelity,
    InspectionRequest,
    Intent,
    ObservationKind,
    ReferencePayload,
    RepresentationKind,
    RequirementOmission,
    RequirementRule,
    RequirementStatus,
    RequirementStrength,
    ResolvedTarget,
    SelectedEvidence,
    SelectionMethod,
    SelectionPlan,
    SourceSpan,
    SourceWindowPayload,
    SymbolTarget,
    TargetKind,
    make_declaration_candidate,
    make_observation,
    make_variant,
)
from agentq.inspection.policy import compile_policy
from agentq.inspection.selection import assess_selected_evidence
from tests.support.inspection_fakes import FakeHandler, declaration_result, fake_context


def _resolution(*, evidence: tuple = ()) -> ResolvedTarget:
    declaration = make_declaration_candidate(
        provider="python",
        path="src/service.py",
        source_version="v1",
        kind="function",
        span=SourceSpan(start_line=1, end_line=2),
        signature="target()",
    )
    return ResolvedTarget(
        target=SymbolTarget(name="target", scopes=("src",)),
        method=SelectionMethod.UNIQUE_CANDIDATE,
        declaration=declaration,
        candidate_evidence=evidence,
    )


def _request(intent: str) -> InspectionRequest:
    return InspectionRequest(
        target=SymbolTarget(name="target", scopes=("src",)),
        intent=Intent.parse(intent),
        request_id="req-1",
    )


def _plan(
    intent: str,
    handlers: tuple[FakeHandler, ...],
    *,
    resolution: ResolvedTarget | None = None,
) -> CollectionPlan:
    request = _request(intent)
    resolved = resolution or _resolution()
    context = fake_context(handlers)
    report = CapabilityRegistry(handlers).describe(request, context)
    return plan_collection(
        compile_policy(request, resolved),
        resolved,
        report,
        AcquisitionLimits(),
        request_id=request.request_id,
        evidence_scopes=("src",),
    )


def _request_for(plan: CollectionPlan, requirement_id: str):
    return next(
        (item for item in plan.requests if item.requirement_id == requirement_id),
        None,
    )


def _omission_for(plan: CollectionPlan, requirement_id: str):
    return next(
        (item for item in plan.omissions if item.requirement_id == requirement_id),
        None,
    )


def _handler(*capabilities: Capability, name: str = "fake") -> FakeHandler:
    return FakeHandler(name=name, supported=frozenset(capabilities))


@pytest.mark.parametrize(
    "supported,expected",
    (
        pytest.param(
            (Capability.SEMANTIC_REFERENCES,),
            Capability.SEMANTIC_REFERENCES,
            id="semantic-only",
        ),
        pytest.param(
            (Capability.SYNTACTIC_MENTIONS,),
            Capability.SYNTACTIC_MENTIONS,
            id="syntactic-fallback",
        ),
        pytest.param(
            (Capability.SEMANTIC_REFERENCES, Capability.SYNTACTIC_MENTIONS),
            Capability.SEMANTIC_REFERENCES,
            id="preference-order",
        ),
    ),
)
def test_reference_recipe_chooses_the_first_available(supported, expected) -> None:
    plan = _plan("rename", (_handler(*supported),))
    reference = _request_for(plan, "representative_reference")
    assert reference is not None
    assert reference.capability is expected
    assert reference.subject is not None


def test_recipe_skips_an_unavailable_higher_preference() -> None:
    unavailable = FakeHandler(
        name="fake-ts",
        supported=frozenset({Capability.SEMANTIC_REFERENCES}),
        availability_by_capability={
            Capability.SEMANTIC_REFERENCES: CapabilityAvailability(
                available=False, reason="no tsconfig.json found"
            )
        },
    )
    plan = _plan(
        "rename",
        (unavailable, _handler(Capability.SYNTACTIC_MENTIONS, name="fake-py")),
    )
    reference = _request_for(plan, "representative_reference")
    assert reference is not None
    assert reference.capability is Capability.SYNTACTIC_MENTIONS


def test_missing_alternatives_produce_one_truthful_gap() -> None:
    plan = _plan("rename", ())
    omission = _omission_for(plan, "representative_reference")
    assert omission is not None
    assert "semantic_references" in omission.reason
    assert "syntactic_mentions" in omission.reason
    assert _request_for(plan, "representative_reference") is None


@pytest.mark.parametrize("intent", ("edit", "rename", "refactor", "impact"))
def test_test_search_carries_the_test_domain(intent) -> None:
    plan = _plan(intent, (_handler(Capability.LEXICAL_MENTIONS, name="repo"),))
    test_search = _request_for(plan, "test_search")
    assert test_search is not None
    assert test_search.domain == "test"
    assert test_search.capability is Capability.LEXICAL_MENTIONS


def test_understand_does_not_plan_test_search() -> None:
    plan = _plan("understand", (_handler(Capability.LEXICAL_MENTIONS),))
    assert _request_for(plan, "test_search") is None


def test_resolution_evidence_is_not_collected_twice() -> None:
    handler = FakeHandler(
        name="fake-python",
        supported=frozenset({Capability.FIND_DECLARATIONS}),
        results={Capability.FIND_DECLARATIONS: declaration_result("target")},
    )
    acquired = CapabilityRegistry((handler,)).acquire(
        EvidenceRequest(
            request_id="req-decl",
            capability=Capability.FIND_DECLARATIONS,
            target=SymbolTarget(name="target", scopes=("src",)),
        ),
        fake_context(handler),
    )[0]
    plan = _plan("edit", (handler,), resolution=_resolution(evidence=(acquired,)))
    assert _request_for(plan, "declaration_identity") is None


# ---------------------------------------------------------------------------
# Assessment: selected evidence plus plan-awareness
# ---------------------------------------------------------------------------


def _acquisition(
    capability: Capability, status: CollectionStatus = CollectionStatus.EMPTY
) -> AcquisitionRecord:
    return AcquisitionRecord(
        acquisition_id=f"acq-{capability.value}",
        capability=capability,
        provider="repository",
        provider_version=None,
        method=capability.value,
        effective_scope=(),
        coverage=typed_coverage(COMPLETE),
        status=status,
    )


def _empty_plan(*omissions: RequirementOmission) -> CollectionPlan:
    return CollectionPlan(
        profile="collection-v0",
        request_id="req-1",
        target=SymbolTarget(name="target"),
        omissions=omissions,
    )


def _empty_pool(
    *records: AcquisitionRecord, observations: tuple = (), variants: tuple = ()
) -> EvidencePool:
    return EvidencePool(
        request_id="req-1",
        acquisitions=records,
        observations=observations,
        variants=variants,
        coverage=typed_coverage(COMPLETE),
    )


def _assess(policy, plan, pool):
    return assess_selected_evidence(
        policy, plan, pool, SelectionPlan(profile="selection-v0")
    )


TEST_SEARCH_CASES = (
    pytest.param(
        None,
        (Capability.LEXICAL_MENTIONS, CollectionStatus.EMPTY),
        RequirementStatus.SATISFIED,
        None,
        id="completed-empty-is-an-outcome",
    ),
    pytest.param(
        (Capability.LEXICAL_MENTIONS, "ripgrep is required for lexical search"),
        None,
        RequirementStatus.UNSATISFIED,
        "ripgrep",
        id="missing-capability-is-not-absence",
    ),
)


@pytest.mark.parametrize("omission,record,expected,detail", TEST_SEARCH_CASES)
def test_test_search_outcomes(omission, record, expected, detail) -> None:
    policy = compile_policy(_request("edit"), _resolution())
    omissions = ()
    if omission is not None:
        capability, reason = omission
        omissions = (
            RequirementOmission(
                requirement_id="test_search",
                capability=capability,
                status=AvailabilityStatus.UNAVAILABLE,
                reason=reason,
            ),
        )
    records = (_acquisition(*record),) if record is not None else ()
    assessment = _assess(policy, _empty_plan(*omissions), _empty_pool(*records)).by_id(
        "test_search"
    )
    assert assessment is not None
    assert assessment.status is expected
    if detail is not None:
        assert detail in (assessment.detail or "")


REFERENCE_CASES = (
    pytest.param(
        None,
        (Capability.SEMANTIC_REFERENCES, CollectionStatus.EMPTY),
        RequirementStatus.SATISFIED,
        "no admissible evidence was acquired",
        id="empty-acquisition-is-explicit",
    ),
    pytest.param(
        (Capability.SEMANTIC_REFERENCES, "no adapter implements references"),
        None,
        RequirementStatus.UNSATISFIED,
        "no adapter implements references",
        id="missing-capability-is-not-vacuous",
    ),
    pytest.param(
        None,
        (Capability.SEMANTIC_REFERENCES, CollectionStatus.FAILED),
        RequirementStatus.UNSATISFIED,
        "unavailable or failed",
        id="failed-acquisition-is-unsatisfied",
    ),
)


@pytest.mark.parametrize("omission,record,expected,detail", REFERENCE_CASES)
def test_representative_reference_outcomes(omission, record, expected, detail) -> None:
    policy = compile_policy(_request("rename"), _resolution())
    omissions = ()
    if omission is not None:
        capability, reason = omission
        omissions = (
            RequirementOmission(
                requirement_id="representative_reference",
                capability=capability,
                status=AvailabilityStatus.UNSUPPORTED,
                reason=reason,
            ),
        )
    records = (_acquisition(*record),) if record is not None else ()
    assessment = _assess(policy, _empty_plan(*omissions), _empty_pool(*records)).by_id(
        "representative_reference"
    )
    assert assessment is not None
    assert assessment.status is expected
    assert detail in (assessment.detail or "")


def _source_evidence(
    fidelity: Fidelity,
) -> tuple[object, object]:
    span = SourceSpan(start_line=1, end_line=2)
    observation = make_observation(
        kind=ObservationKind.SOURCE_WINDOW,
        payload=SourceWindowPayload(text="def target():\n    return 1", span=span),
        source=SourceRef(path="src/service.py", start_line=1, end_line=2),
    )
    variant = make_variant(
        observation_id=observation.observation_id,
        representation=RepresentationKind.EXACT_SOURCE,
        fidelity=fidelity,
        source=observation.source,
        text="def target():\n    return 1",
        span=span,
    )
    return observation, variant


def test_truncated_source_never_satisfies_an_exact_source_requirement() -> None:
    policy = compile_policy(_request("edit"), _resolution())
    observation, variant = _source_evidence(Fidelity.BOUNDED)
    pool = _empty_pool(
        _acquisition(Capability.READ_SOURCE),
        observations=(observation,),
        variants=(variant,),
    )
    selection = SelectionPlan(
        profile="selection-v0",
        selected=(SelectedEvidence(variant=variant, reason="relevance"),),  # type: ignore[arg-type]
        measured_cost=1,
        budget_chars=10,
    )
    assessment = assess_selected_evidence(policy, _empty_plan(), pool, selection).by_id(
        "target_source"
    )
    assert assessment is not None
    assert assessment.status is RequirementStatus.UNSATISFIED
    assert assessment.detail == "no exact source representation was acquired"


def test_acquired_exact_source_must_be_selected() -> None:
    policy = compile_policy(_request("edit"), _resolution())
    observation, variant = _source_evidence(Fidelity.EXACT)
    pool = _empty_pool(
        _acquisition(Capability.READ_SOURCE),
        observations=(observation,),
        variants=(variant,),
    )
    omitted = _assess(policy, _empty_plan(), pool).by_id("target_source")
    assert omitted is not None
    assert omitted.status is RequirementStatus.UNSATISFIED
    assert (
        omitted.detail == "an exact source representation was acquired but not selected"
    )

    selection = SelectionPlan(
        profile="selection-v0",
        selected=(SelectedEvidence(variant=variant, reason="required"),),  # type: ignore[arg-type]
        measured_cost=1,
        budget_chars=10,
    )
    selected = assess_selected_evidence(policy, _empty_plan(), pool, selection).by_id(
        "target_source"
    )
    assert selected is not None
    assert selected.status is RequirementStatus.SATISFIED
    assert selected.supporting == (observation.observation_id,)  # type: ignore[union-attr]


def test_domain_match_decides_admissibility() -> None:
    requirement = EvidenceRequirement(
        requirement_id="test_reference",
        role=EvidenceRole.REFERENCE,
        rule=RequirementRule.REPRESENTATIVE_EVIDENCE,
        strength=RequirementStrength.REQUIRED,
        capabilities=(Capability.SEMANTIC_REFERENCES,),
        acceptable_kinds=(ObservationKind.SEMANTIC_REFERENCE,),
        domain="test",
    )
    policy = EvidencePolicy(
        profile="policy-test",
        intent=Intent.EDIT,
        target_kind=TargetKind.SYMBOL,
        requirements=(requirement,),
    )
    plan = _empty_plan()
    source_reference = make_observation(
        kind=ObservationKind.SEMANTIC_REFERENCE,
        payload=ReferencePayload(relationship="reference", text="target()"),
        source=SourceRef(path="src/use.py", start_line=1, end_line=1),
    )
    unlabeled = _assess(
        policy,
        plan,
        _empty_pool(
            _acquisition(Capability.SEMANTIC_REFERENCES),
            observations=(source_reference,),
        ),
    ).by_id("test_reference")
    assert unlabeled is not None
    assert unlabeled.status is RequirementStatus.SATISFIED
    assert unlabeled.detail == "no admissible evidence was acquired"

    test_reference = make_observation(
        kind=ObservationKind.SEMANTIC_REFERENCE,
        payload=ReferencePayload(
            relationship="reference", text="target()", domain="test"
        ),
        source=SourceRef(path="tests/test_use.py", start_line=1, end_line=1),
    )
    variant = make_variant(
        observation_id=test_reference.observation_id,
        representation=RepresentationKind.REFERENCE,
        fidelity=Fidelity.BOUNDED,
        source=test_reference.source,
        text="target()",
    )
    selected = SelectionPlan(
        profile="selection-v0",
        selected=(SelectedEvidence(variant=variant, reason="required"),),
        measured_cost=1,
        budget_chars=10,
    )
    supported = assess_selected_evidence(
        policy,
        plan,
        _empty_pool(
            _acquisition(Capability.SEMANTIC_REFERENCES),
            observations=(source_reference, test_reference),
        ),
        selected,
    ).by_id("test_reference")
    assert supported is not None
    assert supported.status is RequirementStatus.SATISFIED
    assert supported.supporting == (test_reference.observation_id,)
