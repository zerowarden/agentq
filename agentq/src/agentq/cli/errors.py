"""CLI error serialization and reporting."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from agentq.discovery import repo_root

from .transport import write_stderr

_ERROR_FORMATS = frozenset({"text", "json", "compact-json"})
_JSON_ERROR_FORMATS = frozenset({"json", "compact-json"})


def report_error(
    exc: Exception,
    *,
    args: argparse.Namespace | None,
    root: Path | None,
    argv: list[str],
) -> int:
    """Render one CLI failure to stderr and return exit code 2."""
    if root is None:
        try:
            root = repo_root(".")
        except Exception:
            root = None
    error_format = _requested_error_format(args, argv)
    error_data = {"error": str(exc), "type": "AgentQError"}
    error_visible = (
        json.dumps(error_data, ensure_ascii=False, indent=2)
        if error_format in _JSON_ERROR_FORMATS
        else f"agentq: {exc}"
    )
    write_stderr(error_visible)
    return 2


def _requested_error_format(args: argparse.Namespace | None, argv: list[str]) -> str:
    """Best-effort output format for an error raised before or during parsing."""
    if args is not None:
        value = str(getattr(args, "format", ""))
        if value in _ERROR_FORMATS:
            return value
    inline = next(
        (item.split("=", 1)[1] for item in argv if item.startswith("--format=")),
        None,
    )
    if inline in _JSON_ERROR_FORMATS:
        return inline
    if "--format" in argv:
        index = argv.index("--format")
        separate = argv[index + 1] if index + 1 < len(argv) else None
        if separate in _JSON_ERROR_FORMATS:
            return separate
    return "text"
