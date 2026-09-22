"""Telemetry analytics: one module per analysis, plus the shared numeric
and session primitives they agree on.
"""

from __future__ import annotations

import math
from collections import defaultdict
from collections.abc import Sequence
from typing import Any


def fallback_session_contexts(
    events: list[dict[str, Any]],
    gap_seconds: int = 30 * 60,
) -> dict[int, str]:
    by_repo: dict[str, list[tuple[float, int]]] = defaultdict(list)
    for index, event in enumerate(events):
        if event.get("thread_id"):
            continue
        by_repo[str(event.get("repo_id", ""))].append(
            (float(event.get("time", 0)), index)
        )
    contexts: dict[int, str] = {}
    for repository_id, entries in by_repo.items():
        previous: float | None = None
        session = 0
        for timestamp, index in sorted(entries):
            if previous is None or timestamp - previous > gap_seconds:
                session += 1
            contexts[index] = f"session:{repository_id}:{session}"
            previous = timestamp
    return contexts


def percent(numerator: int | float, denominator: int | float) -> float | None:
    return round(100.0 * numerator / denominator, 1) if denominator else None


def _percentile(values: Sequence[int | float], q: float) -> float | None:
    if not values:
        return None
    ordered = sorted(float(v) for v in values)
    if len(ordered) == 1:
        return ordered[0]
    pos = (len(ordered) - 1) * q
    low, high = math.floor(pos), math.ceil(pos)
    if low == high:
        return ordered[low]
    return ordered[low] * (high - pos) + ordered[high] * (pos - low)


def distribution(values: Sequence[int | float]) -> dict[str, float | int | None]:
    return {
        "p50": round(_percentile(values, 0.50) or 0, 1) if values else None,
        "p90": round(_percentile(values, 0.90) or 0, 1) if values else None,
        "max": round(max(values), 1) if values else None,
    }
