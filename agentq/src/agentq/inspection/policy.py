"""Intent requirements and policy compilation.

Requirements precede scores: each requirement names acceptable evidence, a
strength, and one satisfaction rule. Every intent compiles its own collection
emphasis, and target kinds compile target-appropriate requirements: a
directory inspection reports structure and ownership without pretending that
an outline accounts for all editable source.

Capability lists are ordered recipes. When a language cannot provide semantic
references the planner falls back to syntactic mentions; the compiled
requirement stays the same, so the plan shows exactly which capability was
chosen and which gap remained.
"""

from __future__ import annotations

from collections.abc import Callable

from .contracts import (
    CandidateTarget,
    Capability,
    EvidencePolicy,
    EvidenceRequirement,
    EvidenceRole,
    InspectionRequest,
    Intent,
    LocationTarget,
    ObservationKind,
    PathKind,
    PathTarget,
    RangeTarget,
    RepresentationKind,
    RequirementRule,
    RequirementStrength,
    ResolutionResult,
    SymbolTarget,
)

POLICY_PROFILE = "intent-policy-v1"

INTENT_LIMITATIONS: dict[Intent, str] = {
    Intent.UNDERSTAND: "representative context, not exhaustive dependency analysis",
    Intent.EDIT: "does not authorize an edit or establish behavioral correctness",
    Intent.RENAME: "no proposed-name collision analysis or complete rename guarantee",
    Intent.REFACTOR: (
        "no proof of behavior preservation or automatic invariant discovery"
    ),
    Intent.IMPACT: (
        "potential impact around the target, not diff-conditioned or transitive "
        "impact completeness"
    ),
}

REQUIRED = RequirementStrength.REQUIRED
OPTIONAL = RequirementStrength.OPTIONAL

# Ordered capability recipes: the planner takes the first available entry.
REFERENCE_CAPABILITIES = (
    Capability.SEMANTIC_REFERENCES,
    Capability.SYNTACTIC_MENTIONS,
)


def compile_policy(
    request: InspectionRequest, resolution: ResolutionResult
) -> EvidencePolicy:
    """Compile the intent- and target-appropriate evidence requirements."""
    target = resolution.target
    intent = request.intent
    limitations = [INTENT_LIMITATIONS[intent]]
    requirements: tuple[EvidenceRequirement, ...] = ()
    match target:
        case SymbolTarget() | CandidateTarget():
            requirements = _symbol_requirements(intent)
        case LocationTarget():
            requirements = _declaration_requirements(
                intent, Capability.RESOLVE_LOCATION
            )
        case PathTarget():
            is_file = target.path_kind is PathKind.FILE
            requirements = _path_requirements(intent, is_file=is_file)
            if not is_file:
                limitations.append(
                    "directory target: structure and ownership are inspected; "
                    "source completeness for the directory's files is not claimed"
                )
        case RangeTarget():
            requirements = _range_requirements(intent)
    return EvidencePolicy(
        profile=POLICY_PROFILE,
        intent=intent,
        target_kind=target.kind,
        requirements=requirements,
        limitations=tuple(limitations),
    )


# ---------------------------------------------------------------------------
# Requirement builders
# ---------------------------------------------------------------------------


def _strength(required: bool) -> RequirementStrength:
    return REQUIRED if required else OPTIONAL


def _declaration(capability: Capability) -> EvidenceRequirement:
    return EvidenceRequirement(
        requirement_id="declaration_identity",
        role=EvidenceRole.DECLARATION,
        rule=RequirementRule.MINIMUM_EVIDENCE,
        strength=REQUIRED,
        description="the selected declaration must be represented in the bundle",
        capabilities=(capability,),
        acceptable_kinds=(ObservationKind.DECLARATION,),
        representations=(
            RepresentationKind.SIGNATURE,
            RepresentationKind.EXACT_SOURCE,
        ),
    )


def _source(*, required: bool) -> EvidenceRequirement:
    return EvidenceRequirement(
        requirement_id="target_source",
        role=EvidenceRole.TARGET_SOURCE,
        rule=RequirementRule.EXACT_SOURCE,
        strength=_strength(required),
        description=(
            "an exact source representation of the requested span must be selected"
        ),
        capabilities=(Capability.READ_SOURCE,),
        acceptable_kinds=(
            ObservationKind.SOURCE_WINDOW,
            ObservationKind.DECLARATION,
        ),
        representations=(RepresentationKind.EXACT_SOURCE,),
    )


def _references(*, required: bool) -> EvidenceRequirement:
    return EvidenceRequirement(
        requirement_id="representative_reference",
        role=EvidenceRole.REFERENCE,
        rule=RequirementRule.REPRESENTATIVE_EVIDENCE,
        strength=_strength(required),
        description=(
            "representative references; semantic references are preferred and "
            "syntactic mentions are the labeled fallback"
        ),
        capabilities=REFERENCE_CAPABILITIES,
        acceptable_kinds=(
            ObservationKind.SEMANTIC_REFERENCE,
            ObservationKind.SYNTACTIC_MENTION,
        ),
    )


def _implementations(*, required: bool) -> EvidenceRequirement:
    return EvidenceRequirement(
        requirement_id="implementations",
        role=EvidenceRole.IMPLEMENTATION,
        rule=RequirementRule.REPRESENTATIVE_EVIDENCE,
        strength=_strength(required),
        description="select an admissible implementation when one was acquired",
        capabilities=(Capability.IMPLEMENTATIONS,),
        acceptable_kinds=(ObservationKind.IMPLEMENTATION,),
    )


