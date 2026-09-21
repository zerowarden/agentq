"""Bounded text rendering helpers shared by every capability output.

These helpers format, truncate, and bound text before it reaches a transport.
They never write to stdout/stderr.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any, Protocol

from agentq.core import AgentQError, RenderedText, project_json
from agentq.redaction import redact_text

ANSI_RE = re.compile(r"\x1B(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])")
JSON_FORMATS = frozenset({"json", "compact-json"})


def strip_ansi(text: str) -> str:
    return ANSI_RE.sub("", text)


def compact_line(text: str, max_chars: int = 240) -> str:
    text = strip_ansi(text).replace("\r", "").rstrip("\n")
    text = redact_text(text)
    if len(text) <= max_chars:
        return text
    return text[: max(0, max_chars - 15)] + " …[truncated]"


def read_item_header(path: str, start: int, end: int, total_lines: int | None) -> str:
    """The one read-window header every renderer and delivery manifest shares."""
    total = total_lines if total_lines is not None else "?"
    return f"--- {path}:{start}-{end} ({total} lines total) ---"


def read_line_text(marker: str, number: int, width: int, text: str) -> str:
    """The one rendered source-line format renderers and manifests share."""
    return f"{marker} {number:>{width}} │ {text}"


def truncate_line(text: str, max_chars: int = 240) -> str:
    """Truncate a line that is already redacted (no re-redaction).

    Used by streamed output where :func:`redact_text` has already been applied,
    so calling it again would mangle already-redacted tokens.
    """
    text = strip_ansi(text).replace("\r", "").rstrip("\n")
    if len(text) <= max_chars:
        return text
    return text[: max(0, max_chars - 15)] + " …[truncated]"


def bound_output(text: str, budget: int) -> tuple[str, bool]:
    if budget <= 0 or len(text) <= budget:
        return text, False
    marker = (
        f"\n… [complete records omitted at {budget} chars; narrow the command scope]"
    )
    cutoff = max(0, budget - len(marker))
    head = text[:cutoff]
    newline = head.rfind("\n")
    head = head[:newline] if newline >= 0 else ""
    return (head.rstrip() + marker if head else marker.lstrip()), True


def human_bytes(value: int) -> str:
    units = ["B", "KiB", "MiB", "GiB"]
    current = float(value)
    for unit in units:
        if current < 1024 or unit == units[-1]:
            return f"{current:.1f} {unit}" if unit != "B" else f"{int(current)} B"
        current /= 1024
    return f"{value} B"


class Renderer(Protocol):
    """Every renderer accepts the typed value and an optional char budget."""

    def __call__(
        self, result: Any, /, *, budget: int = 0
    ) -> str | RenderedText: ...


@dataclass(frozen=True)
class RenderedOutput:
    """The budget projection of one result, before transport."""

    visible: str
    prebudget_chars: int = 0
    truncated: bool = False


def project_output(
    data: dict[str, Any],
    renderer: Renderer,
    *,
    result: Any | None = None,
    output_format: str = "text",
    budget: int = 0,
) -> RenderedOutput:
    """Render one result under its budget, without writing anything.

    JSON formats project the wire payload with :func:`project_json`; text
    formats call the renderer with the budget and hard-cut any renderer that
    ignored it. ``prebudget_chars`` is the size before truncation.
    """
    source = result if result is not None else data
    if output_format in JSON_FORMATS:
        full = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
        visible, truncated = project_json(data, budget)
        return RenderedOutput(visible, len(full), truncated)
    rendered = renderer(source, budget=budget)
    visible = str(rendered).rstrip()
    prebudget_chars = (
        rendered.prebudget_chars if isinstance(rendered, RenderedText) else len(visible)
    )
    truncated = rendered.truncated if isinstance(rendered, RenderedText) else False
    if budget > 0 and len(visible) > budget:
        visible, hard_cut = bound_output(visible, budget)
        truncated = truncated or hard_cut
    return RenderedOutput(visible, prebudget_chars, truncated)


def _is_evidence_only(command: str, data: dict[str, Any]) -> bool:
    """True when the final output is usable only if it carries source evidence."""
    if command == "read":
        return True
    return command == "inspect" and data.get("kind") == "source-windows"


def require_usable_budget(
    command: str,
    data: dict[str, Any],
    rendered: RenderedOutput,
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
    from agentq.continuations import iter_continuation_blocks

    if any(
        isinstance(block.get("command"), str) and block["command"] in rendered.visible
        for block in iter_continuation_blocks(data)
    ):
        return
    needed = max(budget * 2, int(rendered.prebudget_chars) + 64)
    raise AgentQError(
        f"{command}: render budget {budget} chars is too small to return evidence "
        f"or a recovery step; retry with --budget {needed} or higher"
    )
