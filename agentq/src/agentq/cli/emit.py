"""CLI emission, render-budget projection, and cached operation suppression."""

from __future__ import annotations

import argparse
import hashlib
import inspect
import json
import time
from collections.abc import Callable
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from agentq.core import AgentQError
from agentq.delivery import bound_output
from agentq.emission import DispatchResult

from .transport import sink_encoding, write_stdout

Formatter = Callable[..., str]

_JSON_FORMATS = frozenset({"json", "compact-json"})


@dataclass(frozen=True)
class _Rendered:
    visible: str
    prebudget_chars: int
    truncated: bool


def _render_visible(
    args: argparse.Namespace,
    data: dict[str, Any],
    formatter: Formatter,
    *,
    result: Any | None = None,
) -> _Rendered:
    from agentq.core import RenderedText, project_json

    source = result if result is not None else data
    if args.format in _JSON_FORMATS:
        full = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
        visible, truncated = project_json(data, args.budget)
        return _Rendered(visible, len(full), truncated)
    if "budget" in inspect.signature(formatter).parameters:
        rendered = formatter(source, budget=args.budget)
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
    full = formatter(source).rstrip()
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
    root: Path | None = None,
    result: Any | None = None,
) -> DispatchResult:
    from agentq.context_cache import (
        extract_delivered_fragments,
        suppression_identity,
    )
    from agentq.core import repo_id, session_id
    from agentq.emission import finalize_output, request_identity
    from agentq.output_attribution import attribute_output, output_view

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
    rendered = _render_visible(args, data, formatter, result=result)
    command = str(getattr(args, "command", "unknown"))
    repo_text = str(getattr(args, "repo", "."))
    output_format = str(getattr(args, "format", "text"))
    budget = int(getattr(args, "budget", 0) or 0)
    encoding = sink_encoding()
    truncation_data = _truncation_data(data)
    render_budget_truncated = (
        rendered.truncated
        or bool(internal.get("truncated", False))
        or bool(truncation_data.get("render_budget_truncated", False))
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
    _require_usable_budget(
        command, data, rendered, budget, evidence, render_budget_truncated
    )
    payload = (rendered.visible + "\n").encode(encoding, errors="replace")
    write_stdout(rendered.visible)
    dispatch = finalize_output(
        data,
        output=rendered.visible,
        prebudget_chars=max(
            rendered.prebudget_chars, int(internal.get("prebudget_chars", 0) or 0)
        ),
        truncated=render_budget_truncated,
        request_id=request_identity(command, repo_text, output_format, budget),
        repo_id=repo_id(Path(repo_text)),
        context_id=identity[0] if identity is not None else None,
        consumer_id=(identity[1] or None) if identity is not None else session_id(),
        output_view=output_view(command, data),
        output_attribution=attribute_output(
            command, data, rendered.visible, output_format=output_format
        ),
        render_budget_truncated=render_budget_truncated,
        source_cap_truncated=bool(truncation_data.get("source_cap_truncated", False)),
        telemetry_data=telemetry_data,
        record_receipt=root is not None,
        fragments=tuple(evidence),
        encoding=encoding,
        written_bytes=len(payload),
        output_digest=hashlib.sha256(payload).hexdigest(),
    )
    if root is not None:
        dispatch = _record_delivery(command, dispatch, internal, fragment_rows)
    if exit_code:
        dispatch = replace(dispatch, exit_code=exit_code)
    return dispatch


def _record_delivery(
    command: str,
    dispatch: DispatchResult,
    internal: dict[str, Any],
    fragment_rows: list[dict[str, Any]],
) -> DispatchResult:
    """Persist the delivery receipt and its evidence after a flushed write.

    Ledger failure never reruns the command or undoes its outcome: evidence
    simply stays unsuppressed so a later call redelivers it, and the failure
    is carried on the dispatch for telemetry.
    """
    from agentq.state import store_receipt

    receipt = dispatch.receipt
    if receipt is None:
        return dispatch
    if receipt.context_id is None:
        return replace(dispatch, receipt=None)
    rows = list(fragment_rows)
    hints = (
        internal.get("delivery") if isinstance(internal.get("delivery"), dict) else {}
    )
    if not dispatch.render_budget_truncated:
        for kind in ("operation", "result"):
            hint = hints.get(kind) if isinstance(hints, dict) else None
            if isinstance(hint, dict) and isinstance(hint.get("key"), str):
                rows.append(
                    {
                        "command": command,
                        "kind": kind,
                        "key": hint["key"],
                        "payload": None,
                    }
                )
    if not rows:
        # Nothing was recorded, so no receipt exists to report: a receipt
        # object that was never persisted must not appear in telemetry.
        return replace(dispatch, receipt=None)
    try:
        stored = store_receipt(
            {
                "receipt_id": receipt.receipt_id,
                "repo_id": receipt.repo_id,
                "context_id": receipt.context_id,
                "consumer_id": receipt.consumer_id,
                "request_id": receipt.request_id,
                "output_digest": receipt.output_digest,
                "written_bytes": receipt.written_bytes,
                "transport": receipt.transport_status.value,
                "acknowledgment": receipt.acknowledgment_status.value,
                "emitted_at": receipt.emitted_at,
            },
            [{**row, "consumer_id": receipt.consumer_id or ""} for row in rows],
            now=time.time(),
        )
    except Exception as exc:
        return replace(dispatch, receipt_error=f"receipt not stored: {exc}")
    if not stored:
        return replace(dispatch, receipt_error="receipt not stored: ledger unavailable")
    return dispatch


def render_context_repeat(data: dict[str, Any]) -> str:
    return (
        f"{data['command']}: exact result already returned in this {data['repeat_scope']}; "
        "use --repeat to render it again"
    )


def _nested_continuation_scopes(data: dict[str, Any]):
    """Top-level and bounded nested mappings that may hold a continuation."""
    yield data
    for value in data.values():
        if not isinstance(value, dict):
            continue
        yield value
        for inner in value.values():
            if isinstance(inner, dict):
                yield inner


def _continuation_blocks(data: dict[str, Any]):
    """Every continuation block, in the bounded scope walk.

    Covers the top level, evidence wrappers (source), provider results
    (semantic, python, edit bundles), and the per-record follow-ups of a
    bounded hunk index. Cursor attachment and the budget-envelope check must
    see exactly the same blocks.
    """
    for scope in _nested_continuation_scopes(data):
        for key in ("continuation", "budget_continuation"):
            block = scope.get(key)
            if isinstance(block, dict):
                yield block
    hunks = data.get("hunks")
    if isinstance(hunks, list):
        for hunk in hunks:
            block = hunk.get("follow_up") if isinstance(hunk, dict) else None
            if isinstance(block, dict):
                yield block


def _is_evidence_only(command: str, data: dict[str, Any]) -> bool:
    """True when the final output is usable only if it carries source evidence."""
    if command == "read":
        return True
    return command == "inspect" and data.get("kind") == "source-windows"


def _require_usable_budget(
    command: str,
    data: dict[str, Any],
    rendered: _Rendered,
    budget: int,
    evidence: Any,
    render_budget_truncated: bool,
) -> None:
    """Refuse a successful page that shows neither evidence nor recovery.

    An evidence-only command whose render was budget-truncated, emitted no
    fragment, and lost its recovery continuation is a dead end: exit 0 there
    would read as a completed scan. The caller gets an explicit budget error
    naming a sufficient budget instead.
    """
    if (
        budget <= 0
        or not render_budget_truncated
        or evidence
        or not _is_evidence_only(command, data)
    ):
        return
    if any(
        isinstance(block.get("command"), str) and block["command"] in rendered.visible
        for block in _continuation_blocks(data)
    ):
        return
    needed = max(budget * 2, int(rendered.prebudget_chars) + 64)
    raise AgentQError(
        f"{command}: render budget {budget} chars is too small to return evidence "
        f"or a recovery step; retry with --budget {needed} or higher"
    )


def _attach_continuation_cursors(root: Path, data: dict[str, Any]) -> None:
    """Replace producer continuation blocks with short local cursor tokens.

    Typed blocks are validated and stored as typed records; legacy command
    blocks from unmigrated producers are validated into argv records. Both
    become ``agentq continue CURSOR`` for display, and typed records are only
    ever executed from their stored request, never from command text.
    """
    from agentq.continuations import attach_cursor
    from agentq.core import ContractError

    for block in _continuation_blocks(data):
        try:
            attach_cursor(root, block)
        except ContractError:
            # A malformed producer block stays visible in its literal form
            # instead of failing the whole command result.
            continue


def _mark_operation_delivery(data: dict[str, Any], key: str) -> None:
    """Attach the operation digest the emission will acknowledge when untruncated.

    The emission layer stores it only after a successful write and flush, and
    only when the result was fully rendered: a truncated render suppresses
    individual delivered fragments, never the whole operation.
    """
    internal = data.get("_agentq_internal")
    if not isinstance(internal, dict):
        internal = {}
        data["_agentq_internal"] = internal
    delivery = internal.get("delivery")
    if not isinstance(delivery, dict):
        delivery = {}
        internal["delivery"] = delivery
    delivery["operation"] = {"key": key}


def emit_cached(
    args: argparse.Namespace,
    root: Path,
    command: str,
    options: dict[str, Any],
    producer: Callable[[], Any],
    formatter: Formatter,
    *,
    wire: Callable[[Any], dict[str, Any]] | None = None,
) -> DispatchResult:
    from agentq.context_cache import (
        operation_cache_key,
        operation_repeat_advice,
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
    produced = producer()
    typed = produced if hasattr(produced, "to_wire") else None
    if typed is None:
        data = produced
    elif wire is not None:
        data = wire(typed)
    else:
        data = typed.to_wire()
    if key is not None:
        _mark_operation_delivery(data, key)
    _attach_continuation_cursors(root, data)
    if typed is not None and hasattr(typed, "with_wire_continuations"):
        typed = typed.with_wire_continuations(data)
    return emit(args, data, formatter, root=root, result=typed)
