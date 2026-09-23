"""Minimum source validation for one inspection.

M1 provides versioned observations and best-effort consistency checks; it does
not claim an atomic repository snapshot, persistent invalidation, or
cross-request refresh. Those are M2 concerns.
"""

from __future__ import annotations

from dataclasses import replace
from hashlib import sha256

from agentq.core import PARTIAL, SOURCE_UNSTABLE, Diagnostic

from .contracts import (
    DeclarationCandidate,
    EvidencePool,
    Observation,
    SourceVersionReader,
)

VERSION_METHOD = "content_sha256"


def content_version(content: bytes) -> str:
    """The content version of one file, hashed from its bytes."""
    return sha256(content).hexdigest()[:16]


def candidate_is_current(
    candidate: DeclarationCandidate, read_version: SourceVersionReader
) -> bool:
    """Validate a candidate against the source as it exists now.

    An unreadable or unversioned candidate is not current: a candidate is only
    accepted when its recorded version matches a version re-read from source.
    """
    current = read_version(candidate.path)
    return current is not None and current == candidate.source_version


def unstable_observations(
    observations: tuple[Observation, ...], read_version: SourceVersionReader
) -> tuple[str, ...]:
    """Observation ids whose recorded source versions no longer match.

    A path whose version cannot be read is not marked unstable; absence is a
    separate limitation from a changed source.
    """
    unstable: list[str] = []
    for observation in observations:
        for stamp in observation.source_versions:
            current = read_version(stamp.path)
            if current is not None and current != stamp.version:
                unstable.append(observation.observation_id)
                break
    return tuple(dict.fromkeys(unstable))


def apply_unstable(
    pool: EvidencePool, observation_ids: tuple[str, ...]
) -> EvidencePool:
    """Mark affected observations unstable and downgrade their coverage."""
    if not observation_ids:
        return pool
    diagnostics: list[Diagnostic] = []
    for observation_id in observation_ids:
        observation = pool.observation(observation_id)
        diagnostics.append(
            Diagnostic(
                message=(
                    "source changed during inspection for observation "
                    f"{observation_id}"
                ),
                code=SOURCE_UNSTABLE,
                path=observation.source.path if observation is not None else None,
                severity="warning",
            )
        )
    coverage = (
        pool.coverage.with_failure(SOURCE_UNSTABLE, status=PARTIAL)
        if pool.observations
        else pool.coverage
    )
    return replace(
        pool,
        unstable_observation_ids=observation_ids,
        limitations=(*pool.limitations, *diagnostics),
        coverage=coverage,
    )
