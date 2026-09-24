"""Deterministic, human-readable selectors for captured variants.

Repository captures have no authored alias map, so review needs stable names
derived from the evidence itself. An alias reads
``<path>:<start>-<end>/<observation kind>/<representation>`` and is only ever
a review aid: it resolves to exactly one variant id inside one capture.
"""

from __future__ import annotations

from agentq.inspection.contracts import EvidencePool


def _base_alias(
    path: str | None, start: int | None, end: int | None, kind: str, representation: str
) -> str:
    location = path or "unknown"
    if start is not None:
        location = f"{location}:{start}-{end if end is not None else start}"
    return f"{location}/{kind}/{representation}"


def variant_aliases(pool: EvidencePool) -> dict[str, str]:
    """Alias -> variant id for every variant in one pool, deterministically."""
    kinds = {
        observation.observation_id: observation.kind.value
        for observation in pool.observations
    }
    ordered = sorted(
        pool.variants,
        key=lambda variant: (
            variant.source.path or "",
            variant.span.start_line if variant.span is not None else 0,
            variant.span.end_line if variant.span is not None else 0,
            kinds.get(variant.observation_id, ""),
            variant.representation.value,
            variant.fidelity.value,
            variant.text,
        ),
    )
    aliases: dict[str, str] = {}
    seen: dict[str, int] = {}
    for variant in ordered:
        base = _base_alias(
            variant.source.path,
            variant.span.start_line if variant.span is not None else None,
            variant.span.end_line if variant.span is not None else None,
            kinds.get(variant.observation_id, "observation"),
            variant.representation.value,
        )
        seen[base] = seen.get(base, 0) + 1
        alias = base if seen[base] == 1 else f"{base}#{seen[base]}"
        aliases[alias] = variant.variant_id
    return aliases
