"""Subparsers for the discovery command domain."""

from __future__ import annotations

import argparse

from .options import (
    SubParsers,
    add_common,
    add_scope,
    add_sensitive,
    line_range,
    nonnegative_int,
    positive_float,
    positive_int,
)
from .registry import Command, Group, register_commands


def _task_options(p: argparse.ArgumentParser) -> None:
    p.add_argument(
        "action",
        nargs="?",
        default="status",
        choices=(
            "begin",
            "status",
            "changes",
            "accept",
            "abandon",
            "next",
            "start",
            "current",
            "done",
            "drop",
            "cancel",
        ),
    )


def _stats_options(p: argparse.ArgumentParser) -> None:
    p.add_argument(
        "--since", default="7d", help="all, or duration such as 24h, 7d, or 4w"
    )
    p.add_argument(
        "--detailed",
        "--detail",
        dest="detailed",
        action="store_true",
        help="show failures, command chains, semantic actions, read behavior, task distributions, and verification detail",
    )
    p.add_argument(
        "--recent",
        type=nonnegative_int,
        default=None,
        help="also show N recent operations; implies --detailed",
    )
    p.add_argument(
        "--operation",
        action="append",
        default=[],
        help="filter by operation; repeatable",
    )
    p.add_argument(
        "--all-repos",
        action="store_true",
        help="aggregate all locally observed repositories",
    )
    p.add_argument(
        "--watch",
        type=positive_float,
        help="refresh terminal dashboard every N seconds",
    )
    p.add_argument("--color", choices=("auto", "always", "never"), default="auto")
    p.add_argument(
        "--plain",
        action="store_true",
        help="force dependency-free plain-text rendering",
    )
    p.add_argument(
        "--utc",
        action="store_true",
        help="display timestamps in UTC instead of local time",
    )
    p.add_argument(
        "--archive",
        action="store_true",
        help="persist hot /tmp telemetry under XDG_STATE_HOME before rendering stats",
    )
    admin = p.add_mutually_exclusive_group()
    admin.add_argument(
        "--archive-only",
        action="store_true",
        help="archive hot telemetry without computing/rendering stats",
    )
    admin.add_argument(
        "--storage",
        action="store_true",
        help="show hot/persistent storage and archive-timer status",
    )
    admin.add_argument(
        "--reset",
        action="store_true",
        help="reset telemetry for the current repository; combine with --all-repos deliberately",
    )
    admin.add_argument(
        "--install-persistence",
        action="store_true",
        help="install and enable the user-level systemd archive timer",
    )
    admin.add_argument(
        "--remove-persistence",
        action="store_true",
        help="disable and remove the user-level systemd archive timer",
    )
    p.add_argument(
        "--hot-only",
        action="store_true",
        help="with --reset, remove only volatile /tmp telemetry",
    )
    p.add_argument(
        "--force",
        action="store_true",
        help="with --reset, allow reset while an agentq task is active",
    )
    p.add_argument(
        "--persistence-interval",
        default="5min",
        help="with --install-persistence; default 5min",
    )


def _files_options(p: argparse.ArgumentParser) -> None:
    p.add_argument("query", nargs="?", default="", help="filename/path fragment")
    p.add_argument(
        "--limit", "--max-results", dest="limit", type=positive_int, default=60
    )


def _search_options(p: argparse.ArgumentParser) -> None:
    add_common(p, formats=("text", "json", "compact-json"))
    add_scope(p)
    add_sensitive(p)
    p.add_argument("query")
    p.add_argument("trailing_paths", nargs="*", metavar="PATH")
    mode = p.add_mutually_exclusive_group()
    mode.add_argument(
        "--fixed", dest="mode", action="store_const", const="fixed", default="fixed"
    )
    mode.add_argument("--regex", dest="mode", action="store_const", const="regex")
    p.add_argument("--word", action="store_true")
    p.add_argument(
        "--case", choices=("smart", "sensitive", "insensitive"), default="smart"
    )
    p.add_argument("--glob", action="append", default=[])
    p.add_argument("--type", dest="types", action="append", default=[])
    p.add_argument(
        "--limit", "--max-results", dest="limit", type=positive_int, default=80
    )
    p.add_argument(
        "--per-file",
        "--samples-per-file",
        dest="per_file",
        type=positive_int,
        default=8,
    )
    p.add_argument("--max-files", type=positive_int, default=40)
    p.add_argument(
        "--view", choices=("auto", "summary", "snippets", "matches"), default="auto"
    )
    p.add_argument(
        "--coverage",
        dest="coverage_policy",
        choices=("fast", "auto", "exact"),
        default="auto",
        help=(
            "fast: single collection pass, lower-bound totals when the scan cap is reached; "
            "auto: single pass, exact totals unless the cap is reached (default); "
            "exact: additional counting pass for exact totals"
        ),
    )
    p.add_argument(
        "--scan-cap", type=positive_int, default=5000, help=argparse.SUPPRESS
    )
    p.add_argument("--context", type=nonnegative_int, default=0)
    p.add_argument("--max-chars", type=positive_int, default=240)
    p.add_argument(
        "--repeat",
        action="store_true",
        help="force an exact result already returned in the active context",
    )


