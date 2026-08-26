#!/usr/bin/env python3
from __future__ import annotations

import argparse
import difflib
import inspect
import json
import re
import sys
import time
from pathlib import Path
from typing import Any, Callable

from agentq_lib.audit import audit_data, render_audit
from agentq_lib.benchmark import benchmark_data, render_benchmark
from agentq_lib.budgeting import RenderedText, project_json
from agentq_lib.codemod import apply_data, render_apply, render_scan, scan_data
from agentq_lib.common import AgentQError, VERSION, bound_output, compact_line, ensure_within, relpath, repo_root
from agentq_lib.context_cache import operation_cache_key, operation_repeat_advice, remember_operation
from agentq_lib.doctor import doctor_data, render_doctor
from agentq_lib.deps import dependencies_data, render_dependencies
from agentq_lib.gitops import (
    diff_data,
    history_data,
    render_diff,
    render_history,
    render_status,
    render_structural,
    status_data,
    structural_diff_data,
)
from agentq_lib.impact import impact_data, render_impact
from agentq_lib.inspectops import inspect_data, render_inspect
from agentq_lib.output_attribution import attribute_output, output_view
from agentq_lib.runops import render_run, run_compact
from agentq_lib.search import (
    files_data,
    outline_data,
    read_data,
    render_files,
    render_outline,
    render_read,
    render_repo_map,
    render_search,
    repo_map_data,
    search_data,
)
from agentq_lib.tasking import current_task_state, render_task, task_changes, task_data
from agentq_lib.telemetry import (
    archive_hot_events,
    install_persistence,
    print_stats,
    record_event,
    remove_persistence,
    render_archive,
    render_persistence,
    render_reset,
    render_stats,
    render_storage,
    reset_telemetry,
    stats_data,
    storage_data,
    watch_stats,
)
from agentq_lib.testplan import render_test_plan, test_plan_data
from agentq_lib.tsnav import render_ts_nav, ts_nav_data
from agentq_lib.verifychanged import render_verify_changed, verify_changed_data

Formatter = Callable[..., str]


class AgentQArgumentParser(argparse.ArgumentParser):
    def __init__(self, *args: Any, **kwargs: Any) -> None:
        kwargs.setdefault("allow_abbrev", False)
        super().__init__(*args, **kwargs)

    def error(self, message: str) -> None:
        active = self
        for action in self._actions:
            if isinstance(action, argparse._SubParsersAction):
                selected = next((value for value in sys.argv[1:] if value in action.choices), None)
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
                nearest = difflib.get_close_matches(value, active._option_string_actions, n=1, cutoff=0.55)
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
        if usage.startswith('usage: '):
            usage = usage[7:]
        suffix = f"; {hint}" if hint else ""
        raise AgentQError(f"invalid arguments: {detail}{suffix}; usage: {compact_line(usage, 260)}")


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


def add_common(parser: argparse.ArgumentParser, *, formats: tuple[str, ...] = ("text", "json")) -> None:
    parser.add_argument("--repo", default=".", help="repository path; defaults to the current directory")
    parser.add_argument("--format", choices=formats, default="text", help="bounded human output or structured JSON")
    parser.add_argument("--budget", type=positive_int, default=12000, help="maximum model-visible characters; default 12000")


