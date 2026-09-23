"""CLI emission adapter: render, transport, and receipt one command result.

The adapter only sequences typed delivery steps: project the result under its
budget, write the final bytes, then finalize and record the receipt. Rendering,
suppression, and receipt semantics live in :mod:`agentq.delivery`.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path
from typing import Any, cast

from agentq.continuations import attach_continuation_cursors
from agentq.core import (
    as_dict,
    canonical_digest,
    canonical_json,
    dict_field,
    list_field,
    repo_id,
    request_identity,
    session_id,
)
from agentq.delivery import (
    DeliveryContext,
    DispatchResult,
    EmittedBytes,
    EvidenceFragment,
    RenderedOutput,
    Renderer,
    begin_cached_operation,
    extract_delivered_fragments,
    finalize_output,
    mark_operation_delivery,
    project_output,
    record_delivery,
    require_usable_budget,
    suppression_identity,
)
from agentq.inspection.contracts import InspectionBundle, RenderedBundle
from agentq.inspection.rendering import selected_cost
from agentq.output_attribution import (
    attribute_output,
    empty_attribution,
    output_view,
)

from .transport import sink_encoding, write_stderr, write_stdout

_PRESENTATION_ARGS = frozenset(
    {
        "command",
        "repo",
        "format",
        "repeat",
        "color",
        "plain",
        "utc",
        "watch",
        "debug",
    }
)


def _json_safe(value: Any) -> Any:
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    if isinstance(value, (list, tuple)):
        return [_json_safe(item) for item in cast("list[Any] | tuple[Any, ...]", value)]
    if isinstance(value, dict):
        return {
            str(key): _json_safe(item)
            for key, item in cast("dict[str, Any]", value).items()
        }
    return str(value)


def invocation_request_id(args: argparse.Namespace) -> str:
    """The one semantic identity for a CLI emission.

    Presentation flags are excluded so the identity depends on what the
    operation asked for, not how it was displayed.
    """
    values = {
        key: _json_safe(value)
        for key, value in sorted(vars(args).items())
        if key not in _PRESENTATION_ARGS and value is not None
    }
    scopes: list[Any] = list_field(vars(args), "paths")
    return request_identity(
        root=str(getattr(args, "repo", ".")),
        operation=str(getattr(args, "command", "unknown")),
        options_wire=values,
        scopes=tuple(str(item) for item in scopes),
    )


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
    budget: int = 0,
) -> DispatchResult:
    internal: dict[str, Any] = as_dict(data.pop("_agentq_internal", None))
    telemetry_data: dict[str, Any] = dict_field(internal, "telemetry_data") or dict(
        data
    )
    command = str(getattr(args, "command", "unknown"))
    repo_text = str(getattr(args, "repo", "."))
    output_format = str(getattr(args, "format", "text"))
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
            request_id=invocation_request_id(args),
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


def render_context_repeat(
    data: dict[str, Any], *, budget: int = 0
) -> str:  # pyright: ignore[reportUnusedParameter]
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
    budget: int = 0,
) -> DispatchResult:
    decision = begin_cached_operation(
        root,
        command,
        options,
        budget=budget,
        output_format=args.format,
        repeat=bool(getattr(args, "repeat", False)),
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
    return emit(args, data, formatter, root=root, result=typed, budget=budget)


def emit_rendered(
    args: argparse.Namespace,
    root: Path,
    command: str,
    rendered: RenderedBundle,
    *,
    bundle: InspectionBundle | None = None,
) -> DispatchResult:
    """Deliver an already-selected, already-bounded inspection result.

    The inspection service owns evidence selection and serialization cost; this
    path writes exactly those bytes and records the same receipt as any other
    command. It performs no projection, suppression, or continuation: a
    selected bundle is never dropped or shortened a second time.
    """
    encoding = sink_encoding()
    visible = rendered.text
    payload = (visible + "\n").encode(encoding, errors="replace")
    fragments, rows = (
        _inspection_fragments(bundle) if bundle is not None else ([], [])
    )
    write_stdout(visible)
    identity = suppression_identity(root)
    data: dict[str, Any] = {"command": command, "kind": "inspection"}
    dispatch = finalize_output(
        data,
        rendered=RenderedOutput(
            visible=visible, prebudget_chars=len(visible), truncated=False
        ),
        context=DeliveryContext(
            request_id=invocation_request_id(args),
            repo_id=repo_id(root),
            context_id=identity[0] if identity is not None else None,
            consumer_id=(identity[1] or None) if identity is not None else session_id(),
            output_view=output_view(command, data),
            output_attribution=_inspection_attribution(bundle, visible, rendered.format),
            encoding=encoding,
            record_receipt=True,
        ),
        sink=EmittedBytes(
            written_bytes=len(payload),
            output_digest=hashlib.sha256(payload).hexdigest(),
        ),
        fragments=tuple(fragments),
        telemetry_data={
            "command": command,
            "selection": (
                len(bundle.selection.selected)
                if bundle is not None and bundle.selection is not None
                else 0
            ),
        },
    )
    return record_delivery(command, dispatch, {}, rows)


def _inspection_attribution(
    bundle: InspectionBundle | None, visible: str, output_format: str
) -> dict[str, int]:
    """Label the selected evidence text separately from framing.

    The generic attributor understands search/read payload shapes; inspection
    evidence is already selected and measurable here, so it is measured here.
    """
    attribution = empty_attribution()
    evidence = 0
    if bundle is not None and bundle.selection is not None and visible:
        for item in bundle.selection.selected:
            if output_format == "text":
                evidence += selected_cost(item, "text") - 1
            else:
                evidence += len(canonical_json(item.variant.to_wire()))
    evidence = min(evidence, len(visible))
    attribution["unique_evidence_chars"] = evidence
    attribution["framing_chars"] = len(visible) - evidence
    return attribution


def _inspection_fragments(
    bundle: InspectionBundle,
) -> tuple[list[EvidenceFragment], list[dict[str, Any]]]:
    """Evidence records and ledger rows for the representations actually emitted."""
    selection = bundle.selection
    if selection is None:
        return [], []
    fragments: list[EvidenceFragment] = []
    rows: list[dict[str, Any]] = [
        {
            "kind": "operation",
            "key": canonical_digest(
                {"command": "inspect", "request_id": bundle.request.request_id},
                length=32,
            ),
        }
    ]
    for item in selection.selected:
        variant = item.variant
        key = canonical_digest(
            {
                "observation_id": item.observation_id,
                "variant_id": item.variant_id,
                "reason": item.reason,
            },
            length=32,
        )
        fragments.append(
            EvidenceFragment(
                evidence_id=item.observation_id,
                kind=variant.representation.value,
                source=variant.source,
                variant=variant.fidelity.value,
                rendered_chars=len(variant.text),
            )
        )
        rows.append(
            {
                "kind": "inspection-evidence",
                "key": key,
                "payload": {
                    "path": variant.source.path,
                    "representation": variant.representation.value,
                    "fidelity": variant.fidelity.value,
                    "reason": item.reason,
                    "score": item.score,
                },
            }
        )
    return fragments, rows


def write_trace(trace_wire: dict[str, Any]) -> None:
    """Write one structured debug record to stderr, never to stdout."""
    write_stderr(json.dumps(trace_wire, ensure_ascii=False, sort_keys=True) + "\n")
