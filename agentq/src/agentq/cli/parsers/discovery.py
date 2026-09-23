"""Subparsers and dispatch for the discovery command domain."""

from __future__ import annotations

import argparse

from ..commands.discovery import run_search
from ..registry import CommandSpec
from .options import add_common, add_scope


def _search_options(p: argparse.ArgumentParser) -> None:
    add_common(p, formats=("text", "json", "compact-json"))
    add_scope(p)
    p.add_argument("query")
    p.add_argument(
        "--regex",
        dest="mode",
        action="store_const",
        const="regex",
        default="fixed",
        help="regex semantics; fixed-string is the default",
    )


COMMANDS = (
    CommandSpec(
        "search",
        help="bounded ripgrep search; fixed-string by default",
        execute=run_search,
        groups=(),
        configure=_search_options,
    ),
)
