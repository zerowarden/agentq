"""CLI emission, render-budget projection, and cached operation suppression."""

from __future__ import annotations

import argparse
import inspect
import json
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from agentq_lib.common import bound_output
from agentq_lib.emission import DispatchResult

from .types import Formatter

_JSON_FORMATS = frozenset({"json", "compact-json"})


@dataclass(frozen=True)
class _Rendered:
    visible: str
    prebudget_chars: int
    truncated: bool


def _render_visible(
    args: argparse.Namespace, data: dict[str, Any], formatter: Formatter
) -> _Rendered:
    from agentq_lib.budgeting import RenderedText, project_json

    if args.format in _JSON_FORMATS:
        full = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
        visible, truncated = project_json(data, args.budget)
        return _Rendered(visible, len(full), truncated)
    if "budget" in inspect.signature(formatter).parameters:
        rendered = formatter(data, budget=args.budget)
        visible = str(rendered).rstrip()
        prebudget_chars = (
            rendered.prebudget_chars
            if isinstance(rendered, RenderedText)
            else len(visible)
        )
        truncated = rendered.truncated if isinstance(rendered, RenderedText) else False
        if args.budget > 0 and len(visible) > args.budget:
            visible, hard_cut = bound_output(visible, args.budget)
            truncated = truncated or hard_cut
        return _Rendered(visible, prebudget_chars, truncated)
    full = formatter(data).rstrip()
    visible, truncated = bound_output(full, args.budget)
    return _Rendered(visible, len(full), truncated)


def _truncation_data(data: dict[str, Any]) -> dict[str, Any]:
    if data.get("kind") == "source-windows" and isinstance(data.get("source"), dict):
        return data["source"]
    return data


def emit(
    args: argparse.Namespace,
    data: dict[str, Any],
    formatter: Formatter,
    *,
    exit_code: int = 0,
) -> DispatchResult:
    from dataclasses import replace

    from agentq_lib.emission import finalize_output, request_identity
    from agentq_lib.output_attribution import attribute_output, output_view
    from agentq_lib.runtime import repo_id, session_id

    internal = (
        data.pop("_agentq_internal", {})
        if isinstance(data.get("_agentq_internal"), dict)
        else {}
    )
    telemetry_data = (
        internal.get("telemetry_data")
        if isinstance(internal.get("telemetry_data"), dict)
        else data
    )
    rendered = _render_visible(args, data, formatter)
    print(rendered.visible)
    truncation_data = _truncation_data(data)
    render_budget_truncated = (
        rendered.truncated
        or bool(internal.get("truncated", False))
        or bool(truncation_data.get("render_budget_truncated", False))
    )
    command = str(getattr(args, "command", "unknown"))
    repo_text = str(getattr(args, "repo", "."))
    output_format = str(getattr(args, "format", "text"))
    budget = int(getattr(args, "budget", 0) or 0)
    dispatch = finalize_output(
        data,
        output=rendered.visible,
        prebudget_chars=max(
            rendered.prebudget_chars, int(internal.get("prebudget_chars", 0) or 0)
        ),
        truncated=render_budget_truncated,
        request_id=request_identity(command, repo_text, output_format, budget),
        repo_id=repo_id(Path(repo_text)),
        consumer_id=session_id(),
        output_view=output_view(command, data),
        output_attribution=attribute_output(
            command, data, rendered.visible, output_format=output_format
        ),
        render_budget_truncated=render_budget_truncated,
        source_cap_truncated=bool(truncation_data.get("source_cap_truncated", False)),
        telemetry_data=telemetry_data,
    )
    if exit_code:
        dispatch = replace(dispatch, exit_code=exit_code)
    return dispatch


def render_context_repeat(data: dict[str, Any]) -> str:
    return (
        f"{data['command']}: exact result already returned in this {data['repeat_scope']}; "
        "use --repeat to render it again"
    )


def _attach_continuation_cursors(root: Path, data: dict[str, Any]) -> None:
    """Replace verbose continuation commands with short local cursor tokens.

    `agentq continue CURSOR` replays the stored command after session and
    workspace validation, so reproducibility survives without spending render
    budget on verbose command text. The walk is bounded at two levels, which
    covers every continuation block: top level, evidence wrappers (source),
    and provider results (semantic, python, edit bundles).
    """
    from agentq_lib.context_cache import remember_continuation

    scopes = [data]
    for value in data.values():
        if isinstance(value, dict):
            scopes.append(value)
            for inner in value.values():
                if isinstance(inner, dict):
                    scopes.append(inner)
    for scope in scopes:
        for key in ("continuation", "budget_continuation"):
            block = scope.get(key)
            if not (isinstance(block, dict) and isinstance(block.get("command"), str)):
                continue
            stored = remember_continuation(root, str(block["command"]))
            if not stored:
                continue
            block["command"] = f"agentq continue {stored['cursor']}"
            block["cursor"] = stored["cursor"]
            block["expires_at"] = stored["expires_at"]


def emit_cached(
    args: argparse.Namespace,
    root: Path,
    command: str,
    options: dict[str, Any],
    producer: Callable[[], dict[str, Any]],
    formatter: Formatter,
) -> DispatchResult:
    from agentq_lib.context_cache import (
        operation_cache_key,
        operation_repeat_advice,
        remember_operation,
        suppression_active,
    )

    # Workspace identity runs Git and stats the worktree; skip it entirely when
    # repeat suppression cannot apply (feature disabled or no session identity).
    key: str | None = None
    if suppression_active(root):
        key = operation_cache_key(
            root, command, {**options, "budget": args.budget, "format": args.format}
        )
        advice = operation_repeat_advice(root, command, key)
        if advice and not args.repeat:
            return emit(
                args,
                {
                    "command": command,
                    "repeat_suppressed": True,
                    "repeat_scope": advice["scope"],
                },
                render_context_repeat,
            )
    data = producer()
    if key is not None:
        remember_operation(root, command, key)
    _attach_continuation_cursors(root, data)
    return emit(args, data, formatter)
