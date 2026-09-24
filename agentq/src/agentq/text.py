"""Foundational bounded-text helpers shared by every capability.

Display-safe truncation, ANSI stripping, and redaction compose here, below the
capabilities that render output. Nothing in this module writes to a sink.
"""

from __future__ import annotations

import re

from agentq.redaction import redact_text

ANSI_RE = re.compile(r"\x1B(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])")
TRUNCATION_MARKER = " …[truncated]"
# Reserve for the marker inside the requested width so a compacted line never
# exceeds the caller's limit.
_TRUNCATION_RESERVE = 15


def strip_ansi(text: str) -> str:
    return ANSI_RE.sub("", text)


def cleaned_line(text: str) -> str:
    """One display line: ANSI-free, CR-free, redacted, newline-stripped."""
    return redact_text(strip_ansi(text).replace("\r", "").rstrip("\n"))


def compact_line(text: str, max_chars: int = 240) -> str:
    cleaned = cleaned_line(text)
    if len(cleaned) <= max_chars:
        return cleaned
    return cleaned[: max(0, max_chars - _TRUNCATION_RESERVE)] + TRUNCATION_MARKER


def line_truncated(text: str, max_chars: int) -> bool:
    """Whether :func:`compact_line` would truncate this raw line."""
    return len(cleaned_line(text)) > max_chars
