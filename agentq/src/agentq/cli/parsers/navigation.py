"""Subparsers and dispatch for the navigation command domain."""

from __future__ import annotations

import argparse

from ..commands.navigation import run_continue, run_inspect, run_ts_nav
from ..registry import CommandSpec, Group
from .options import (
    line_range,
    nonnegative_int,
    positive_int,
)


def _ts_nav_options(p: argparse.ArgumentParser) -> None:
    p.add_argument(
        "action",
        choices=(
            "locate",
            "definition",
            "def",
            "references",
            "refs",
            "implementations",
            "impls",
            "overview",
        ),
    )
    p.add_argument(
        "symbol_arg",
        nargs="?",
        metavar="SYMBOL",
        help="symbol name for symbol-first navigation",
    )
    p.add_argument(
        "--symbol",
        dest="symbol_option",
        help="symbol name; equivalent to the optional positional SYMBOL",
    )
    p.add_argument("--file")
    p.add_argument("--line", type=positive_int)
    p.add_argument("--column", type=positive_int)
    p.add_argument(
        "--pick",
        type=positive_int,
        help="select a numbered symbol candidate when resolution is ambiguous",
    )
    p.add_argument("--limit", type=positive_int, default=80)


def _continue_options(p: argparse.ArgumentParser) -> None:
    p.add_argument(
        "cursor", help="cursor token from a previous 'continue: agentq continue …' hint"
    )


def _inspect_options(p: argparse.ArgumentParser) -> None:
    p.add_argument("target")
    p.add_argument(
        "--intent",
        choices=("locate", "understand", "edit"),
        default="understand",
        help="locate: candidates only; understand: declaration and references; edit: adds declaration body, tests, owning package, and verification scope",
    )
    p.add_argument(
        "--lang",
        choices=("typescript", "python"),
        help="restrict symbol resolution to one language provider",
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
        "--limit", "--max-results", dest="limit", type=positive_int, default=80
    )
    p.add_argument("--context", type=nonnegative_int, default=2)
    p.add_argument(
        "--max-lines",
        type=positive_int,
        default=240,
        help="global source-line cap; independent of --limit",
    )
    p.add_argument(
        "--repeat",
        action="store_true",
        help="force source windows already returned in the active context",
    )
    p.add_argument(
        "--candidate",
        metavar="ID",
        help=(
            "select a declaration candidate by its opaque candidate id "
            "(requires --intent edit)"
        ),
    )


COMMANDS = (
    CommandSpec(
        "ts-nav",
        help="TypeScript/JavaScript symbol-first or position-based semantic navigation",
        description=(
            "Resolve a known symbol directly, or query an exact file position. "
            "Symbol-first mode avoids a separate lexical search when declarations are unambiguous."
        ),
        execute=run_ts_nav,
        groups=(Group.COMMON, Group.SCOPE),
        configure=_ts_nav_options,
    ),
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
        help="single-entry repository inspection for symbols, literals, files, or source anchors",
        execute=run_inspect,
        groups=(Group.COMMON, Group.SCOPE),
        configure=_inspect_options,
    ),
)
