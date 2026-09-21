"""CLI emission adapter: render, transport, and receipt one command result.

The adapter only sequences typed delivery steps: project the result under its
budget, write the final bytes, then finalize and record the receipt. Rendering,
suppression, and receipt semantics live in :mod:`agentq.delivery`.
"""

from __future__ import annotations

import argparse
import hashlib
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path
from typing import Any

from agentq.continuations import attach_continuation_cursors
from agentq.core import repo_id, session_id
from agentq.delivery import (
    DeliveryContext,
    DispatchResult,
    EmittedBytes,
    Renderer,
    begin_cached_operation,
    extract_delivered_fragments,
    finalize_output,
    mark_operation_delivery,
    project_output,
    record_delivery,
    request_identity,
    require_usable_budget,
    suppression_identity,
)
from agentq.output_attribution import attribute_output, output_view

from .transport import sink_encoding, write_stdout


def _truncation_data(data: dict[str, Any]) -> dict[str, Any]:
    if data.get("kind") == "source-windows" and isinstance(data.get("source"), dict):
        return data["source"]
    return data


def emit(
    args: argparse.Namespace,
    data: dict[str, Any],
    formatter: Renderer,
    *,
    exit_code: int = 0,
    root: Path | None = None,
    result: Any | None = None,
) -> DispatchResult:
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
    command = str(getattr(args, "command", "unknown"))
    repo_text = str(getattr(args, "repo", "."))
    output_format = str(getattr(args, "format", "text"))
    budget = int(getattr(args, "budget", 0) or 0)
    encoding = sink_encoding()
    rendered = project_output(
        data,
        formatter,
        result=result,
        output_format=output_format,
        budget=budget,
    )
    truncation_data = _truncation_data(data)
    render_budget_truncated = (
        rendered.truncated
        or bool(internal.get("truncated", False))
        or bool(truncation_data.get("render_budget_truncated", False))
    )
    rendered = replace(
        rendered,
        prebudget_chars=max(
            rendered.prebudget_chars, int(internal.get("prebudget_chars", 0) or 0)
        ),
        truncated=render_budget_truncated,
    )
    # Fragments provably present in the final bytes. Pure collection: writing
    # them to the ledger happens only after a successful write and flush, and
    # only under a safe identity. The manifest itself does not depend on a
    # suppression identity: it is evidence of what this output showed.
    identity = suppression_identity(root) if root is not None else None
    evidence, fragment_rows = (
        extract_delivered_fragments(
            root, command, data, rendered.visible, output_format
        )
        if root is not None
        else ((), [])
    )
    require_usable_budget(
        command, data, rendered, budget, evidence, render_budget_truncated
    )
    payload = (rendered.visible + "\n").encode(encoding, errors="replace")
    write_stdout(rendered.visible)
    dispatch = finalize_output(
        data,
        rendered=rendered,
        context=DeliveryContext(
            request_id=request_identity(command, repo_text, output_format, budget),
            repo_id=repo_id(Path(repo_text)),
            context_id=identity[0] if identity is not None else None,
            consumer_id=(identity[1] or None) if identity is not None else session_id(),
            output_view=output_view(command, data),
            output_attribution=attribute_output(
                command, data, rendered.visible, output_format=output_format
            ),
            encoding=encoding,
            record_receipt=root is not None,
        ),
        sink=EmittedBytes(
            written_bytes=len(payload),
            output_digest=hashlib.sha256(payload).hexdigest(),
        ),
        fragments=tuple(evidence),
        source_cap_truncated=bool(truncation_data.get("source_cap_truncated", False)),
        telemetry_data=telemetry_data,
    )
    if root is not None:
        dispatch = record_delivery(command, dispatch, internal, fragment_rows)
    if exit_code:
        dispatch = replace(dispatch, exit_code=exit_code)
    return dispatch


def render_context_repeat(data: dict[str, Any], *, budget: int = 0) -> str:
    return (
        f"{data['command']}: exact result already returned in this {data['repeat_scope']}; "
        "use --repeat to render it again"
    )


def emit_cached(
    args: argparse.Namespace,
    root: Path,
    command: str,
    options: dict[str, Any],
    producer: Callable[[], Any],
    formatter: Renderer,
    *,
    wire: Callable[[Any], dict[str, Any]] | None = None,
) -> DispatchResult:
    decision = begin_cached_operation(
        root,
        command,
        options,
        budget=args.budget,
        output_format=args.format,
        repeat=args.repeat,
    )
    if decision.suppressed_scope is not None:
        return emit(
            args,
            {
                "command": command,
                "repeat_suppressed": True,
                "repeat_scope": decision.suppressed_scope,
            },
            render_context_repeat,
        )
    produced = producer()
    typed = produced if hasattr(produced, "to_wire") else None
    if typed is None:
        data = produced
    elif wire is not None:
        data = wire(typed)
    else:
        data = typed.to_wire()
    if decision.key is not None:
        mark_operation_delivery(data, decision.key)
    attach_continuation_cursors(root, data)
    if typed is not None and hasattr(typed, "with_wire_continuations"):
        typed = typed.with_wire_continuations(data)
    return emit(args, data, formatter, root=root, result=typed)
