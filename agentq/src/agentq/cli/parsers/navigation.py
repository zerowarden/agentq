"""Subparsers and dispatch for the navigation command domain."""

from __future__ import annotations

import argparse

from ..commands.navigation import run_continue, run_inspect
from ..registry import CommandSpec, Group
from .options import line_range, positive_int

INTENTS = ("understand", "edit", "rename", "refactor", "impact")


def _continue_options(p: argparse.ArgumentParser) -> None:
    p.add_argument(
        "cursor", help="cursor token from a previous 'continue: agentq continue …' hint"
    )


def _inspect_options(p: argparse.ArgumentParser) -> None:
    p.add_argument(
        "target",
        help=(
            "symbol name, repository-relative path, or a symbol:/path: prefixed "
            "selector when the string is ambiguous"
        ),
    )
    p.add_argument(
        "--intent",
        choices=INTENTS,
        default="understand",
        help=(
            "evidence emphasis: understand context, edit source and tests, "
            "rename references and mentions, refactor implementations, or impact "
            "dependents and ownership"
        ),
    )
    p.add_argument(
        "--line",
        dest="line_anchors",
        type=positive_int,
        nargs="+",
        action="extend",
        default=[],
        help="one or more source anchors when TARGET is a file; repeatable",
    )
    p.add_argument(
        "--lines",
        dest="line_ranges",
        type=line_range,
        action="append",
        default=[],
        help="explicit START:END source range when TARGET is a file; repeatable",
    )
    p.add_argument(
        "--column",
        type=positive_int,
        default=None,
        help="one-based column with a single --line; expresses an exact location",
    )
    p.add_argument(
        "--candidate",
        metavar="ID",
        help=(
            "select a declaration candidate by the opaque id issued for this "
            "repository state; applies to symbol targets under any intent"
        ),
    )
    p.add_argument(
        "--debug",
        action="store_true",
        help="write structured stage trace records to stderr",
    )


COMMANDS = (
    CommandSpec(
        "continue",
        help="resume a truncated result from a local continuation cursor",
        description=(
            "Replay a stored continuation command after validating that the cursor belongs to "
            "this repository and session and that the workspace has not changed since creation."
        ),
        execute=run_continue,
        groups=(Group.COMMON,),
        configure=_continue_options,
    ),
    CommandSpec(
        "inspect",
        help="single-entry repository inspection for symbols, paths, or source ranges",
        execute=run_inspect,
        groups=(Group.COMMON, Group.SCOPE),
        configure=_inspect_options,
    ),
)
