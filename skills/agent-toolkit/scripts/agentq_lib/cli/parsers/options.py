"""Shared argument helpers, validators, and subparser option groups."""

from __future__ import annotations

import argparse
import difflib
import re
import sys
from typing import Any, Protocol

from agentq_lib.common import AgentQError, compact_line


class SubParsers(Protocol):
    """Structural type for argparse's subparser action."""

    def add_parser(self, name: str, **kwargs: Any) -> argparse.ArgumentParser: ...


class AgentQArgumentParser(argparse.ArgumentParser):
    def __init__(self, *args: Any, **kwargs: Any) -> None:
        kwargs.setdefault("allow_abbrev", False)
        super().__init__(*args, **kwargs)

    def error(self, message: str) -> None:
        active = self
        for action in self._actions:
            if isinstance(action, argparse._SubParsersAction):
                selected = next(
                    (value for value in sys.argv[1:] if value in action.choices), None
                )
                if selected:
                    active = action.choices[selected]
                    break
        hint = None
        option = re.search(r"unrecognized arguments?:\s+(--[A-Za-z0-9-]+)", message)
        if option:
            value = option.group(1)
            if value == "--include-source":
                hint = "use --line N or --lines START:END to preview source for a file target"
            else:
                nearest = difflib.get_close_matches(
                    value, active._option_string_actions, n=1, cutoff=0.55
                )
                if nearest:
                    hint = f"did you mean {nearest[0]}?"
            detail = f"unrecognized option {value}"
        else:
            choice = re.search(r"invalid choice: ['\"]([^'\"]+)['\"]", message)
            if choice:
                value = choice.group(1)
                choices = [
                    str(candidate)
                    for action in self._actions
                    if action.choices
                    for candidate in action.choices
                ]
                nearest = difflib.get_close_matches(value, choices, n=1, cutoff=0.45)
                if nearest:
                    hint = f"did you mean {nearest[0]}?"
                detail = f"invalid choice {value!r}"
            else:
                detail = compact_line(message, 180)
        usage = " ".join(active.format_usage().split())
        if usage.startswith("usage: "):
            usage = usage[7:]
        suffix = f"; {hint}" if hint else ""
        raise AgentQError(
            f"invalid arguments: {detail}{suffix}; usage: {compact_line(usage, 260)}"
        )


def line_range(value: str) -> tuple[int, int]:
    match = re.fullmatch(r"(\d+)(?::|-)(\d+)", value.strip())
    if not match:
        raise argparse.ArgumentTypeError("must be START:END or START-END")
    start, end = int(match.group(1)), int(match.group(2))
    if start < 1 or end < start:
        raise argparse.ArgumentTypeError("must satisfy 1 <= START <= END")
    return start, end


def positive_int(value: str) -> int:
    parsed = int(value)
    if parsed < 1:
        raise argparse.ArgumentTypeError("must be at least 1")
    return parsed


def nonnegative_int(value: str) -> int:
    parsed = int(value)
    if parsed < 0:
        raise argparse.ArgumentTypeError("must be non-negative")
    return parsed


def positive_float(value: str) -> float:
    parsed = float(value)
    if parsed <= 0:
        raise argparse.ArgumentTypeError("must be greater than zero")
    return parsed


def add_common(
    parser: argparse.ArgumentParser, *, formats: tuple[str, ...] = ("text", "json")
) -> None:
    parser.add_argument(
        "--repo", default=".", help="repository path; defaults to the current directory"
    )
    parser.add_argument(
        "--format",
        choices=formats,
        default="text",
        help="bounded human output or structured JSON",
    )
    parser.add_argument(
        "--budget",
        type=positive_int,
        default=12000,
        help="maximum model-visible characters; default 12000",
    )


def add_scope(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--path",
        dest="paths",
        nargs="+",
        action="extend",
        default=[],
        help="scope to one or more files/directories; repeatable",
    )


def add_sensitive(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--include-sensitive",
        action="store_true",
        help="explicitly include normally excluded sensitive paths",
    )


def add_plan_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--base", help="optional base branch/commit to include committed changes"
    )
    parser.add_argument(
        "--mode", choices=("focused", "standard", "thorough"), default="standard"
    )
    parser.add_argument(
        "--dependents", choices=("auto", "none", "direct", "all"), default="auto"
    )
    parser.add_argument(
        "--include-build", action="store_true", help="include package build scripts"
    )


def expansion_controls(args: argparse.Namespace) -> dict[str, int]:
    return {
        target: int(value)
        for target, value in (
            ("budget", getattr(args, "budget", None)),
            ("limit", getattr(args, "limit", None)),
            ("max_lines", getattr(args, "max_lines", None)),
            ("max_files", getattr(args, "max_files", None)),
            ("max_hunks", getattr(args, "max_hunks", None)),
            ("max_chars", getattr(args, "max_chars", None)),
            ("scan_cap", getattr(args, "scan_cap", None)),
            ("samples_per_file", getattr(args, "per_file", None)),
        )
        if isinstance(value, (int, float)) and not isinstance(value, bool)
    }
