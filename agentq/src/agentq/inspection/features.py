"""Pure feature extraction.

Features are observable inputs to scoring, not provider identities or costs.
Unknown values stay unknown: a feature is never defaulted to a plausible value
just so scoring can produce a number.
"""

from __future__ import annotations

from .contracts import (
    Binding,
    EvidenceFeatures,
    EvidencePool,
    EvidenceRole,
    MentionPayload,
    Observation,
    ObservationKind,
    ReferencePayload,
)

ROLE_BY_KIND: dict[ObservationKind, EvidenceRole] = {
    ObservationKind.DECLARATION: EvidenceRole.DECLARATION,
    ObservationKind.SEMANTIC_REFERENCE: EvidenceRole.REFERENCE,
    ObservationKind.SYNTACTIC_MENTION: EvidenceRole.REFERENCE,
    ObservationKind.LEXICAL_MENTION: EvidenceRole.LEXICAL_MENTION,
    ObservationKind.TEST_MENTION: EvidenceRole.TEST,
    ObservationKind.IMPLEMENTATION: EvidenceRole.IMPLEMENTATION,
    ObservationKind.SOURCE_WINDOW: EvidenceRole.TARGET_SOURCE,
    ObservationKind.OUTLINE: EvidenceRole.TARGET_STRUCTURE,
    ObservationKind.OWNING_PACKAGE: EvidenceRole.OWNERSHIP,
}


def extract_features(pool: EvidencePool) -> tuple[EvidenceFeatures, ...]:
    """Extract one immutable feature record per observation."""
    return tuple(_features_for(pool, observation) for observation in pool.observations)


def _features_for(pool: EvidencePool, observation: Observation) -> EvidenceFeatures:
    relation: str | None = None
    binding = Binding.UNKNOWN
    domain: str | None = None
    payload = observation.payload
    if isinstance(payload, ReferencePayload):
        relation = payload.relationship
        binding = payload.binding
        domain = payload.domain
    elif isinstance(payload, MentionPayload):
        domain = payload.domain
    flags = (
        ("unstable",)
        if observation.observation_id in pool.unstable_observation_ids
        else ()
    )
    return EvidenceFeatures(
        observation_id=observation.observation_id,
        role=ROLE_BY_KIND.get(observation.kind),
        observation_kind=observation.kind,
        relation=relation,
        binding=binding,
        domain=domain,
        flags=flags,
    )
