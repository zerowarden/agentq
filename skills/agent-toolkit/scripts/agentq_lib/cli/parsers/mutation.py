"""Subparsers for the mutation command domain."""

from __future__ import annotations

import argparse

from .options import (
    SubParsers,
    nonnegative_int,
    positive_int,
)
from .registry import Command, Group, register_commands


def _codemod_scan_options(p: argparse.ArgumentParser) -> None:
    p.add_argument("pattern")
    p.add_argument("--rewrite")
    p.add_argument("--mode", choices=("fixed", "regex", "ast"), default="fixed")
    p.add_argument("--lang")
    p.add_argument("--samples", type=positive_int, default=12)
    p.add_argument("--max-files", type=positive_int, default=100)
    p.add_argument(
        "--plan-out",
        help="write an immutable codemod plan (JSON) for later reviewed application",
    )


def _codemod_apply_options(p: argparse.ArgumentParser) -> None:
    p.add_argument(
        "pattern", nargs="?", help="codemod pattern (omit when using --plan)"
    )
    p.add_argument(
        "rewrite", nargs="?", help="codemod replacement (omit when using --plan)"
    )
    p.add_argument("--mode", choices=("fixed", "regex", "ast"), default="fixed")
    p.add_argument("--lang")
    p.add_argument("--expect-count", type=nonnegative_int)
    p.add_argument("--max-files", type=positive_int, default=100)
    p.add_argument(
        "--plan",
        help="apply a previously written codemod plan (JSON) instead of PATTERN/REWRITE",
    )
    p.add_argument("--apply", action="store_true")


COMMANDS = (
    Command(
        "codemod-scan",
        help="count and sample a proposed codemod without mutation",
        groups=(Group.COMMON, Group.SCOPE, Group.SENSITIVE),
        configure=_codemod_scan_options,
    ),
    Command(
        "codemod-apply",
        help="guarded codemod; dry-run unless --apply is explicit",
        groups=(Group.COMMON, Group.SCOPE),
        configure=_codemod_apply_options,
    ),
)


def register(sub: SubParsers) -> None:
    register_commands(sub, COMMANDS)