def _test_search(*, required: bool) -> EvidenceRequirement:
    return EvidenceRequirement(
        requirement_id="test_search",
        role=EvidenceRole.TEST,
        rule=RequirementRule.COLLECTION_OUTCOME,
        strength=_strength(required),
        description=(
            "the requested test-domain acquisition must have an explicit outcome; "
            "an empty completed search does not establish that no tests exist"
        ),
        capabilities=(Capability.LEXICAL_MENTIONS,),
        acceptable_kinds=(ObservationKind.TEST_MENTION,),
        domain="test",
    )


def _ownership(*, required: bool) -> EvidenceRequirement:
    return EvidenceRequirement(
        requirement_id="owning_package",
        role=EvidenceRole.OWNERSHIP,
        rule=RequirementRule.REPRESENTATIVE_EVIDENCE,
        strength=_strength(required),
        description="select owning-package evidence when it was acquired",
        capabilities=(Capability.OWNING_PACKAGE,),
        acceptable_kinds=(ObservationKind.OWNING_PACKAGE,),
    )


def _lexical_mentions(*, required: bool) -> EvidenceRequirement:
    return EvidenceRequirement(
        requirement_id="additional_lexical_mention",
        role=EvidenceRole.LEXICAL_MENTION,
        rule=RequirementRule.REPRESENTATIVE_EVIDENCE,
        strength=_strength(required),
        description=(
            "lexical mentions, labeled separately from semantic and syntactic "
            "reference evidence"
        ),
        capabilities=(Capability.LEXICAL_MENTIONS,),
        acceptable_kinds=(ObservationKind.LEXICAL_MENTION,),
    )


def _structure(*, required: bool) -> EvidenceRequirement:
    return EvidenceRequirement(
        requirement_id="target_structure",
        role=EvidenceRole.TARGET_STRUCTURE,
        rule=RequirementRule.MINIMUM_EVIDENCE,
        strength=_strength(required),
        description="the requested target structure must be represented",
        capabilities=(Capability.OUTLINE,),
        acceptable_kinds=(
            ObservationKind.OUTLINE,
            ObservationKind.SOURCE_WINDOW,
        ),
    )


def _requested_source(*, required: bool) -> EvidenceRequirement:
    return EvidenceRequirement(
        requirement_id="requested_source",
        role=EvidenceRole.TARGET_SOURCE,
        rule=RequirementRule.EXACT_SOURCE,
        strength=_strength(required),
        description="the requested source range must be selected exactly",
        capabilities=(Capability.READ_SOURCE,),
        acceptable_kinds=(ObservationKind.SOURCE_WINDOW,),
        representations=(RepresentationKind.EXACT_SOURCE,),
    )


# ---------------------------------------------------------------------------
# Intent profiles
# ---------------------------------------------------------------------------


def _declaration_requirements(
    intent: Intent, capability: Capability
) -> tuple[EvidenceRequirement, ...]:
    return (_declaration(capability), *_symbol_extras(intent))


def _symbol_requirements(intent: Intent) -> tuple[EvidenceRequirement, ...]:
    return (
        _declaration(Capability.FIND_DECLARATIONS),
        *_symbol_extras(intent),
    )


# Per-intent collection emphasis: (builder, required) in bundle order.
_INTENT_RECIPES: dict[
    Intent, tuple[tuple[Callable[..., EvidenceRequirement], bool], ...]
] = {
    Intent.UNDERSTAND: (
        (_source, True),
        (_references, False),
        (_implementations, False),
        (_ownership, False),
    ),
    Intent.EDIT: (
        (_source, True),
        (_references, False),
        (_test_search, True),
        (_ownership, False),
    ),
    Intent.RENAME: (
        (_source, False),
        (_references, True),
        (_lexical_mentions, True),
        (_test_search, True),
    ),
    Intent.REFACTOR: (
        (_source, True),
        (_references, False),
        (_implementations, True),
        (_test_search, True),
        (_ownership, False),
    ),
    Intent.IMPACT: (
        (_source, False),
        (_references, True),
        (_implementations, False),
        (_test_search, True),
        (_ownership, True),
    ),
}


def _symbol_extras(intent: Intent) -> tuple[EvidenceRequirement, ...]:
    return tuple(
        builder(required=required) for builder, required in _INTENT_RECIPES[intent]
    )


_PATH_SOURCE_INTENTS = frozenset({Intent.UNDERSTAND, Intent.EDIT, Intent.REFACTOR})
_PATH_OWNERSHIP_INTENTS = frozenset(
    {Intent.UNDERSTAND, Intent.EDIT, Intent.REFACTOR, Intent.IMPACT}
)
_RANGE_OWNERSHIP_INTENTS = frozenset({Intent.EDIT, Intent.REFACTOR, Intent.IMPACT})


def _path_requirements(
    intent: Intent, *, is_file: bool
) -> tuple[EvidenceRequirement, ...]:
    requirements: list[EvidenceRequirement] = [_structure(required=True)]
    if is_file and intent in _PATH_SOURCE_INTENTS:
        requirements.append(_source(required=True))
    if intent in _PATH_OWNERSHIP_INTENTS:
        requirements.append(_ownership(required=intent is Intent.IMPACT))
    return tuple(requirements)


def _range_requirements(intent: Intent) -> tuple[EvidenceRequirement, ...]:
    requirements: list[EvidenceRequirement] = [_requested_source(required=True)]
    if intent in _RANGE_OWNERSHIP_INTENTS:
        requirements.append(_ownership(required=False))
    return tuple(requirements)
