#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Any, Callable

from agentq_lib.audit import audit_data, render_audit
from agentq_lib.benchmark import benchmark_data, render_benchmark
from agentq_lib.codemod import apply_data, render_apply, render_scan, scan_data
from agentq_lib.common import AgentQError, VERSION, bound_output, repo_root
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
from agentq_lib.tasking import render_task, task_data
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

Formatter = Callable[[dict[str, Any]], str]


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


def add_common(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--repo", default=".", help="repository path; defaults to the current directory")
    parser.add_argument("--format", choices=("text", "json"), default="text", help="bounded human output or structured JSON")
    parser.add_argument("--budget", type=positive_int, default=12000, help="maximum model-visible characters; default 12000")


def add_scope(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--path", dest="paths", action="append", default=[], help="scope to a file/directory; repeatable")


def add_sensitive(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--include-sensitive", action="store_true", help="explicitly include normally excluded sensitive paths")


def add_plan_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--base", help="optional base branch/commit to include committed changes")
    parser.add_argument("--mode", choices=("focused", "standard", "thorough"), default="standard")
    parser.add_argument("--dependents", choices=("auto", "none", "direct", "all"), default="auto")
    parser.add_argument("--include-build", action="store_true", help="include package build scripts")


def emit(args: argparse.Namespace, data: dict[str, Any], formatter: Formatter) -> None:
    if args.format == "json":
        full = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
        truncated = args.budget > 0 and len(full) > args.budget
        if truncated:
            visible = json.dumps({
                "truncated": True,
                "reason": "structured output exceeds agentq character budget",
                "chars": len(full),
                "budget": args.budget,
                "hint": "narrow scope or explicitly raise --budget",
            }, ensure_ascii=False, separators=(",", ":"))
        else:
            visible = full
    else:
        full = formatter(data).rstrip()
        visible, truncated = bound_output(full, args.budget)
    print(visible)
    args._agentq_data = data
    args._agentq_render_meta = {
        "prebudget_chars": len(full),
        "visible_chars": len(visible),
        "truncated": truncated,
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="agentq",
        description="Privacy-preserving, token-bounded repository tools for coding agents.",
    )
    parser.add_argument("--version", action="version", version=f"agentq {VERSION}")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("doctor", help="report runtime/tool readiness and privacy defaults")
    add_common(p)

    p = sub.add_parser("task", help="mark explicit task boundaries for per-task efficiency telemetry")
    add_common(p)
    p.add_argument("action", choices=("begin", "status", "accept", "abandon"))

    p = sub.add_parser("stats", help="visualize local agentq activity and output suppression")
    add_common(p)
    p.add_argument("--since", default="7d", help="all, or duration such as 24h, 7d, or 4w")
    p.add_argument("--recent", type=nonnegative_int, default=8)
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
    p.add_argument("--limit", type=positive_int, default=60)

    p = sub.add_parser("search", help="bounded ripgrep search; fixed-string by default")
    add_common(p); add_scope(p); add_sensitive(p)
    p.add_argument("query")
    mode = p.add_mutually_exclusive_group()
    mode.add_argument("--fixed", dest="mode", action="store_const", const="fixed", default="fixed")
    mode.add_argument("--regex", dest="mode", action="store_const", const="regex")
    p.add_argument("--word", action="store_true")
    p.add_argument("--case", choices=("smart", "sensitive", "insensitive"), default="smart")
    p.add_argument("--glob", action="append", default=[])
    p.add_argument("--type", dest="types", action="append", default=[])
    p.add_argument("--limit", type=positive_int, default=80)
    p.add_argument("--per-file", type=positive_int, default=8)
    p.add_argument("--context", type=nonnegative_int, default=0)
    p.add_argument("--max-chars", type=positive_int, default=240)

    p = sub.add_parser("read", help="read bounded file ranges with line numbers")
    add_common(p); add_sensitive(p)
    p.add_argument("files", nargs="+")
    p.add_argument("--start", type=positive_int)
    p.add_argument("--end", type=positive_int)
    p.add_argument("--around", type=positive_int)
    p.add_argument("--context", type=nonnegative_int, default=20)
    p.add_argument("--max-lines", type=positive_int, default=240)
    p.add_argument("--max-chars", type=positive_int, default=260)
    p.add_argument("--allow-outside", action="store_true", help="explicitly permit reads outside the repository")

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
    p.add_argument("--patch", action="store_true")
    p.add_argument("--context", type=nonnegative_int, default=2)
    p.add_argument("--max-files", type=positive_int, default=40)
    p.add_argument("--max-hunks", type=positive_int, default=60)
    p.add_argument("--max-lines", type=positive_int, default=700)

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
    p.add_argument("--limit", type=positive_int, default=60)

    for name in ("verify-changed", "verified-changed"):
        p = sub.add_parser(name, help="plan and execute workspace-aware affected verification")
        add_common(p); add_plan_options(p)
        p.add_argument("--dry-run", action="store_true", help="show the plan without running commands")
        p.add_argument("--continue-on-failure", action="store_true")
        p.add_argument("--timeout", type=positive_int, default=900, help="timeout per verification step")
        p.add_argument("--max-steps", type=positive_int, default=40)
        p.add_argument("--max-diagnostics", type=positive_int, default=24)
        p.add_argument("--offline", action="store_true")
        p.add_argument("--skip-lint", action="store_true")

    p = sub.add_parser("ts-nav", help="TypeScript/JavaScript semantic definition, reference, or implementation lookup")
    add_common(p)
    p.add_argument("action", choices=("definition", "references", "implementations"))
    p.add_argument("--file", required=True)
    p.add_argument("--line", type=positive_int, required=True)
    p.add_argument("--column", type=positive_int, required=True)
    p.add_argument("--limit", type=positive_int, default=80)

    p = sub.add_parser("audit", help="heuristic bounded audit of the current patch")
    add_common(p)
    scope = p.add_mutually_exclusive_group()
    scope.add_argument("--staged", action="store_true")
    scope.add_argument("--base")
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
        if args.watch:
            if args.format != "text":
                raise AgentQError("--watch requires --format text")
            if args.archive:
                raise AgentQError("archive once before --watch; do not combine --archive and --watch")
            watch_stats(root, interval=args.watch, since=args.since, recent=args.recent,
                        operations=args.operation, all_repos=args.all_repos, color=args.color,
                        plain=args.plain, utc=args.utc)
            return 0
        data = stats_data(root, since=args.since, recent=args.recent, operations=args.operation,
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
        data = search_data(root, args.query, args.paths, mode=args.mode, word=args.word, case=args.case,
                           globs=args.glob, types=args.types, limit=args.limit, per_file=args.per_file,
                           context=args.context, max_chars=args.max_chars, include_sensitive=args.include_sensitive)
        emit(args, data, render_search)
    elif command == "read":
        data = read_data(root, args.files, start=args.start, end=args.end, around=args.around,
                         context=args.context, max_lines=args.max_lines, max_chars=args.max_chars,
                         include_sensitive=args.include_sensitive, allow_outside=args.allow_outside)
        emit(args, data, render_read)
    elif command == "repo-map":
        emit(args, repo_map_data(root, args.max_dirs, args.max_manifests), render_repo_map)
    elif command == "outline":
        emit(args, outline_data(root, args.paths, args.match, args.public, args.lang, args.limit), render_outline)
    elif command == "git-status":
        emit(args, status_data(root, args.limit), render_status)
    elif command == "git-diff":
        data = diff_data(root, staged=args.staged, unstaged=args.unstaged, base=args.base,
                         range_value=args.range_value, paths=args.paths, patch=args.patch,
                         context=args.context, max_files=args.max_files, max_hunks=args.max_hunks,
                         max_lines=args.max_lines)
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
        data = test_plan_data(root, base=args.base, limit=args.limit, mode=args.mode,
                              dependents=args.dependents, include_build=args.include_build)
        emit(args, data, render_test_plan)
    elif command in {"verify-changed", "verified-changed"}:
        data = verify_changed_data(
            root, base=args.base, mode=args.mode, dependents=args.dependents,
            include_build=args.include_build, dry_run=args.dry_run,
            continue_on_failure=args.continue_on_failure, timeout=args.timeout,
            max_steps=args.max_steps, max_diagnostics=args.max_diagnostics,
            offline=args.offline, skip_lint=args.skip_lint,
        )
        emit(args, data, render_verify_changed)
        return int(data.get("exit_code", 0))
    elif command == "ts-nav":
        emit(args, ts_nav_data(root, args.action, args.file, args.line, args.column, args.limit), render_ts_nav)
    elif command == "audit":
        emit(args, audit_data(root, staged=args.staged, base=args.base, max_findings=args.max_findings), render_audit)
    elif command == "benchmark":
        emit(args, benchmark_data(root, args.commands, args.warmup, args.runs, args.prepare), render_benchmark)
    else:
        raise AgentQError(f"unknown command: {command}")
    return 0


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    start = time.perf_counter()
    root: Path | None = None
    try:
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
            data=data,
        )
        return exit_code
    except AgentQError as exc:
        if root is not None:
            record_event(
                root,
                command=getattr(args, "command", "unknown"),
                duration_ms=round((time.perf_counter() - start) * 1000),
                tool_status="error",
                agentq_exit_code=2,
                error_type=type(exc).__name__,
            )
        if getattr(args, "format", "text") == "json":
            print(json.dumps({"error": str(exc), "type": "AgentQError"}, indent=2), file=sys.stderr)
        else:
            print(f"agentq: {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("agentq: interrupted", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