def _read_options(p: argparse.ArgumentParser) -> None:
    p.add_argument("files", nargs="+")
    p.add_argument("--start", type=positive_int)
    p.add_argument(
        "--line",
        dest="line_anchors",
        type=positive_int,
        nargs="+",
        action="extend",
        default=[],
        help="one or more source anchors; repeatable",
    )
    p.add_argument(
        "--lines",
        dest="line_ranges",
        type=line_range,
        action="append",
        default=[],
        help="explicit START:END source range; repeatable",
    )
    p.add_argument("--end", type=positive_int)
    p.add_argument("--around", type=positive_int)
    p.add_argument("--context", type=nonnegative_int, default=20)
    p.add_argument("--max-lines", type=positive_int, default=240)
    p.add_argument("--max-chars", type=positive_int, default=260)
    p.add_argument(
        "--allow-outside",
        action="store_true",
        help="explicitly permit reads outside the repository",
    )
    p.add_argument(
        "--repeat",
        action="store_true",
        help="force output for an unchanged range already returned in the active context",
    )


def _repo_map_options(p: argparse.ArgumentParser) -> None:
    p.add_argument("--max-dirs", type=positive_int, default=40)
    p.add_argument("--max-manifests", type=positive_int, default=40)


def _outline_options(p: argparse.ArgumentParser) -> None:
    p.add_argument("paths", nargs="*", default=["."])
    p.add_argument("--match")
    p.add_argument("--public", action="store_true")
    p.add_argument("--lang")
    p.add_argument("--limit", type=positive_int, default=160)
    p.add_argument(
        "--repeat",
        action="store_true",
        help="force an exact result already returned in the active context",
    )


COMMANDS = (
    Command(
        "doctor",
        help="report runtime/tool readiness and privacy defaults",
        groups=(Group.COMMON,),
    ),
    Command(
        "task",
        help="mark independently acceptable work units inside one or more Codex threads",
        description=(
            "Track one independently acceptable implementation, fix, refactor, or review outcome. "
            "A Codex thread may contain several sequential tasks; debugging and verification retries stay inside the current task."
        ),
        epilog=(
            "Ergonomic aliases: start=begin, current=status, done=accept, drop=abandon. "
            "Use 'next' to accept the current task and immediately begin another in the same worktree."
        ),
        groups=(Group.COMMON,),
        configure=_task_options,
    ),
    Command(
        "stats",
        help="visualize local agentq activity and output suppression",
        groups=(Group.COMMON,),
        configure=_stats_options,
    ),
    Command(
        "files",
        help="find repository paths with bounded ranked output",
        groups=(Group.COMMON, Group.SCOPE, Group.SENSITIVE),
        configure=_files_options,
    ),
    Command(
        "search",
        help="bounded ripgrep search; fixed-string by default",
        groups=(),
        configure=_search_options,
    ),
    Command(
        "read",
        help="read bounded file ranges with line numbers",
        groups=(Group.COMMON, Group.SENSITIVE),
        configure=_read_options,
    ),
    Command(
        "repo-map",
        help="compact repository/workspace map",
        groups=(Group.COMMON,),
        configure=_repo_map_options,
    ),
    Command(
        "outline",
        help="bounded symbol outline; ast-grep, ctags, then fallback",
        groups=(Group.COMMON,),
        configure=_outline_options,
    ),
)


def register(sub: SubParsers) -> None:
    register_commands(sub, COMMANDS)
