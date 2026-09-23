"""Intent requirements and policy compilation.

Requirements precede scores: each requirement names acceptable evidence, a
strength, and one satisfaction rule. The M1.1 baseline compiles
target-appropriate requirements shared by all intents; the per-intent emphasis
profiles replace this baseline in M1.3 without changing this module's contract.
"""

from __future__ import annotations

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
    PathTarget,
    RangeTarget,
    RepresentationKind,
    RequirementRule,
    RequirementStrength,
    ResolutionResult,
    SymbolTarget,
)

POLICY_PROFILE = "intent-policy-v0"

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


def compile_policy(
    request: InspectionRequest, resolution: ResolutionResult
) -> EvidencePolicy:
    """Compile the evidence requirements for one resolved request."""
    target = resolution.target
    if isinstance(target, (SymbolTarget, CandidateTarget)):
        requirements = _symbol_requirements()
    elif isinstance(target, PathTarget):
        requirements = _path_requirements()
    elif isinstance(target, RangeTarget):
        requirements = _range_requirements()
    elif isinstance(target, LocationTarget):
        requirements = _location_requirements()
    else:
        requirements = ()
    return EvidencePolicy(
        profile=POLICY_PROFILE,
        intent=request.intent,
        target_kind=target.kind,
        requirements=requirements,
        limitations=(INTENT_LIMITATIONS[request.intent],),
    )


def _declaration_requirement(capability: Capability) -> EvidenceRequirement:
    return EvidenceRequirement(
        requirement_id="declaration_identity",
        role=EvidenceRole.DECLARATION,
        rule=RequirementRule.MINIMUM_EVIDENCE,
        strength=REQUIRED,
        description="the selected declaration must be represented in the bundle",
        capability=capability,
        acceptable_kinds=(ObservationKind.DECLARATION,),
        representations=(
            RepresentationKind.SIGNATURE,
            RepresentationKind.EXACT_SOURCE,
        ),
    )


def _source_requirement() -> EvidenceRequirement:
    return EvidenceRequirement(
        requirement_id="target_source",
        role=EvidenceRole.TARGET_SOURCE,
        rule=RequirementRule.EXACT_SOURCE,
        strength=REQUIRED,
        description=(
            "an exact source representation of the requested span must be selected"
        ),
        capability=Capability.READ_SOURCE,
        acceptable_kinds=(
            ObservationKind.SOURCE_WINDOW,
            ObservationKind.DECLARATION,
        ),
        representations=(RepresentationKind.EXACT_SOURCE,),
    )


def _optional_evidence() -> tuple[EvidenceRequirement, ...]:
    """Optional role evidence collected for every intent in the baseline."""
    return (
        EvidenceRequirement(
            requirement_id="representative_reference",
            role=EvidenceRole.REFERENCE,
            rule=RequirementRule.REPRESENTATIVE_EVIDENCE,
            strength=OPTIONAL,
            description="select an admissible reference when one was acquired",
            capability=Capability.SEMANTIC_REFERENCES,
            acceptable_kinds=(
                ObservationKind.SEMANTIC_REFERENCE,
                ObservationKind.SYNTACTIC_MENTION,
            ),
        ),
        EvidenceRequirement(
            requirement_id="implementations",
            role=EvidenceRole.IMPLEMENTATION,
            rule=RequirementRule.REPRESENTATIVE_EVIDENCE,
            strength=OPTIONAL,
            description="select an admissible implementation when one was acquired",
            capability=Capability.IMPLEMENTATIONS,
            acceptable_kinds=(ObservationKind.IMPLEMENTATION,),
        ),
        EvidenceRequirement(
            requirement_id="test_search",
            role=EvidenceRole.TEST,
            rule=RequirementRule.COLLECTION_OUTCOME,
            strength=OPTIONAL,
            description=(
                "the requested test-domain acquisition must have an explicit outcome"
            ),
            capability=Capability.LEXICAL_MENTIONS,
            acceptable_kinds=(
                ObservationKind.TEST_MENTION,
                ObservationKind.LEXICAL_MENTION,
            ),
            domain="test",
        ),
        EvidenceRequirement(
            requirement_id="owning_package",
            role=EvidenceRole.OWNERSHIP,
            rule=RequirementRule.REPRESENTATIVE_EVIDENCE,
            strength=OPTIONAL,
            description="select owning-package evidence when it was acquired",
            capability=Capability.OWNING_PACKAGE,
            acceptable_kinds=(ObservationKind.OWNING_PACKAGE,),
        ),
        EvidenceRequirement(
            requirement_id="additional_lexical_mention",
            role=EvidenceRole.LEXICAL_MENTION,
            rule=RequirementRule.REPRESENTATIVE_EVIDENCE,
            strength=OPTIONAL,
            description=(
                "select an admissible lexical mention when one was acquired; "
                "lexical evidence stays labeled separately from semantic references"
            ),
            capability=Capability.LEXICAL_MENTIONS,
            acceptable_kinds=(ObservationKind.LEXICAL_MENTION,),
        ),
    )


def _symbol_requirements() -> tuple[EvidenceRequirement, ...]:
    return (
        _declaration_requirement(Capability.FIND_DECLARATIONS),
        _source_requirement(),
        *_optional_evidence(),
    )


def _location_requirements() -> tuple[EvidenceRequirement, ...]:
    return (
        _declaration_requirement(Capability.RESOLVE_LOCATION),
        _source_requirement(),
        *_optional_evidence(),
    )


def _path_requirements() -> tuple[EvidenceRequirement, ...]:
    return (
        EvidenceRequirement(
            requirement_id="target_structure",
            role=EvidenceRole.TARGET_STRUCTURE,
            rule=RequirementRule.MINIMUM_EVIDENCE,
            strength=REQUIRED,
            description="the requested target structure must be represented",
            capability=Capability.OUTLINE,
            acceptable_kinds=(
                ObservationKind.OUTLINE,
                ObservationKind.SOURCE_WINDOW,
            ),
        ),
    )


def _range_requirements() -> tuple[EvidenceRequirement, ...]:
    return (
        EvidenceRequirement(
            requirement_id="requested_source",
            role=EvidenceRole.TARGET_SOURCE,
            rule=RequirementRule.EXACT_SOURCE,
            strength=REQUIRED,
            description="the requested source range must be selected exactly",
            capability=Capability.READ_SOURCE,
            acceptable_kinds=(ObservationKind.SOURCE_WINDOW,),
            representations=(RepresentationKind.EXACT_SOURCE,),
        ),
    )
