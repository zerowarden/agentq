"""Canonical evidence vocabulary shared by evidence-producing commands.

Provenance states how evidence was obtained. Coverage states how much of the
requested evidence a bounded operation actually returned, with machine-readable
causes. Structured payloads converge on:

    "provenance": "semantic" | "syntactic" | "lexical" | "heuristic"
    "coverage": {"status": "complete" | "sampled" | "partial" | "unknown",
                 "reason": ["<cause>", ...]}
"""

from __future__ import annotations

from typing import Any

SEMANTIC = "semantic"
SYNTACTIC = "syntactic"
LEXICAL = "lexical"
HEURISTIC = "heuristic"
_PROVENANCE_RANK = {HEURISTIC: 0, LEXICAL: 1, SYNTACTIC: 2, SEMANTIC: 3}

COMPLETE = "complete"
SAMPLED = "sampled"
PARTIAL = "partial"
UNKNOWN = "unknown"
_COVERAGE_RANK = {UNKNOWN: 0, PARTIAL: 1, SAMPLED: 2, COMPLETE: 3}

# Standard coverage causes; new causes may be added but should stay generic.
SCAN_CAP = "scan_cap"
RESULT_LIMIT = "result_limit"
REFERENCE_LIMIT = "reference_limit"
LINE_CAP = "line_cap"
STEP_LIMIT = "step_limit"
PARSE_ERROR = "parse_error"
PROVIDER_ERROR = "provider_error"
PROVIDER_UNAVAILABLE = "provider_unavailable"


def coverage(status: str, *reasons: str) -> dict[str, Any]:
    """Build the canonical coverage block; causes keep first-seen order."""
    return {"status": status, "reason": list(dict.fromkeys(reasons))}


def complete() -> dict[str, Any]:
    return {"status": COMPLETE, "reason": []}


def status_of(value: Any) -> str:
    """Canonical status from a coverage block, a legacy status string, or None."""
    if isinstance(value, dict):
        value = value.get("status")
    return value if isinstance(value, str) and value in _COVERAGE_RANK else UNKNOWN


def merge_coverage(*blocks: Any) -> dict[str, Any]:
    """Combine coverage blocks: the weakest provided status wins; causes merge in order."""
    statuses = [status_of(block) for block in blocks if block is not None]
    if not statuses:
        return coverage(UNKNOWN)
    weakest = statuses[0]
    for status in statuses[1:]:
        if _COVERAGE_RANK[status] < _COVERAGE_RANK[weakest]:
            weakest = status
    reasons: list[str] = []
    for block in blocks:
        if isinstance(block, dict):
            reasons.extend(str(cause) for cause in block.get("reason") or [])
    return coverage(weakest, *reasons)


def downgrade(current: Any, status: str, *reasons: str) -> dict[str, Any]:
    """Return coverage that never upgrades an already-weaker status."""
    previous = status_of(current)
    merged = status if _COVERAGE_RANK[status] < _COVERAGE_RANK[previous] else previous
    causes = []
    if isinstance(current, dict):
        causes.extend(str(cause) for cause in current.get("reason") or [])
    return coverage(merged, *causes, *reasons)


def best_provenance(*values: str | None) -> str | None:
    """Strongest provenance among the given values, or None when none apply."""
    ranked = [_PROVENANCE_RANK[value] for value in values if value in _PROVENANCE_RANK]
    if not ranked:
        return None
    strongest = max(ranked)
    return next(value for value in values if value in _PROVENANCE_RANK and _PROVENANCE_RANK[value] == strongest)
