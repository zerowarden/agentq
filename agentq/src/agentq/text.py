"""Foundational bounded-text helpers shared by every capability.

Display-safe truncation, ANSI stripping, and redaction compose here, below the
capabilities that render output. Nothing in this module writes to a sink.
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
