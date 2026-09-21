"""Bounded text rendering helpers shared by every capability output.

These helpers format, truncate, and bound text before it reaches a transport.
They never write to stdout/stderr.
"""

from __future__ import annotations

import re

from agentq.redaction import redact_text

ANSI_RE = re.compile(r"\x1B(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])")


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
