"""Subparsers for the git command domain."""

from __future__ import annotations

import argparse

from .options import (
    SubParsers,
    nonnegative_int,
    positive_int,
)
from .registry import Command, Group, register_commands


def _git_status_options(p: argparse.ArgumentParser) -> None:
    p.add_argument("--limit", type=positive_int, default=80)


def _git_diff_options(p: argparse.ArgumentParser) -> None:
    scope = p.add_mutually_exclusive_group()
    scope.add_argument("--staged", action="store_true")
    scope.add_argument("--unstaged", action="store_true")
    scope.add_argument("--base")
    scope.add_argument("--range", dest="range_value")
    view = p.add_mutually_exclusive_group()
    view.add_argument("--patch", action="store_true")
    view.add_argument(
        "--hunks",
        action="store_true",
        help="show bounded hunk metadata without patch bodies",
    )
    p.add_argument(
        "--stat",
        action="store_true",
        help="summary view (accepted conventional alias for the default)",
    )
    p.add_argument(
        "--task",
        dest="task_scope",
        action="store_true",
        help="restrict paths to changes since the active agentq task baseline",
    )
    p.add_argument("--context", type=nonnegative_int, default=2)
    p.add_argument("--max-files", type=positive_int, default=40)
    p.add_argument("--max-hunks", type=positive_int, default=60)
    p.add_argument("--max-lines", type=positive_int, default=700)
    p.add_argument(
        "--repeat",
        action="store_true",
        help="force an unchanged task/thread-local diff to be rendered again",
    )


def _git_history_options(p: argparse.ArgumentParser) -> None:
    p.add_argument("--limit", type=positive_int, default=20)


def _git_structural_options(p: argparse.ArgumentParser) -> None:
    p.add_argument("path")
    p.add_argument("--context", type=nonnegative_int, default=3)
    p.add_argument("--max-lines", type=positive_int, default=500)


def _dependencies_options(p: argparse.ArgumentParser) -> None:
    p.add_argument("--target")
    p.add_argument("--depth", type=positive_int, default=2)
    p.add_argument("--limit", type=positive_int, default=100)


def _impact_options(p: argparse.ArgumentParser) -> None:
    p.add_argument("target", help="symbol, file, directory, or public surface")
    p.add_argument("--limit", type=positive_int, default=120)


def _audit_options(p: argparse.ArgumentParser) -> None:
    scope = p.add_mutually_exclusive_group()
    scope.add_argument("--staged", action="store_true")
    scope.add_argument("--base")
    p.add_argument(
        "--task",
        dest="task_scope",
        action="store_true",
        help="restrict the audit to changes since the active agentq task baseline",
    )
    p.add_argument("--max-findings", type=positive_int, default=100)


COMMANDS = (
    Command(
        "git-status",
        help="compact porcelain-v2 status",
        groups=(Group.COMMON,),
        configure=_git_status_options,
    ),
    Command(
        "git-diff",
        help="diff summary with optional bounded patch",
        groups=(Group.COMMON, Group.SCOPE),
        configure=_git_diff_options,
    ),
    Command(
        "git-history",
        help="bounded commit history",
        groups=(Group.COMMON, Group.SCOPE),
        configure=_git_history_options,
    ),
    Command(
        "git-structural",
        help="single-file syntax-aware diff using difftastic",
        groups=(Group.COMMON,),
        configure=_git_structural_options,
    ),
    Command(
        "dependencies",
        help="local workspace package dependency graph from manifests",
        groups=(Group.COMMON,),
        configure=_dependencies_options,
    ),
    Command(
        "impact",
        help="bounded lexical/import blast-radius evidence",
        groups=(Group.COMMON, Group.SCOPE),
        configure=_impact_options,
    ),
    Command(
        "audit",
        help="heuristic bounded audit of the current patch",
        groups=(Group.COMMON,),
        configure=_audit_options,
    ),
)


def register(sub: SubParsers) -> None:
    register_commands(sub, COMMANDS)
