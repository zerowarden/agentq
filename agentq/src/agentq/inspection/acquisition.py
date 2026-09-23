"""Collection planning and bounded execution.

Planning matches policy requirements to available capabilities and internal
limits without consulting scores: changing a scoring weight must never change
what collection acquires. Declaration evidence already acquired during
resolution is reused rather than queried twice.
"""

from __future__ import annotations

from agentq.core import (
    PROVIDER_UNAVAILABLE,
    Coverage,
    Diagnostic,
    merge_typed,
)

from .budgeting import AcquisitionLimits
from .capabilities import Capability
from .contracts import (
    AcquiredEvidence,
    AcquisitionRecord,
    AvailabilityStatus,
    CapabilityReport,
    CollectionPlan,
    CollectionRequest,
    CollectionStatus,
    DeclarationCandidate,
    EvidencePolicy,
    EvidencePool,
    EvidenceRequest,
    EvidenceRequirement,
    EvidenceVariant,
    InspectionContext,
    InspectionTarget,
    Observation,
    PathTarget,
    RangeTarget,
    RequirementOmission,
    ResolutionResult,
    ResolvedTarget,
)

COLLECTION_PROFILE = "collection-v0"

FILE_ANCHORED = frozenset(
    {Capability.READ_SOURCE, Capability.OUTLINE, Capability.OWNING_PACKAGE}
)
SYMBOL_ANCHORED = frozenset(
    {
        Capability.SEMANTIC_REFERENCES,
        Capability.SYNTACTIC_MENTIONS,
        Capability.IMPLEMENTATIONS,
        Capability.LEXICAL_MENTIONS,
    }
)


def plan_collection(
    policy: EvidencePolicy,
    resolution: ResolutionResult,
    capabilities: CapabilityReport,
    limits: AcquisitionLimits,
    *,
    request_id: str,
    evidence_scopes: tuple[str, ...] = (),
) -> CollectionPlan:
    """Match each policy requirement to a bounded collection request."""
    declaration = (
        resolution.declaration if isinstance(resolution, ResolvedTarget) else None
    )
    covered = _resolved_capabilities(resolution)
    requests: list[CollectionRequest] = []
    omissions: list[RequirementOmission] = []
    for requirement in policy.requirements:
        if not requirement.capabilities:
            continue
        capability = _choose_capability(requirement, capabilities)
        if capability is None:
            omissions.append(_omission(requirement, capabilities))
            continue
        if capability in covered:
            continue
        target, scope = _evidence_target(
            capability, resolution, declaration, evidence_scopes
        )
        requests.append(
            CollectionRequest(
                request_id=f"collect-{requirement.requirement_id}",
                capability=capability,
                role=requirement.role,
                requirement_id=requirement.requirement_id,
                target=target,
                subject=declaration if capability in SYMBOL_ANCHORED else None,
                scope=scope,
                domain=requirement.domain,
                limit=limits.limit_for(capability),
                detail=requirement.description,
            )
        )
    return CollectionPlan(
        profile=COLLECTION_PROFILE,
        request_id=request_id,
        target=resolution.target,
        requests=tuple(requests),
        omissions=tuple(omissions),
    )


def _choose_capability(
    requirement: EvidenceRequirement, capabilities: CapabilityReport
) -> Capability | None:
    """The first capability in the requirement's recipe that can run now."""
    for capability in requirement.capabilities:
        if capabilities.available(capability):
            return capability
    return None


def _omission(
    requirement: EvidenceRequirement, capabilities: CapabilityReport
) -> RequirementOmission:
    """One truthful gap naming every alternative that could not run."""
    reasons: list[str] = []
    first_status = AvailabilityStatus.UNAVAILABLE
    for index, capability in enumerate(requirement.capabilities):
        gap = capabilities.gap_for(capability)
        status = gap.status if gap is not None else AvailabilityStatus.UNAVAILABLE
        reason = (
            gap.reason
            if gap is not None and gap.reason
            else "capability is not available for this request"
        )
        if index == 0:
            first_status = status
        reasons.append(f"{capability.value} ({status.value}: {reason})")
    return RequirementOmission(
        requirement_id=requirement.requirement_id,
        capability=requirement.capabilities[0],
        status=first_status,
        reason="; ".join(reasons),
    )


def _resolved_capabilities(resolution: ResolutionResult) -> frozenset[Capability]:
    if not isinstance(resolution, ResolvedTarget):
        return frozenset()
    return frozenset(item.record.capability for item in resolution.candidate_evidence)


def _evidence_target(
    capability: Capability,
    resolution: ResolutionResult,
    declaration: DeclarationCandidate | None,
    evidence_scopes: tuple[str, ...],
) -> tuple[InspectionTarget, tuple[str, ...]]:
    if capability in FILE_ANCHORED and declaration is not None:
        return (
            RangeTarget(path=declaration.path, ranges=(declaration.source_span(),)),
            (declaration.path,),
        )
    match capability:
        case Capability.OUTLINE:
            return resolution.target, _target_scope(resolution.target, evidence_scopes)
        case Capability.READ_SOURCE if isinstance(
            resolution.target, (PathTarget, RangeTarget)
        ):
            return resolution.target, (resolution.target.path,)
        case Capability.RESOLVE_LOCATION:
            return resolution.target, ()
        case _:
            return resolution.target, evidence_scopes


