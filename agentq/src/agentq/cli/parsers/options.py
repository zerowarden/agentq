"""Shared argument helpers, validators, and subparser option groups."""

from __future__ import annotations

import argparse
import difflib
import re
import sys
from typing import Any, NoReturn, Protocol, cast

from agentq.core import AgentQError
from agentq.text import compact_line


class SubParsers(Protocol):
    """Structural type for argparse's subparser action."""

    def add_parser(self, name: str, **kwargs: Any) -> argparse.ArgumentParser: ...


class _SubParserChoices(Protocol):
    """Structural type for a subparser action's selected-parser mapping."""

    choices: dict[str, argparse.ArgumentParser]


_SubParsersActionType = (
    argparse._SubParsersAction  # pyright: ignore[reportPrivateUsage]
)


class AgentQArgumentParser(argparse.ArgumentParser):
    def __init__(self, *args: Any, **kwargs: Any) -> None:
        kwargs.setdefault("allow_abbrev", False)
        super().__init__(*args, **kwargs)

    def error(self, message: str) -> NoReturn:
        active: argparse.ArgumentParser = self
        for action in self._actions:
            if isinstance(action, _SubParsersActionType):
                choices = cast("_SubParserChoices", action).choices
                selected = next(
                    (value for value in sys.argv[1:] if value in choices), None
                )
                if selected:
                    active = choices[selected]
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


def add_common(
    parser: argparse.ArgumentParser, *, formats: tuple[str, ...] = ("text", "json")
) -> None:
    parser.add_argument(
        "--format",
        choices=formats,
        default="text",
        help="bounded human output or structured JSON",
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
