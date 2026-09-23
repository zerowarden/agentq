"""Intent- and target-dependent policy compilation (parametrized)."""

from __future__ import annotations

import pytest

from agentq.inspection.contracts import (
    CandidateTarget,
    Capability,
    InspectionRequest,
    Intent,
    LocationTarget,
    PathKind,
    PathTarget,
    RangeTarget,
    RequirementStrength,
    ResolvedTarget,
    SelectionMethod,
    SourceSpan,
    SymbolTarget,
    TargetKind,
    make_declaration_candidate,
)
from agentq.inspection.policy import POLICY_PROFILE, compile_policy

REQUIRED = RequirementStrength.REQUIRED
OPTIONAL = RequirementStrength.OPTIONAL


def _candidate() -> object:
    return make_declaration_candidate(
        provider="fake",
        path="src/a.ts",
        source_version="v1",
        kind="function",
        span=SourceSpan(start_line=1, end_line=2),
        signature="function target()",
    )


def _resolved(target) -> ResolvedTarget:
    if isinstance(target, (PathTarget, RangeTarget)):
        return ResolvedTarget(target=target, method=SelectionMethod.DIRECT_TARGET)
    return ResolvedTarget(
        target=target,
        method=SelectionMethod.UNIQUE_CANDIDATE,
        declaration=_candidate(),  # type: ignore[arg-type]
    )


def _policy(intent: str, target) -> object:
    request = InspectionRequest(target=target, intent=Intent.parse(intent))
    return compile_policy(request, _resolved(target))


SYMBOL_PROFILES = (
    pytest.param(
        "understand",
        {"target_source": REQUIRED},
        ("representative_reference", "implementations", "owning_package"),
        ("test_search", "additional_lexical_mention"),
        id="understand",
    ),
    pytest.param(
        "edit",
        {"target_source": REQUIRED, "test_search": REQUIRED},
        ("representative_reference", "owning_package"),
        ("additional_lexical_mention",),
        id="edit",
    ),
    pytest.param(
        "rename",
        {
            "representative_reference": REQUIRED,
            "additional_lexical_mention": REQUIRED,
            "test_search": REQUIRED,
        },
        ("target_source",),
        ("implementations", "owning_package"),
        id="rename",
    ),
    pytest.param(
        "refactor",
        {
            "target_source": REQUIRED,
            "implementations": REQUIRED,
            "test_search": REQUIRED,
        },
        ("representative_reference", "owning_package"),
        ("additional_lexical_mention",),
        id="refactor",
    ),
    pytest.param(
        "impact",
        {
            "representative_reference": REQUIRED,
            "test_search": REQUIRED,
            "owning_package": REQUIRED,
        },
        ("target_source", "implementations"),
        ("additional_lexical_mention",),
        id="impact",
    ),
)


@pytest.mark.parametrize("intent,required,optional,absent", SYMBOL_PROFILES)
def test_symbol_intent_profiles(intent, required, optional, absent) -> None:
    policy = _policy(intent, SymbolTarget(name="target"))
    assert policy.profile == POLICY_PROFILE
    for requirement_id, strength in required.items():
        requirement = policy.requirement(requirement_id)
        assert requirement is not None, requirement_id
        assert requirement.strength is strength
    for requirement_id in optional:
        requirement = policy.requirement(requirement_id)
        assert requirement is not None, requirement_id
        assert requirement.strength is OPTIONAL
    for requirement_id in absent:
        assert policy.requirement(requirement_id) is None, requirement_id


RECIPE_CASES = (
    pytest.param(
        "rename",
        "representative_reference",
        (Capability.SEMANTIC_REFERENCES, Capability.SYNTACTIC_MENTIONS),
        id="references-prefer-semantic",
    ),
    pytest.param(
        "rename",
        "additional_lexical_mention",
        (Capability.LEXICAL_MENTIONS,),
        id="lexical-mentions-are-separate",
    ),
    pytest.param(
        "edit",
        "test_search",
        (Capability.LEXICAL_MENTIONS,),
        id="tests-use-lexical-acquisition",
    ),
    pytest.param(
        "edit",
        "target_source",
        (Capability.READ_SOURCE,),
        id="source-reads-repository",
    ),
)


@pytest.mark.parametrize("intent,requirement_id,capabilities", RECIPE_CASES)
def test_requirement_recipes(intent, requirement_id, capabilities) -> None:
    requirement = _policy(intent, SymbolTarget(name="target")).requirement(
        requirement_id
    )
    assert requirement is not None
    assert requirement.capabilities == capabilities


def test_location_target_resolves_through_the_location_capability() -> None:
    policy = _policy("understand", LocationTarget(path="src/a.ts", line=1, column=1))
    declaration = policy.requirement("declaration_identity")
    assert declaration is not None
    assert declaration.capabilities == (Capability.RESOLVE_LOCATION,)


def test_candidate_target_uses_the_symbol_declaration_recipe() -> None:
    target = CandidateTarget(candidate_id="cand-1", symbol="target")
    declaration = _policy("edit", target).requirement("declaration_identity")
    assert declaration is not None
    assert declaration.capabilities == (Capability.FIND_DECLARATIONS,)


TARGET_CASES = (
    pytest.param(
        PathTarget(path="src/orders", path_kind=PathKind.DIRECTORY),
        ("target_structure", "owning_package"),
        ("target_source", "requested_source", "declaration_identity"),
        id="directory",
    ),
    pytest.param(
        PathTarget(path="src/orders.ts", path_kind=PathKind.FILE),
        ("target_structure", "target_source", "owning_package"),
        ("requested_source", "declaration_identity"),
        id="file",
    ),
    pytest.param(
        RangeTarget(path="src/a.ts", ranges=(SourceSpan(start_line=4, end_line=8),)),
        ("requested_source", "owning_package"),
        ("target_structure", "declaration_identity", "target_source"),
        id="range",
    ),
)


@pytest.mark.parametrize("target,present,absent", TARGET_CASES)
def test_target_kind_requirements(target, present, absent) -> None:
    policy = _policy("refactor", target)
    for requirement_id in present:
        assert policy.requirement(requirement_id) is not None, requirement_id
    for requirement_id in absent:
        assert policy.requirement(requirement_id) is None, requirement_id


def test_directory_target_never_claims_source_completeness() -> None:
    policy = _policy("edit", PathTarget(path="src", path_kind=PathKind.DIRECTORY))
    assert policy.requirement("target_structure") is not None
    assert policy.requirement("target_source") is None
    assert policy.requirement("requested_source") is None
    assert any("directory target" in item for item in policy.limitations)


def test_file_target_has_no_directory_limitation() -> None:
    policy = _policy("edit", PathTarget(path="src/orders.ts", path_kind=PathKind.FILE))
    assert not any("directory target" in item for item in policy.limitations)


def test_extensionless_file_target_requires_source() -> None:
    policy = _policy("edit", PathTarget(path="Dockerfile", path_kind=PathKind.FILE))
    assert policy.requirement("target_source") is not None
    assert not any("directory target" in item for item in policy.limitations)


@pytest.mark.parametrize(
    "target_kind,target",
    (
        pytest.param(TargetKind.SYMBOL, SymbolTarget(name="target"), id="symbol"),
        pytest.param(
            TargetKind.PATH,
            PathTarget(path="src", path_kind=PathKind.DIRECTORY),
            id="path",
        ),
    ),
)
def test_policy_records_the_target_kind(target_kind, target) -> None:
    assert _policy("understand", target).target_kind is target_kind