def _target_scope(
    target: InspectionTarget, evidence_scopes: tuple[str, ...]
) -> tuple[str, ...]:
    if isinstance(target, PathTarget):
        return (target.path,)
    return evidence_scopes


def acquire(
    plan: CollectionPlan,
    context: InspectionContext,
    *,
    prior: tuple[AcquiredEvidence, ...] = (),
) -> EvidencePool:
    """Execute a collection plan and normalize everything into one pool."""
    records: list[AcquisitionRecord] = []
    observations: list[Observation] = []
    variants: list[EvidenceVariant] = []
    limitations: list[Diagnostic] = []
    for acquired in prior:
        _absorb(acquired, records, observations, variants)
    requests = plan.requests
    results = _acquire_requests(plan, context)
    for request, acquired in zip(requests, results, strict=True):
        _execute(request, acquired, records, observations, variants, limitations)
    for omission in plan.omissions:
        limitations.append(
            Diagnostic(
                message=(
                    f"requirement {omission.requirement_id} could not be collected: "
                    f"{omission.capability.value} is {omission.status.value} "
                    f"({omission.reason})"
                ),
                code=PROVIDER_UNAVAILABLE,
            )
        )
    unique_observations = _dedupe_observations(observations)
    observed_ids = {item.observation_id for item in unique_observations}
    unique_variants = tuple(
        item
        for item in _dedupe_variants(variants)
        if item.observation_id in observed_ids
    )
    coverage = (
        merge_typed(*(record.coverage for record in records)) if records else Coverage()
    )
    return EvidencePool(
        request_id=plan.request_id,
        acquisitions=tuple(records),
        observations=unique_observations,
        variants=unique_variants,
        limitations=tuple(limitations),
        coverage=coverage,
    )


def _execute(
    request: CollectionRequest,
    acquired: tuple[AcquiredEvidence, ...],
    records: list[AcquisitionRecord],
    observations: list[Observation],
    variants: list[EvidenceVariant],
    limitations: list[Diagnostic],
) -> None:
    if not acquired:
        limitations.append(
            _unavailable(
                request, f"no adapter provided {request.capability.value} evidence"
            )
        )
        return
    for item in acquired:
        _absorb(item, records, observations, variants)
        limitations.extend(_outcome_limitations(request, item))


def _acquire_requests(
    plan: CollectionPlan, context: InspectionContext
) -> tuple[tuple[AcquiredEvidence, ...], ...]:
    """Execute planned requests through the registry, batching where possible."""
    requests = plan.requests
    if not requests:
        return ()
    if context.registry is None:
        return tuple(() for _ in requests)
    return context.registry.acquire_many(
        tuple(
            EvidenceRequest(
                request_id=request.request_id,
                capability=request.capability,
                target=request.target if request.target is not None else plan.target,
                subject=request.subject,
                scope=request.scope,
                limit=request.limit,
                domain=request.domain,
                requirement_id=request.requirement_id,
                detail=request.detail,
            )
            for request in requests
        ),
        context,
    )


def _outcome_limitations(
    request: CollectionRequest, acquired: AcquiredEvidence
) -> tuple[Diagnostic, ...]:
    """Surface truncation and failure as explicit, machine-coded limitations."""
    record = acquired.record
    if record.diagnostics:
        return record.diagnostics
    if record.status in {CollectionStatus.UNAVAILABLE, CollectionStatus.FAILED}:
        return (
            Diagnostic(
                message=(
                    f"{request.capability.value} acquisition was "
                    f"{record.status.value} for requirement {request.requirement_id}"
                ),
                code=PROVIDER_UNAVAILABLE,
            ),
        )
    if record.coverage.is_complete():
        return ()
    reason = record.coverage.reasons[0] if record.coverage.reasons else "partial"
    return (
        Diagnostic(
            message=(
                f"{request.capability.value} acquisition was "
                f"{record.coverage.status}: {reason}"
            ),
            code=reason,
            severity="warning",
        ),
    )


def _unavailable(request: CollectionRequest, detail: str) -> Diagnostic:
    return Diagnostic(
        message=(
            f"requirement {request.requirement_id} requested "
            f"{request.capability.value}: {detail}"
        ),
        code=PROVIDER_UNAVAILABLE,
    )


def _absorb(
    acquired: AcquiredEvidence,
    records: list[AcquisitionRecord],
    observations: list[Observation],
    variants: list[EvidenceVariant],
) -> None:
    records.append(acquired.record)
    observations.extend(acquired.observations)
    variants.extend(acquired.variants)


def _dedupe_observations(
    observations: list[Observation],
) -> tuple[Observation, ...]:
    seen: set[str] = set()
    unique: list[Observation] = []
    for observation in observations:
        if observation.observation_id in seen:
            continue
        seen.add(observation.observation_id)
        unique.append(observation)
    return tuple(unique)


def _dedupe_variants(variants: list[EvidenceVariant]) -> tuple[EvidenceVariant, ...]:
    seen: set[str] = set()
    unique: list[EvidenceVariant] = []
    for variant in variants:
        if variant.variant_id in seen:
            continue
        seen.add(variant.variant_id)
        unique.append(variant)
    return tuple(unique)