def add_scope(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--path", dest="paths", nargs="+", action="extend", default=[], help="scope to one or more files/directories; repeatable")


def add_sensitive(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--include-sensitive", action="store_true", help="explicitly include normally excluded sensitive paths")


def add_plan_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--base", help="optional base branch/commit to include committed changes")
    parser.add_argument("--mode", choices=("focused", "standard", "thorough"), default="standard")
    parser.add_argument("--dependents", choices=("auto", "none", "direct", "all"), default="auto")
    parser.add_argument("--include-build", action="store_true", help="include package build scripts")


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


def emit(args: argparse.Namespace, data: dict[str, Any], formatter: Formatter) -> None:
    internal = data.pop("_agentq_internal", {}) if isinstance(data.get("_agentq_internal"), dict) else {}
    telemetry_data = internal.get("telemetry_data") if isinstance(internal.get("telemetry_data"), dict) else data
    if args.format in {"json", "compact-json"}:
        full = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
        prebudget_chars = len(full)
        visible, truncated = project_json(data, args.budget)
    else:
        if "budget" in inspect.signature(formatter).parameters:
            rendered = formatter(data, budget=args.budget)
            visible = str(rendered).rstrip()
            prebudget_chars = rendered.prebudget_chars if isinstance(rendered, RenderedText) else len(visible)
            truncated = rendered.truncated if isinstance(rendered, RenderedText) else False
            if args.budget > 0 and len(visible) > args.budget:
                visible, hard_cut = bound_output(visible, args.budget)
                truncated = truncated or hard_cut
        else:
            full = formatter(data).rstrip()
            prebudget_chars = len(full)
            visible, truncated = bound_output(full, args.budget)
    print(visible)
    truncation_data = data
    if data.get("kind") == "source-windows" and isinstance(data.get("source"), dict):
        truncation_data = data["source"]
    render_budget_truncated = (
        truncated
        or bool(internal.get("truncated", False))
        or bool(truncation_data.get("render_budget_truncated", False))
    )
    args._agentq_data = telemetry_data
    args._agentq_render_meta = {
        "prebudget_chars": max(prebudget_chars, int(internal.get("prebudget_chars", 0) or 0)),
        "visible_chars": len(visible),
        "truncated": render_budget_truncated,
        "render_budget_truncated": render_budget_truncated,
        "source_cap_truncated": bool(truncation_data.get("source_cap_truncated", False)),
        "output_attribution": attribute_output(
            args.command, data, visible, output_format=args.format,
        ),
        "output_view": output_view(args.command, data),
    }


def render_context_repeat(data: dict[str, Any]) -> str:
    return (
        f"{data['command']}: exact result already returned in this {data['repeat_scope']}; "
        "use --repeat to render it again"
    )


def emit_cached(
    args: argparse.Namespace,
    root: Path,
    command: str,
    options: dict[str, Any],
    producer: Callable[[], dict[str, Any]],
    formatter: Formatter,
) -> None:
    key = operation_cache_key(root, command, {**options, "budget": args.budget, "format": args.format})
    advice = operation_repeat_advice(root, command, key)
    if advice and not args.repeat:
        emit(args, {
            "command": command,
            "repeat_suppressed": True,
            "repeat_scope": advice["scope"],
        }, render_context_repeat)
        return
    data = producer()
    remember_operation(root, command, key)
    emit(args, data, formatter)


def build_parser() -> argparse.ArgumentParser:
    parser = AgentQArgumentParser(
        prog="agentq",
        description="Privacy-preserving, token-bounded repository tools for coding agents.",
    )
    parser.add_argument("--version", action="version", version=f"agentq {VERSION}")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("doctor", help="report runtime/tool readiness and privacy defaults")
    add_common(p)

    p = sub.add_parser(
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
    )
    add_common(p)
    p.add_argument(
        "action",
        nargs="?",
        default="status",
        choices=("begin", "status", "changes", "accept", "abandon", "next", "start", "current", "done", "drop", "cancel"),
    )

    p = sub.add_parser("stats", help="visualize local agentq activity and output suppression")
    add_common(p)
    p.add_argument("--since", default="7d", help="all, or duration such as 24h, 7d, or 4w")
    p.add_argument("--detailed", "--detail", dest="detailed", action="store_true", help="show failures, command chains, semantic actions, read behavior, task distributions, and verification detail")
    p.add_argument("--recent", type=nonnegative_int, default=None, help="also show N recent operations; implies --detailed")
    p.add_argument("--operation", action="append", default=[], help="filter by operation; repeatable")
    p.add_argument("--all-repos", action="store_true", help="aggregate all locally observed repositories")
    p.add_argument("--watch", type=positive_float, help="refresh terminal dashboard every N seconds")
    p.add_argument("--color", choices=("auto", "always", "never"), default="auto")
    p.add_argument("--plain", action="store_true", help="force dependency-free plain-text rendering")
    p.add_argument("--utc", action="store_true", help="display timestamps in UTC instead of local time")
    p.add_argument("--archive", action="store_true", help="persist hot /tmp telemetry under XDG_STATE_HOME before rendering stats")
    admin = p.add_mutually_exclusive_group()
    admin.add_argument("--archive-only", action="store_true", help="archive hot telemetry without computing/rendering stats")
    admin.add_argument("--storage", action="store_true", help="show hot/persistent storage and archive-timer status")
    admin.add_argument("--reset", action="store_true", help="reset telemetry for the current repository; combine with --all-repos deliberately")
    admin.add_argument("--install-persistence", action="store_true", help="install and enable the user-level systemd archive timer")
    admin.add_argument("--remove-persistence", action="store_true", help="disable and remove the user-level systemd archive timer")
    p.add_argument("--hot-only", action="store_true", help="with --reset, remove only volatile /tmp telemetry")
    p.add_argument("--force", action="store_true", help="with --reset, allow reset while an agentq task is active")
    p.add_argument("--persistence-interval", default="5min", help="with --install-persistence; default 5min")

    p = sub.add_parser("files", help="find repository paths with bounded ranked output")
    add_common(p); add_scope(p); add_sensitive(p)
    p.add_argument("query", nargs="?", default="", help="filename/path fragment")
    p.add_argument("--limit", "--max-results", dest="limit", type=positive_int, default=60)

    p = sub.add_parser("search", help="bounded ripgrep search; fixed-string by default")
    add_common(p, formats=("text", "json", "compact-json")); add_scope(p); add_sensitive(p)
    p.add_argument("query")
    p.add_argument("trailing_paths", nargs="*", metavar="PATH")
    mode = p.add_mutually_exclusive_group()
    mode.add_argument("--fixed", dest="mode", action="store_const", const="fixed", default="fixed")
    mode.add_argument("--regex", dest="mode", action="store_const", const="regex")
    p.add_argument("--word", action="store_true")
    p.add_argument("--case", choices=("smart", "sensitive", "insensitive"), default="smart")
    p.add_argument("--glob", action="append", default=[])
    p.add_argument("--type", dest="types", action="append", default=[])
    p.add_argument("--limit", "--max-results", dest="limit", type=positive_int, default=80)
    p.add_argument("--per-file", "--samples-per-file", dest="per_file", type=positive_int, default=8)
    p.add_argument("--max-files", type=positive_int, default=40)
    p.add_argument("--view", choices=("auto", "summary", "snippets", "matches"), default="auto")
    p.add_argument("--scan-cap", type=positive_int, default=5000, help=argparse.SUPPRESS)
    p.add_argument("--context", type=nonnegative_int, default=0)
    p.add_argument("--max-chars", type=positive_int, default=240)
    p.add_argument("--repeat", action="store_true", help="force an exact result already returned in the active context")

    p = sub.add_parser("read", help="read bounded file ranges with line numbers")
    add_common(p); add_sensitive(p)
    p.add_argument("files", nargs="+")
    p.add_argument("--start", type=positive_int)
    p.add_argument("--line", dest="line_anchors", type=positive_int, nargs="+", action="extend", default=[], help="one or more source anchors; repeatable")
    p.add_argument("--lines", dest="line_ranges", type=line_range, action="append", default=[], help="explicit START:END source range; repeatable")
    p.add_argument("--end", type=positive_int)
    p.add_argument("--around", type=positive_int)
    p.add_argument("--context", type=nonnegative_int, default=20)
    p.add_argument("--max-lines", type=positive_int, default=240)
    p.add_argument("--max-chars", type=positive_int, default=260)
    p.add_argument("--allow-outside", action="store_true", help="explicitly permit reads outside the repository")
    p.add_argument("--repeat", action="store_true", help="force output for an unchanged range already returned in the active context")

    p = sub.add_parser("repo-map", help="compact repository/workspace map")
    add_common(p)
    p.add_argument("--max-dirs", type=positive_int, default=40)
    p.add_argument("--max-manifests", type=positive_int, default=40)

    p = sub.add_parser("outline", help="bounded symbol outline; ast-grep, ctags, then fallback")
    add_common(p)
    p.add_argument("paths", nargs="*", default=["."])
    p.add_argument("--match")
    p.add_argument("--public", action="store_true")
    p.add_argument("--lang")
    p.add_argument("--limit", type=positive_int, default=160)
    p.add_argument("--repeat", action="store_true", help="force an exact result already returned in the active context")

    p = sub.add_parser("git-status", help="compact porcelain-v2 status")
    add_common(p)
    p.add_argument("--limit", type=positive_int, default=80)

    p = sub.add_parser("git-diff", help="diff summary with optional bounded patch")
    add_common(p); add_scope(p)
    scope = p.add_mutually_exclusive_group()
    scope.add_argument("--staged", action="store_true")
    scope.add_argument("--unstaged", action="store_true")
    scope.add_argument("--base")
    scope.add_argument("--range", dest="range_value")
    view = p.add_mutually_exclusive_group()
    view.add_argument("--patch", action="store_true")
    view.add_argument("--hunks", action="store_true", help="show bounded hunk metadata without patch bodies")
    p.add_argument("--stat", action="store_true", help="summary view (accepted conventional alias for the default)")
    p.add_argument("--task", dest="task_scope", action="store_true", help="restrict paths to changes since the active agentq task baseline")
    p.add_argument("--context", type=nonnegative_int, default=2)
    p.add_argument("--max-files", type=positive_int, default=40)
    p.add_argument("--max-hunks", type=positive_int, default=60)
    p.add_argument("--max-lines", type=positive_int, default=700)
    p.add_argument("--repeat", action="store_true", help="force an unchanged task/thread-local diff to be rendered again")

    p = sub.add_parser("git-history", help="bounded commit history")
    add_common(p); add_scope(p)
    p.add_argument("--limit", type=positive_int, default=20)

    p = sub.add_parser("git-structural", help="single-file syntax-aware diff using difftastic")
    add_common(p)
    p.add_argument("path")
    p.add_argument("--context", type=nonnegative_int, default=3)
    p.add_argument("--max-lines", type=positive_int, default=500)

    p = sub.add_parser("dependencies", help="local workspace package dependency graph from manifests")
    add_common(p)
    p.add_argument("--target")
    p.add_argument("--depth", type=positive_int, default=2)
    p.add_argument("--limit", type=positive_int, default=100)

    p = sub.add_parser("impact", help="bounded lexical/import blast-radius evidence")
    add_common(p); add_scope(p)
    p.add_argument("target", help="symbol, file, directory, or public surface")
    p.add_argument("--limit", type=positive_int, default=120)

    p = sub.add_parser("codemod-scan", help="count and sample a proposed codemod without mutation")
    add_common(p); add_scope(p); add_sensitive(p)
    p.add_argument("pattern")
    p.add_argument("--rewrite")
    p.add_argument("--mode", choices=("fixed", "regex", "ast"), default="fixed")
    p.add_argument("--lang")
    p.add_argument("--samples", type=positive_int, default=12)
    p.add_argument("--max-files", type=positive_int, default=100)

    p = sub.add_parser("codemod-apply", help="guarded codemod; dry-run unless --apply is explicit")
    add_common(p); add_scope(p)
    p.add_argument("pattern")
    p.add_argument("rewrite")
    p.add_argument("--mode", choices=("fixed", "regex", "ast"), default="fixed")
    p.add_argument("--lang")
    p.add_argument("--expect-count", type=nonnegative_int)
    p.add_argument("--max-files", type=positive_int, default=100)
    p.add_argument("--apply", action="store_true")

    p = sub.add_parser("run", help="run argv without a shell; return diagnostics and a local redacted log")
    add_common(p)
    p.add_argument("--cwd")
    p.add_argument("--timeout", type=positive_int, default=900)
    p.add_argument("--label", default="command")
    p.add_argument("--max-diagnostics", type=positive_int, default=60)
    p.add_argument("--tail-lines", type=positive_int, default=40)
    p.add_argument("--offline", action="store_true")
    p.add_argument("argv", nargs=argparse.REMAINDER, help="command after --")

    p = sub.add_parser("test-plan", help="infer a workspace-aware verification ladder from changed files")
    add_common(p); add_plan_options(p)
    p.add_argument("--task", dest="task_scope", action="store_true", help="plan only files changed since the active task baseline")
    p.add_argument("--limit", type=positive_int, default=60)

    for name in ("verify", "verify-changed", "verified-changed", "verify-task"):
        p = sub.add_parser(name, help="plan and execute workspace-aware affected verification")
        add_common(p); add_plan_options(p)
        p.add_argument("--task", dest="task_scope", action="store_true", help="verify only files changed since the active task baseline")
        p.add_argument("--dry-run", action="store_true", help="show the plan without running commands")
        p.add_argument("--continue-on-failure", action="store_true")
        p.add_argument("--timeout", type=positive_int, default=900, help="timeout per verification step")
        p.add_argument("--max-steps", type=positive_int, default=40)
        p.add_argument("--max-diagnostics", type=positive_int, default=24)
        p.add_argument("--offline", action="store_true")
        p.add_argument("--skip-lint", action="store_true")

    p = sub.add_parser(
        "ts-nav",
        help="TypeScript/JavaScript symbol-first or position-based semantic navigation",
        description=(
            "Resolve a known symbol directly, or query an exact file position. "
            "Symbol-first mode avoids a separate lexical search when declarations are unambiguous."
        ),
    )
    add_common(p); add_scope(p)
    p.add_argument("action", choices=("locate", "definition", "def", "references", "refs", "implementations", "impls", "overview"))
    p.add_argument("symbol_arg", nargs="?", metavar="SYMBOL", help="symbol name for symbol-first navigation")
    p.add_argument("--symbol", dest="symbol_option", help="symbol name; equivalent to the optional positional SYMBOL")
    p.add_argument("--file")
    p.add_argument("--line", type=positive_int)
    p.add_argument("--column", type=positive_int)
    p.add_argument("--pick", type=positive_int, help="select a numbered symbol candidate when resolution is ambiguous")
    p.add_argument("--limit", type=positive_int, default=80)

    p = sub.add_parser("inspect", help="single-entry repository inspection for symbols, literals, files, or source anchors")
    add_common(p); add_scope(p)
    p.add_argument("target")
    p.add_argument("--line", dest="line_anchors", type=positive_int, nargs="+", action="extend", default=[], help="one or more source anchors when TARGET is a file; repeatable")
    p.add_argument("--lines", dest="line_ranges", type=line_range, action="append", default=[], help="explicit START:END source range when TARGET is a file; repeatable")
    p.add_argument("--limit", "--max-results", dest="limit", type=positive_int, default=80)
    p.add_argument("--context", type=nonnegative_int, default=2)
    p.add_argument("--max-lines", type=positive_int, default=240, help="global source-line cap; independent of --limit")
    p.add_argument("--repeat", action="store_true", help="force source windows already returned in the active context")

    p = sub.add_parser("audit", help="heuristic bounded audit of the current patch")
    add_common(p)
    scope = p.add_mutually_exclusive_group()
    scope.add_argument("--staged", action="store_true")
    scope.add_argument("--base")
    p.add_argument("--task", dest="task_scope", action="store_true", help="restrict the audit to changes since the active agentq task baseline")
    p.add_argument("--max-findings", type=positive_int, default=100)

    p = sub.add_parser("benchmark", help="benchmark one or more shell commands with hyperfine or a local fallback")
    add_common(p)
    p.add_argument("--command", dest="commands", action="append", required=True, help="shell command; repeatable")
    p.add_argument("--warmup", type=nonnegative_int, default=2)
    p.add_argument("--runs", type=positive_int, default=10)
    p.add_argument("--prepare")

    return parser


def execute(args: argparse.Namespace, root: Path) -> int:
    command = args.command
    if command == "doctor":
        emit(args, doctor_data(root), render_doctor)
    elif command == "task":
        emit(args, task_data(root, args.action), render_task)
    elif command == "stats":
        administrative = any((args.archive_only, args.storage, args.reset, args.install_persistence, args.remove_persistence))
        if args.archive and administrative:
            raise AgentQError("--archive cannot be combined with stats administrative actions")
        if args.hot_only and not args.reset:
            raise AgentQError("--hot-only requires --reset")
        if args.force and not args.reset:
            raise AgentQError("--force requires --reset")
        if args.watch and administrative:
            raise AgentQError("--watch cannot be combined with stats administrative actions")
        if args.archive_only:
            emit(args, archive_hot_events(), render_archive)
            return 0
        if args.storage:
            emit(args, storage_data(root), render_storage)
            return 0
        if args.install_persistence:
            emit(args, install_persistence(interval=args.persistence_interval), render_persistence)
            return 0
        if args.remove_persistence:
            emit(args, remove_persistence(), render_persistence)
            return 0
        if args.reset:
            emit(args, reset_telemetry(root, all_repos=args.all_repos, hot_only=args.hot_only, force=args.force), render_reset)
            return 0
        detailed = bool(args.detailed or args.recent is not None)
        recent = args.recent if args.recent is not None else 0
        if args.watch:
            if args.format != "text":
                raise AgentQError("--watch requires --format text")
            if args.archive:
                raise AgentQError("archive once before --watch; do not combine --archive and --watch")
            watch_stats(root, interval=args.watch, since=args.since, recent=recent, detailed=detailed,
                        operations=args.operation, all_repos=args.all_repos, color=args.color,
                        plain=args.plain, utc=args.utc, budget=args.budget)
            return 0
        data = stats_data(root, since=args.since, recent=recent, detailed=detailed, operations=args.operation,
                          all_repos=args.all_repos, archive=args.archive)
        if args.format == "json":
            emit(args, data, render_stats)
        else:
            print_stats(data, color=args.color, plain=args.plain, utc=args.utc, budget=args.budget)
            args._agentq_data = data
            args._agentq_render_meta = {"prebudget_chars": 0, "visible_chars": 0, "truncated": False}
    elif command == "files":
        emit(args, files_data(root, args.query, args.paths, args.limit, args.include_sensitive), render_files)
    elif command == "search":
        trailing_paths: list[str] = []
        for value in args.trailing_paths:
            try:
                candidate = ensure_within(root, Path(value))
            except AgentQError:
                raise AgentQError("search accepts one QUERY; use: agentq search QUERY --path PATH") from None
            if not candidate.exists():
                raise AgentQError("search accepts one QUERY; use: agentq search QUERY --path PATH")
            trailing_paths.append(relpath(root, candidate))
        search_paths = [*args.paths, *trailing_paths]
        continuation_options = {
            "mode": args.mode, "word": args.word, "case": args.case,
            "globs": args.glob, "types": args.types, "limit": args.limit,
            "per_file": args.per_file, "context": args.context, "max_chars": args.max_chars,
            "max_files": args.max_files, "scan_cap": args.scan_cap,
            "budget": args.budget, "output_format": args.format,
            "include_sensitive": args.include_sensitive,
        }
        emit_cached(
            args, root, command,
            {
                "query": args.query, "paths": search_paths, "mode": args.mode, "word": args.word,
                "case": args.case, "globs": args.glob, "types": args.types, "limit": args.limit,
                "per_file": args.per_file, "context": args.context, "max_chars": args.max_chars,
                "include_sensitive": args.include_sensitive, "view": args.view,
                "max_files": args.max_files, "scan_cap": args.scan_cap,
            },
            lambda: search_data(
                root, args.query, search_paths, mode=args.mode, word=args.word, case=args.case,
                globs=args.glob, types=args.types, limit=args.limit, per_file=args.per_file,
                context=args.context, max_chars=args.max_chars, include_sensitive=args.include_sensitive,
                view=args.view, max_files=args.max_files, scan_cap=args.scan_cap,
                compact=args.format == "compact-json", render_budget=args.budget,
                continuation_options=continuation_options if args.format != "json" else None,
            ),
            render_search,
        )
    elif command == "read":
        if (args.line_anchors or args.line_ranges) and (args.start is not None or args.end is not None or args.around is not None):
            raise AgentQError("--line/--lines cannot be combined with --start, --end, or --around")
        data = read_data(
            root, args.files, start=args.start, end=args.end, around=args.around,
            line_anchors=args.line_anchors, line_ranges=args.line_ranges, context=args.context,
            max_lines=args.max_lines, max_chars=args.max_chars, include_sensitive=args.include_sensitive,
            allow_outside=args.allow_outside, repeat=args.repeat, budget=args.budget,
            output_format=args.format,
        )
        emit(args, data, render_read)
    elif command == "repo-map":
        emit(args, repo_map_data(root, args.max_dirs, args.max_manifests), render_repo_map)
    elif command == "outline":
        emit_cached(
            args, root, command,
            {"paths": args.paths, "match": args.match, "public": args.public, "lang": args.lang, "limit": args.limit},
            lambda: outline_data(root, args.paths, args.match, args.public, args.lang, args.limit),
            render_outline,
        )
    elif command == "git-status":
        emit(args, status_data(root, args.limit), render_status)
    elif command == "git-diff":
        diff_paths = list(args.paths)
        scoped = None
        if args.task_scope:
            scoped = task_changes(root)
            diff_paths = sorted(set(diff_paths) & set(scoped["files"])) if diff_paths else list(scoped["files"])
        if args.task_scope and not diff_paths:
            data = {
                "repo_root": str(root), "scope": "active-task", "total_files": 0,
                "total_added": 0, "total_deleted": 0, "files": [], "files_truncated": False,
                "diff_check_ok": True, "diff_check": [],
            }
            if args.patch:
                data.update({"patch": "", "patch_stats": {}, "patch_truncated": False})
            elif args.hunks:
                data.update({"hunks": [], "hunk_stats": {}, "hunks_truncated": False})
        else:
            data = diff_data(root, staged=args.staged, unstaged=args.unstaged, base=args.base,
                             range_value=args.range_value, paths=diff_paths, patch=args.patch, hunks=args.hunks,
                             context=args.context, max_files=args.max_files, max_hunks=args.max_hunks,
                             max_lines=args.max_lines, repeat=args.repeat, budget=args.budget)
        if args.task_scope:
            data["task_scope"] = True
            data["task_ambiguous_preexisting"] = list((scoped or {}).get("ambiguous_preexisting", []))
            data["preexisting_unchanged_excluded"] = len((scoped or {}).get("excluded_preexisting_unchanged", []))
        emit(args, data, render_diff)
    elif command == "git-history":
        emit(args, history_data(root, args.limit, args.paths), render_history)
    elif command == "git-structural":
        emit(args, structural_diff_data(root, args.path, args.context, args.max_lines), render_structural)
    elif command == "dependencies":
        emit(args, dependencies_data(root, target=args.target, depth=args.depth, limit=args.limit), render_dependencies)
    elif command == "impact":
        emit(args, impact_data(root, args.target, args.paths, args.limit), render_impact)
    elif command == "codemod-scan":
        data = scan_data(root, args.pattern, scopes=args.paths, mode=args.mode, rewrite=args.rewrite,
                         language=args.lang, samples=args.samples, max_files=args.max_files,
                         include_sensitive=args.include_sensitive)
        emit(args, data, render_scan)
    elif command == "codemod-apply":
        data = apply_data(root, args.pattern, args.rewrite, scopes=args.paths, mode=args.mode,
                          language=args.lang, apply=args.apply, expect_count=args.expect_count,
                          max_files=args.max_files)
        emit(args, data, render_apply)
    elif command == "run":
        argv = list(args.argv)
        if argv and argv[0] == "--":
            argv = argv[1:]
        data = run_compact(root, argv, cwd=args.cwd, timeout=args.timeout, label=args.label,
                           max_diagnostics=args.max_diagnostics, tail_lines=args.tail_lines,
                           offline=args.offline)
        emit(args, data, render_run)
    elif command == "test-plan":
        scoped = task_changes(root) if args.task_scope else None
        data = test_plan_data(root, base=args.base, limit=args.limit, mode=args.mode,
                              dependents=args.dependents, include_build=args.include_build,
                              changed_override=list(scoped["files"]) if scoped else None)
        if scoped:
            data["task_scope"] = True
            data["task_ambiguous_preexisting"] = scoped.get("ambiguous_preexisting", [])
        emit(args, data, render_test_plan)
    elif command in {"verify", "verify-changed", "verified-changed", "verify-task"}:
        task_scoped = command == "verify-task" or args.task_scope or (command == "verify" and current_task_state(root) is not None)
        scoped = task_changes(root) if task_scoped else None
        data = verify_changed_data(
            root, base=args.base, mode=args.mode, dependents=args.dependents,
            include_build=args.include_build, dry_run=args.dry_run,
            continue_on_failure=args.continue_on_failure, timeout=args.timeout,
            max_steps=args.max_steps, max_diagnostics=args.max_diagnostics,
            offline=args.offline, skip_lint=args.skip_lint,
            changed_files_override=list(scoped["files"]) if scoped else None,
        )
        if scoped:
            data["task_scope"] = True
            data["task_ambiguous_preexisting"] = scoped.get("ambiguous_preexisting", [])
            data["preexisting_unchanged_excluded"] = len(scoped.get("excluded_preexisting_unchanged", []))
        data["verification_scope"] = "task" if task_scoped else "base" if args.base else "worktree"
        emit(args, data, render_verify_changed)
        return int(data.get("exit_code", 0))
    elif command == "ts-nav":
        action = {"refs": "references", "def": "definition", "impls": "implementations"}.get(args.action, args.action)
        if args.symbol_arg and args.symbol_option:
            raise AgentQError("provide SYMBOL either positionally or with --symbol, not both")
        symbol = args.symbol_option or args.symbol_arg
        # Compatibility with the common `path.ts:line:column` form.
        if symbol and not args.symbol_option:
            position = re.fullmatch(r"(.+):(\d+):(\d+)", symbol)
            if position and (root / position.group(1)).exists():
                args.file, args.line, args.column = position.group(1), int(position.group(2)), int(position.group(3))
                symbol = None
        exact_values = (args.file, args.line, args.column)
        if symbol and any(value is not None for value in exact_values):
            raise AgentQError("symbol-first navigation cannot be combined with --file, --line, or --column")
        if not symbol and action in {"locate", "overview"}:
            raise AgentQError(f"ts-nav {action} requires a symbol")
        if not symbol and not all(value is not None for value in exact_values):
            raise AgentQError("provide SYMBOL/--symbol or all of --file, --line, and --column")
        emit(
            args,
            ts_nav_data(
                root, action, args.file, args.line, args.column, args.limit,
                symbol=symbol, paths=args.paths, pick=args.pick,
            ),
            render_ts_nav,
        )
    elif command == "inspect":
        emit_cached(
            args, root, command,
            {
                "target": args.target, "paths": args.paths, "limit": args.limit, "context": args.context,
                "line_anchors": args.line_anchors, "line_ranges": args.line_ranges, "max_lines": args.max_lines,
            },
            lambda: inspect_data(
                root, args.target, args.paths, limit=args.limit, context=args.context,
                line_anchors=args.line_anchors, line_ranges=args.line_ranges,
                max_lines=args.max_lines, repeat=args.repeat, budget=args.budget,
                output_format=args.format,
            ),
            render_inspect,
        )
    elif command == "audit":
        scoped = task_changes(root) if args.task_scope else None
        data = audit_data(
            root, staged=args.staged, base=args.base,
            paths=list(scoped["files"]) if scoped else None,
            task_scope=args.task_scope, max_findings=args.max_findings,
        )
        if scoped:
            data["task_scope"] = True
            data["task_ambiguous_preexisting"] = list(scoped.get("ambiguous_preexisting", []))
            data["preexisting_unchanged_excluded"] = len(scoped.get("excluded_preexisting_unchanged", []))
        emit(args, data, render_audit)
    elif command == "benchmark":
        emit(args, benchmark_data(root, args.commands, args.warmup, args.runs, args.prepare), render_benchmark)
    else:
        raise AgentQError(f"unknown command: {command}")
    return 0


def main() -> int:
    start = time.perf_counter()
    root: Path | None = None
    args: argparse.Namespace | None = None
    try:
        parser = build_parser()
        args = parser.parse_args()
        stats_repo_optional = (
            getattr(args, "command", None) == "stats"
            and any((
                getattr(args, "archive_only", False),
                getattr(args, "storage", False),
                getattr(args, "install_persistence", False),
                getattr(args, "remove_persistence", False),
                getattr(args, "reset", False) and getattr(args, "all_repos", False),
            ))
        )
        root = Path(args.repo).expanduser().resolve() if stats_repo_optional else repo_root(args.repo)
        exit_code = execute(args, root)
        data = getattr(args, "_agentq_data", None)
        meta = getattr(args, "_agentq_render_meta", {})
        record_event(
            root,
            command=args.command,
            duration_ms=round((time.perf_counter() - start) * 1000),
            tool_status="ok",
            agentq_exit_code=exit_code,
            visible_chars=int(meta.get("visible_chars", 0)),
            prebudget_chars=int(meta.get("prebudget_chars", 0)),
            truncated=bool(meta.get("truncated", False)),
            render_budget_truncated=bool(meta.get("render_budget_truncated", False)),
            source_cap_truncated=bool(meta.get("source_cap_truncated", False)),
            data=data,
            invocation=sys.argv[1:],
            expansion_controls=expansion_controls(args),
            output_format=str(args.format),
            output_view=str(meta.get("output_view", "default")),
            output_attribution=meta.get("output_attribution"),
            repeat_requested=bool(getattr(args, "repeat", False)),
        )
        return exit_code
    except AgentQError as exc:
        if root is None:
            try:
                root = repo_root(".")
            except Exception:
                root = None
        error_format = str(getattr(args, "format", "")) if args else ""
        if error_format not in {"json", "compact-json", "text"}:
            requested_format = next(
                (value.split("=", 1)[1] for value in sys.argv if value.startswith("--format=")),
                None,
            )
            if requested_format is None and "--format" in sys.argv:
                index = sys.argv.index("--format")
                requested_format = sys.argv[index + 1] if index + 1 < len(sys.argv) else None
            error_format = requested_format if requested_format in {"json", "compact-json"} else "text"
        error_data = {"error": str(exc), "type": "AgentQError"}
        error_visible = (
            json.dumps(error_data, ensure_ascii=False, indent=2)
            if error_format in {"json", "compact-json"}
            else f"agentq: {exc}"
        )
        error_command = getattr(args, "command", "unknown") if args else (sys.argv[1] if len(sys.argv) > 1 else "unknown")
        if root is not None:
            record_event(
                root,
                command=error_command,
                duration_ms=round((time.perf_counter() - start) * 1000),
                tool_status="error",
                agentq_exit_code=2,
                visible_chars=len(error_visible),
                prebudget_chars=len(error_visible),
                error_type=type(exc).__name__,
                error_message=str(exc),
                invocation=sys.argv[1:],
                expansion_controls=expansion_controls(args) if args else None,
                output_format=error_format,
                output_view="default",
                output_attribution=attribute_output(
                    error_command, error_data, error_visible, output_format=error_format,
                ),
                repeat_requested=bool(getattr(args, "repeat", False)) if args else "--repeat" in sys.argv,
            )
        print(error_visible, file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("agentq: interrupted", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
