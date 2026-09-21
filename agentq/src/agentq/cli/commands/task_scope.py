"""Shared task-baseline result annotations for command handlers."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any


def attach_task_scope(
    data: dict[str, Any],
    scoped: Mapping[str, Any] | None,
    *,
    requested: bool = False,
) -> None:
    """Record task-baseline selection facts on a result payload.

    ``requested`` keeps the keys present when a task scope was asked for even
    when the baseline could not enumerate files.
    """
    if scoped is None and not requested:
        return
    scope = scoped or {}
    data["task_scope"] = True
    data["task_ambiguous_preexisting"] = list(scope.get("ambiguous_preexisting", []))
    data["preexisting_unchanged_excluded"] = len(
        scope.get("excluded_preexisting_unchanged", [])
    )
