"""CLI error serialization, reporting, and failure telemetry."""

from __future__ import annotations

import argparse
import json
import time
from pathlib import Path

from agentq.core import telemetry_enabled
from agentq.discovery import repo_root

from .parsers.options import expansion_controls
from .transport import write_stderr

_ERROR_FORMATS = frozenset({"text", "json", "compact-json"})
_JSON_ERROR_FORMATS = frozenset({"json", "compact-json"})


def report_error(
    exc: Exception,
    *,
    start: float,
    args: argparse.Namespace | None,
    root: Path | None,
    argv: list[str],
) -> int:
    """Render one CLI failure to stderr, record it, and return exit code 2."""
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
    command = _error_command(args, argv)
    _record_failure(
        exc,
        root=root,
        command=command,
        argv=argv,
        args=args,
        error_data=error_data,
        error_visible=error_visible,
        error_format=error_format,
        duration_ms=round((time.perf_counter() - start) * 1000),
    )
    write_stderr(error_visible)
    return 2


def _record_failure(
    exc: Exception,
    *,
    root: Path | None,
    command: str,
    argv: list[str],
    args: argparse.Namespace | None,
    error_data: dict[str, str],
    error_visible: str,
    error_format: str,
    duration_ms: int,
) -> None:
    if root is None or not telemetry_enabled():
        return
    from agentq.telemetry import Observation, record_error

    record_error(
        root,
        Observation(
            command=command,
            argv=tuple(argv),
            output_format=error_format,
            expansion_controls=expansion_controls(args) if args is not None else None,
            repeat_requested=_repeat_requested(args, argv),
        ),
        duration_ms=duration_ms,
        error=exc,
        error_data=error_data,
        error_visible=error_visible,
    )


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


def _error_command(args: argparse.Namespace | None, argv: list[str]) -> str:
    if args is not None:
        return str(getattr(args, "command", "unknown"))
    return argv[0] if argv else "unknown"


def _repeat_requested(args: argparse.Namespace | None, argv: list[str]) -> bool:
    if args is not None:
        return bool(getattr(args, "repeat", False))
    return "--repeat" in argv
